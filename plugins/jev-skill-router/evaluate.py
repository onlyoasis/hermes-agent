"""Offline evaluation harness for jev-skill-router (plan §6-§7, stage D2).

Reproducible runner that scores the router's per-turn skill decisions
against a frozen, clearly-labeled Chinese case set held OUTSIDE the
repository (``--cases`` path); synthetic fixtures kept in-repo under
``tests/plugins/jev_eval_fixtures/`` verify the harness itself.

Honesty rules baked into the design (plan §6):

* Denominators are sacred — every case lands in exactly one outcome
  label; failures, infra skips and even harness errors stay counted.
  Only successful API calls are never reported alone.
* Arm A ("existing agent") cannot be observed offline: it is reported
  as ``unverified`` and is NEVER simulated as passing.
* Arm B is a deterministic local keyword baseline over the same frozen
  catalog (ASCII words + CJK bigrams vs id/name/description), gated by
  the same explicit-mention check as the router.
* Arm C drives the REAL production pipeline (``_route_turn``) with the
  responder injected at the client seam — scripted fixtures verify
  plumbing only; a scripted run says nothing about model quality, so
  latency/usage/cost are flagged invalid unless the responder is live.
* Live vendor calls are a separate explicit entry: they require an
  acknowledgement flag plus credentials, and never run in unit tests.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

import yaml

from . import catalog as catalog_mod
from . import client as client_mod
from . import policy
from . import questions as questions_mod

logger = logging.getLogger(__name__)

CASES_SCHEMA_VERSION = "jev-skill-eval-cases-v1"
RESPONSES_SCHEMA_VERSION = "jev-skill-eval-responses-v1"
REPORT_SCHEMA_VERSION = "jev-skill-eval-report-v1"

SPLITS = ("dev", "threshold_validation", "final_test")
ORIGINS = ("natural", "synthetic")
KINDS = ("recommend", "no_skill", "explicit_skill", "unsupported")
OUTCOME_LABELS = (
    "correct", "wrong_load", "extra_load", "missed",
    "unavailable_failure", "infra_skipped",
    "boundary_violation", "runner_error",
)

# Clearly fake key so the scripted path never touches a real credential.
SCRIPTED_API_KEY = "k-eval-scripted-not-a-secret"


class EvalInputError(ValueError):
    """Case/responses/parameter problem — refuse to run rather than guess."""


@dataclass(frozen=True)
class EvalCase:
    """One labeled evaluation case (gold expectation included)."""

    case_id: str
    group_id: str
    split: str
    origin: str
    user_message: str
    kind: str
    gold_skill_id: str
    forbidden_skill_ids: FrozenSet[str]


# ---------------------------------------------------------------------------
# Input loading & validation
# ---------------------------------------------------------------------------

def load_cases(path: Path) -> List[EvalCase]:
    """Load and validate the case file. Any problem raises EvalInputError
    listing every finding — an ambiguous gold set must never run."""
    data = _read_json(Path(path))
    if not isinstance(data, dict) or data.get("schema_version") != CASES_SCHEMA_VERSION:
        raise EvalInputError(f"cases file: expected schema_version {CASES_SCHEMA_VERSION}")
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise EvalInputError("cases: non-empty list required")

    errors: List[str] = []
    cases: List[EvalCase] = []
    seen_ids: Set[str] = set()
    group_splits: Dict[str, Set[str]] = {}
    for index, raw in enumerate(raw_cases):
        if not isinstance(raw, dict):
            errors.append(f"cases[{index}]: expected object")
            continue
        case_id = raw.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            errors.append(f"cases[{index}]: case_id required")
            continue
        if case_id in seen_ids:
            errors.append(f"duplicate case_id {case_id!r}")
            continue
        seen_ids.add(case_id)

        split = raw.get("split")
        origin = raw.get("origin")
        group_id = raw.get("group_id")
        message = raw.get("user_message")
        bad = False
        if split not in SPLITS:
            errors.append(f"{case_id}: split must be one of {', '.join(SPLITS)}")
            bad = True
        if origin not in ORIGINS:
            errors.append(f"{case_id}: origin must be natural or synthetic")
            bad = True
        if not isinstance(group_id, str) or not group_id.strip():
            errors.append(f"{case_id}: group_id required (rewrite-group containment)")
            bad = True
        if not isinstance(message, str) or not message.strip():
            errors.append(f"{case_id}: user_message required")
            bad = True

        expectation = raw.get("expectation")
        kind = expectation.get("kind") if isinstance(expectation, dict) else None
        gold = expectation.get("skill_id", "") if isinstance(expectation, dict) else ""
        forbidden = expectation.get("forbidden_skill_ids", []) if isinstance(expectation, dict) else []
        if kind not in KINDS:
            errors.append(f"{case_id}: expectation.kind must be one of {', '.join(KINDS)}")
            bad = True
        elif kind in ("recommend", "explicit_skill"):
            if not isinstance(gold, str) or not gold.strip():
                errors.append(f"{case_id}: kind {kind} requires expectation.skill_id")
                bad = True
        elif gold:
            errors.append(f"{case_id}: kind {kind} must not carry skill_id")
            bad = True
        if not isinstance(forbidden, list) or not all(isinstance(v, str) for v in forbidden):
            errors.append(f"{case_id}: forbidden_skill_ids must be a list of skill ids")
            forbidden = []
            bad = True

        if bad:
            continue
        assert isinstance(split, str) and isinstance(origin, str) and isinstance(group_id, str)
        group_splits.setdefault(group_id, set()).add(split)
        cases.append(EvalCase(
            case_id=case_id, group_id=group_id, split=split, origin=origin,
            user_message=message, kind=kind, gold_skill_id=str(gold).strip(),
            forbidden_skill_ids=frozenset(forbidden),
        ))

    for group, splits in sorted(group_splits.items()):
        if len(splits) > 1:
            errors.append(
                f"group {group!r} spans splits {sorted(splits)}; rewrite groups must "
                f"stay within one split (plan §6 near-duplicate leakage)"
            )
    if errors:
        raise EvalInputError("; ".join(errors))
    return cases


def load_responses(path: Path, known_case_ids: Set[str]) -> Dict[str, List[Any]]:
    """Load scripted vendor answers as per-case action sequences.

    Each action is a response dict (returned as-is) or a JevError
    instance (raised at that call position). Unknown case ids are
    fixture-authoring bugs and fail loudly."""
    data = _read_json(Path(path))
    if not isinstance(data, dict) or data.get("schema_version") != RESPONSES_SCHEMA_VERSION:
        raise EvalInputError(f"responses file: expected schema_version {RESPONSES_SCHEMA_VERSION}")
    responses = data.get("responses")
    if not isinstance(responses, dict):
        raise EvalInputError("responses: object required")
    unknown = sorted(set(responses) - set(known_case_ids))
    if unknown:
        raise EvalInputError(f"responses reference unknown case ids: {', '.join(unknown)}")
    actions: Dict[str, List[Any]] = {}
    for case_id, entry in sorted(responses.items()):
        phases = entry.get("phases") if isinstance(entry, dict) else None
        if not isinstance(phases, list):
            raise EvalInputError(f"responses[{case_id}]: phases list required")
        sequence: List[Any] = []
        for index, phase in enumerate(phases):
            if isinstance(phase, dict) and "error" in phase:
                sequence.append(_scripted_error(case_id, index, phase["error"]))
            elif isinstance(phase, dict):
                sequence.append(phase)
            else:
                raise EvalInputError(f"responses[{case_id}].phases[{index}]: object required")
        actions[case_id] = sequence
    return actions


def _scripted_error(case_id: str, index: int, name: Any) -> Exception:
    exc = getattr(client_mod, str(name), None)
    if not (isinstance(exc, type) and issubclass(exc, client_mod.JevError)):
        raise EvalInputError(
            f"responses[{case_id}].phases[{index}]: {name!r} is not a JevError class"
        )
    return exc(f"scripted {name} (eval fixture)")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvalInputError(f"{path}: unreadable JSON ({exc})") from exc


# ---------------------------------------------------------------------------
# Scripted responder (client-seam injection)
# ---------------------------------------------------------------------------

class ScriptedJevClient:
    """Deterministic JevClient stand-in: pops fixture answers in call order.

    Fixture answers say nothing about the real vendor — a scripted run
    verifies the harness (plumbing, classification, denominators)."""

    def __init__(self, actions: List[Any]) -> None:
        self._actions = list(actions)

    def ask(self, state: Any, questions: Any, timeout_ms: Any) -> Mapping[str, Any]:
        if not self._actions:
            raise RuntimeError("scripted responses exhausted (fixture bug)")
        action = self._actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------

def classify_outcome(case: EvalCase, record: Mapping[str, Any]) -> str:
    """Map one decision record to exactly one outcome label.

    Labels: ``correct``; ``wrong_load`` (a different skill than gold, or
    any suggestion for an unsupported request); ``extra_load``
    (suggestion nobody needed — no_skill, or the user already named the
    skill); ``missed`` (gold existed, router abstained); failure buckets
    ``unavailable_failure`` / ``infra_skipped`` / ``runner_error``; and
    ``boundary_violation`` (suggested a forbidden id) which outranks
    everything (plan §6 zero-tolerance)."""
    status = str(record.get("status") or "")
    if status == "runner_error":
        return "runner_error"
    suggested = str(record.get("shortlist_or_skill") or "") if status == "suggested" else ""
    if suggested and suggested in case.forbidden_skill_ids:
        return "boundary_violation"
    reason = str(record.get("reason_code") or "")

    if case.kind == "recommend":
        if suggested:
            return "correct" if suggested == case.gold_skill_id else "wrong_load"
        if status == "abstain":
            return "missed"
        if status == "unavailable":
            return "unavailable_failure"
        return "infra_skipped"
    if case.kind in ("no_skill", "unsupported"):
        if suggested:
            return "extra_load" if case.kind == "no_skill" else "wrong_load"
        if status == "abstain":
            return "correct"
        if status == "unavailable":
            return "unavailable_failure"
        return "infra_skipped"
    # explicit_skill: the deterministic mention gate is the designed
    # outcome; any outcome without a suggestion is acceptable (the wasted
    # call, if any, stays visible in the calls/latency metrics).
    if suggested:
        return "extra_load"
    if status == "unavailable":
        return "unavailable_failure"
    if status == "skipped" and reason != "explicit_skill_mention":
        return "infra_skipped"
    return "correct"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def wilson_interval(correct: int, n: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    """Wilson score interval — small samples must report uncertainty (§6)."""
    if n <= 0:
        return (None, None)
    p = correct / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


def _percentile(values: Sequence[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, math.ceil(pct / 100.0 * len(ordered)) - 1)
    return round(ordered[rank], 1)


def aggregate_rows(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-case rows; the counts always sum to the denominator."""
    n = len(rows)
    counts: Dict[str, int] = {label: 0 for label in OUTCOME_LABELS}
    for row in rows:
        counts[str(row.get("label"))] += 1
    correct = counts["correct"]
    all_ms = [float(row.get("elapsed_ms") or 0.0) for row in rows]
    send_ms = [float(row.get("elapsed_ms") or 0.0) for row in rows if row.get("calls")]

    def latency(values: List[float]) -> Dict[str, Any]:
        if not values:
            return {"p50": None, "p95": None, "max": None}
        return {"p50": _percentile(values, 50), "p95": _percentile(values, 95),
                "max": round(max(values), 1)}

    def tokens(field: str) -> int:
        return sum(int(row.get(field) or 0) for row in rows)

    return {
        "denominator": n,
        "counts": {label: value for label, value in counts.items() if value},
        "accuracy": (correct / n) if n else None,
        "accuracy_ci95": list(wilson_interval(correct, n)),
        "suggested": sum(1 for row in rows if row.get("status") == "suggested"),
        "abstain": sum(1 for row in rows if row.get("status") == "abstain"),
        "calls_total": sum(int(row.get("calls") or 0) for row in rows),
        "usage": {"input_tokens_total": tokens("input_tokens"),
                  "output_tokens_total": tokens("output_tokens")},
        "cost_usd_total": round(sum(float(row.get("cost_usd") or 0.0) for row in rows), 12),
        "latency_ms": latency(all_ms),
        "latency_ms_sendpath": latency(send_ms),
    }


# ---------------------------------------------------------------------------
# Arm B: deterministic keyword baseline
# ---------------------------------------------------------------------------

_ASCII_WORD = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[一-鿿]+")


def _tokens(text: str) -> Set[str]:
    lowered = text.lower()
    tokens = set(_ASCII_WORD.findall(lowered))
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def keyword_baseline_suggestion(user_message: str, entries: Any, min_score: int = 2) -> str:
    """Arm B (plan §6): local keyword matching over id/name/description.

    Deliberately simple and fully deterministic — ASCII words + CJK
    bigrams; the SAME explicit-mention gate as the router; ties break
    alphabetically. Its threshold is a baseline parameter, not a tuned
    product setting."""
    ids = [entry.skill_id for entry in entries]
    if policy.mentions_explicit_skill(user_message, ids):
        return ""
    query = _tokens(user_message)
    scored: List[Tuple[int, str]] = []
    for entry in entries:
        doc = " ".join((
            entry.skill_id.replace("-", " ").replace("_", " "),
            entry.name or "",
            entry.description or "",
        ))
        score = len(query & _tokens(doc))
        if score >= min_score:
            scored.append((score, entry.skill_id))
    if not scored:
        return ""
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored[0][1]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_evaluation(
    *,
    cases_path: Path,
    skills_dir: Path,
    out_dir: Path,
    responses_path: Optional[Path] = None,
    responder: str = "scripted",
    platform: str = "telegram",
    min_any_match: float = 0.5,
    min_fit: float = 0.5,
    min_choice_prob: float = 0.5,
    daily_budget_usd: float = 1.0,
    max_input_tokens: int = 200000,
    model: str = "jev-1.13.0",
    kw_min_score: int = 2,
    split_filter: Optional[Set[str]] = None,
    acknowledge_cost: bool = False,
    api_key_env: str = "TYPESAFE_API_KEY",
) -> Dict[str, Any]:
    """Run one reproducible evaluation and write report.json + cases.jsonl.

    Arm C drives the real production pipeline (shadow mode, its own
    isolated HERMES_HOME under ``out_dir``) with the responder injected
    at the client seam. Returns the report mapping."""
    if responder not in ("scripted", "live"):
        raise EvalInputError(f"responder must be 'scripted' or 'live' (got {responder!r})")
    if responder == "scripted" and responses_path is None:
        raise EvalInputError("scripted responder requires responses_path")
    if responder == "live":
        if not acknowledge_cost:
            raise EvalInputError(
                "live responder requires acknowledge_cost=True: real API calls "
                "cost money and send data (authorized D2 only, plan §6)"
            )
        if not (os.environ.get(api_key_env) or "").strip():
            raise EvalInputError(f"live responder requires ${api_key_env} to be set")

    router = _router_module()
    cases = load_cases(Path(cases_path))
    actions = (load_responses(Path(responses_path), {c.case_id for c in cases})
               if responder == "scripted" else {})
    if split_filter:
        cases = [case for case in cases if case.split in set(split_filter)]

    out_dir = Path(out_dir)
    home = _prepare_eval_home(out_dir, Path(skills_dir), platform=platform, model=model,
                              api_key_env=api_key_env,
                              min_any_match=min_any_match, min_fit=min_fit,
                              min_choice_prob=min_choice_prob,
                              daily_budget_usd=daily_budget_usd,
                              max_input_tokens=max_input_tokens)
    saved_env = {key: os.environ.get(key) for key in ("HERMES_HOME", api_key_env)}
    os.environ["HERMES_HOME"] = str(home)
    if responder == "scripted":
        os.environ[api_key_env] = SCRIPTED_API_KEY
    try:
        from agent.skill_utils import _external_dirs_cache_clear

        _external_dirs_cache_clear()
        router._DEDUP = router.policy.TurnDedup()
        compiled = catalog_mod.compile_catalog(None)
        _validate_gold_ids(cases, set(compiled.ids()))
        ledger = policy.BudgetLedger(home, daily_budget_usd)

        jev_rows = [
            _run_case(router, case, home, ledger, actions.get(case.case_id, []),
                      platform=platform, model=model, scripted=(responder == "scripted"))
            for case in cases
        ]
        kw_rows = [_run_keyword_case(case, compiled, min_score=kw_min_score) for case in cases]

        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run": {
                "responder": responder,
                "latency_valid": responder == "live",
                "usage_valid": responder == "live",
                "cost_valid": responder == "live",
                "scripted_note": (None if responder == "live" else
                                  "scripted vendor answers: harness plumbing verification "
                                  "only, NOT model-quality evidence"),
                "platform": platform,
                "model": model,
                "plugin_version": _plugin_version(),
                "question_version": questions_mod.QUESTION_VERSION,
                "git_rev": _git_rev(),
                "catalog_revision": router._catalog_revision(compiled),
                "thresholds": {"min_any_match": min_any_match, "min_fit": min_fit,
                               "min_choice_prob": min_choice_prob,
                               "kw_min_score": kw_min_score},
                "budget": {"daily_budget_usd": daily_budget_usd,
                           "max_input_tokens": max_input_tokens},
                "inputs": _input_manifest(cases_path, responses_path),
                "skills_manifest": sorted(compiled.ids()),
                "split_case_counts": _split_counts(cases),
                "final_ledger": ledger.snapshot(),
            },
            "arms": {
                "existing_agent": {
                    "arm": "existing_agent",
                    "status": "unverified",
                    "reason": "Main-agent behaviour (plan §6 arm A) cannot be observed "
                              "offline; requires authorised D2 evidence with the real "
                              "agent. NOT simulated.",
                },
                "keyword_baseline": {
                    "status": "computed",
                    "by_split": _group_rows(kw_rows, cases),
                    "per_case": kw_rows,
                },
                "jev_router": {
                    "status": "computed",
                    "by_split": _group_rows(jev_rows, cases),
                    "per_case": jev_rows,
                },
            },
        }
        _write_outputs(out_dir, report, jev_rows)
        return report
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _prepare_eval_home(out_dir: Path, skills_dir: Path, **config_overrides: Any) -> Path:
    """Fresh isolated HERMES_HOME under out_dir: frozen catalog copy +
    shadow-mode config. Deleted and rebuilt each run for reproducibility."""
    skills_src = Path(skills_dir)
    skill_dirs = sorted(p for p in skills_src.iterdir() if p.is_dir()) if skills_src.is_dir() else []
    if not skill_dirs:
        raise EvalInputError(f"skills dir {skills_src} contains no skill folders")
    home = out_dir / "eval-home"
    if home.exists():
        shutil.rmtree(home)
    (home / "skills").mkdir(parents=True)
    for src in skill_dirs:
        shutil.copytree(src, home / "skills" / src.name)
    entry = {
        "mode": "shadow",  # evaluate suggestions + records; never inject
        "allowed_platforms": [config_overrides["platform"]],
        "outbound_catalog_full": True,  # the frozen eval catalog IS the declared scope
    }
    entry.update({key: value for key, value in config_overrides.items()
                  if key not in ("platform",)})
    config = {"plugins": {"entries": {"jev-skill-router": entry}}, "skills": {}}
    (home / "config.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return home


def _validate_gold_ids(cases: List[EvalCase], catalog_ids: Set[str]) -> None:
    errors = [f"case {case.case_id}: gold skill {case.gold_skill_id!r} not in the "
              f"frozen eval catalog"
              for case in cases
              if case.kind in ("recommend", "explicit_skill")
              and case.gold_skill_id not in catalog_ids]
    if errors:
        raise EvalInputError("; ".join(errors))


def _run_case(router: Any, case: EvalCase, home: Path, ledger: Any, actions: List[Any],
              *, platform: str, model: str, scripted: bool) -> Dict[str, Any]:
    """Run ONE case through the real pipeline; measure via the production
    audit trail (decision record) and ledger deltas."""
    before = ledger.snapshot()
    client = ScriptedJevClient(actions) if scripted else None
    turn = {
        "session_id": f"eval-{case.case_id}",
        "task_id": "jev-eval",
        "turn_id": case.case_id,
        "user_message": case.user_message,
        "conversation_history": [],
        "is_first_turn": True,
        "model": model,
        "platform": platform,
        "parent_session_id": "",
        "sender_id": "jev-eval",
    }
    try:
        router._route_turn(turn, client_factory=(lambda api_key, cfg: client) if scripted else None)
        record: Mapping[str, Any] = _last_decision(home)
        if record is None:
            record = {"status": "runner_error", "reason_code": "no_decision_record"}
    except Exception as exc:  # harness/fixture bug: loud, and still counted
        record = {"status": "runner_error", "reason_code": f"{type(exc).__name__}: {exc}"}
    after = ledger.snapshot()

    usage_all = record.get("usage_all") or []

    def tokens(field: str) -> int:
        total = 0
        for usage in usage_all:
            if isinstance(usage, Mapping):
                value = usage.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    total += value
        return total

    suggested = (str(record.get("shortlist_or_skill") or "")
                 if record.get("status") == "suggested" else "")
    return {
        "case_id": case.case_id,
        "label": classify_outcome(case, record),
        "status": str(record.get("status") or ""),
        "reason_code": str(record.get("reason_code") or ""),
        "suggested": suggested,
        "abstain": record.get("status") == "abstain",
        "elapsed_ms": round(float(record.get("elapsed_ms") or 0.0), 1),
        "calls": int(after["requests"] - before["requests"]),
        "input_tokens": tokens("input_tokens"),
        "output_tokens": tokens("output_tokens"),
        "cost_usd": round(max(0.0, after["spent_usd"] - before["spent_usd"]), 12),
    }


def _run_keyword_case(case: EvalCase, compiled: Any, *, min_score: int) -> Dict[str, Any]:
    suggested = keyword_baseline_suggestion(case.user_message, compiled.entries,
                                            min_score=min_score)
    if policy.mentions_explicit_skill(case.user_message, compiled.ids()):
        record = {"status": "skipped", "reason_code": "explicit_skill_mention"}
    elif suggested:
        record = {"status": "suggested", "reason_code": "keyword_match"}
    else:
        record = {"status": "abstain", "reason_code": "no_keyword_match"}
    return {
        "case_id": case.case_id,
        "label": classify_outcome(case, record),
        "status": record["status"],
        "reason_code": record["reason_code"],
        "suggested": suggested,
        "abstain": record["status"] == "abstain",
        "elapsed_ms": 0.0,
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
    }


def _group_rows(rows: List[Mapping[str, Any]], cases: List[EvalCase]) -> Dict[str, Any]:
    """Segment by split × origin; every combination is present (empty
    segments show denominator 0 — synthetic never merges into natural)."""
    meta = {case.case_id: (case.split, case.origin) for case in cases}
    grouped: Dict[str, Dict[str, List[Mapping[str, Any]]]] = {}
    for row in rows:
        split, origin = meta.get(str(row.get("case_id")), ("?", "?"))
        grouped.setdefault(split, {}).setdefault(origin, []).append(row)
    return {split: {origin: aggregate_rows(grouped.get(split, {}).get(origin, []))
                    for origin in ORIGINS}
            for split in SPLITS}


def _split_counts(cases: List[EvalCase]) -> Dict[str, int]:
    counts: Dict[str, int] = {split: 0 for split in SPLITS}
    for case in cases:
        counts[case.split] += 1
    return counts


def _input_manifest(cases_path: Path, responses_path: Optional[Path]) -> List[Dict[str, Any]]:
    manifest = [{"path": str(Path(cases_path).resolve()), "sha256": _sha256(Path(cases_path))}]
    if responses_path is not None:
        manifest.append({"path": str(Path(responses_path).resolve()),
                         "sha256": _sha256(Path(responses_path))})
    return manifest


def _write_outputs(out_dir: Path, report: Mapping[str, Any], jev_rows: List[Mapping[str, Any]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "cases.jsonl").open("w", encoding="utf-8") as fh:
        for row in jev_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _last_decision(home: Path) -> Optional[Mapping[str, Any]]:
    path = Path(home) / "jev-skill-router" / "decisions.jsonl"
    if not path.exists():
        return None
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except ValueError:
        return None


def _router_module() -> Any:
    """The plugin package __init__ (the orchestrator under evaluation)."""
    return sys.modules[__package__]


def _plugin_version() -> Optional[str]:
    try:
        manifest = yaml.safe_load(
            (Path(__file__).parent / "plugin.yaml").read_text(encoding="utf-8"))
        return str(manifest.get("version")) if isinstance(manifest, dict) else None
    except (OSError, ValueError):
        return None


def _git_rev() -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(Path(__file__).parent),
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
