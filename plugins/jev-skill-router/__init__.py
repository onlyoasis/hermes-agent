"""Hermes plugin: optional Jev (TypeSafe) skill-recommendation router.

Registered hooks only — no tools, no permission surface, no schema cost
for agents that don't enable it. The whole pipeline is fail-open: any
unexpected error returns ``None`` and the turn proceeds exactly as it
would have without this plugin (plan §1: “所有失败路径都退回原流程”).

Pipeline per user turn (plan §3):

1. deterministic gate — mode/surface/delegation/dedup (policy layer);
2. compile the outbound catalog from the ACTIVE profile's loadable
   skills (catalog layer);
3. skip when the user already names a skill explicitly;
4. skip entirely when ``max_requests_per_turn < 2`` — v1 has no
   verified single-phase decision path;
5. two-phase Choice/Noul questions within the turn deadline
   (questions + client layers, one POST each, no retries); EACH request
   atomically reserves its own conservative budget slice before sending
   and settles it after — a phase-2 request that no longer fits the
   daily cap is simply not sent;
6. per-primitive threshold decision, candidate re-availability check,
   whole-payload contract validation;
7. shadow mode records only; recommend mode additionally returns the
   fixed-template ``{"context": ...}`` injected into THIS turn's user
   message API copy only.

Audit records (decisions.jsonl in the profile home) carry no user text,
no skill bodies, no credentials (plan §5).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import catalog as catalog_mod
from . import client as client_mod
from . import policy
from . import questions as questions_mod

logger = logging.getLogger(__name__)

# Bounded per-(profile, session, turn) state shared across hook calls.
_DEDUP = policy.TurnDedup()

# Deadline split between the two vendor requests: phase 1 (full catalog
# ranking) is the bigger payload, phase 2 verifies a tiny shortlist.
_PHASE_ONE_DEADLINE_SHARE = 0.6

# The only user data sent to the vendor: the current request text (plan
# §3.2 input minimisation — no conversation history).
_STATE_TEMPLATE = "用户本轮请求：{request}"


def register(ctx: Any) -> None:
    """Plugin entry point. Registers hooks; registers no tools."""
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)


# ---------------------------------------------------------------------------
# Hook callbacks
# ---------------------------------------------------------------------------

def _on_session_end(**kwargs: Any) -> None:
    session_id = str(kwargs.get("session_id") or "")
    if session_id:
        _DEDUP.clear_session(session_id)


def _on_pre_llm_call(**kwargs: Any) -> Optional[Dict[str, str]]:
    try:
        return _route_turn(kwargs)
    except Exception as exc:  # never break the host turn
        logger.warning("jev-skill-router: turn routing failed (%s); falling back", exc)
        return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

class _Outcome:
    """Accumulates the audit fields while the pipeline runs."""

    def __init__(self, cfg: policy.RouterConfig, mode: str) -> None:
        self.cfg = cfg
        self.request_id = policy.new_request_id()
        self.mode = mode
        self.question_version = questions_mod.QUESTION_VERSION
        self.catalog_revision = ""
        self.candidate_ids: List[str] = []
        self.usage: Optional[Mapping[str, Any]] = None
        # Every response's usage, in call order (the offline evaluation
        # runner reads this; "usage" above stays the latest for advice).
        self.usage_log: List[Mapping[str, Any]] = []
        self.started = time.monotonic()
        self.elapsed_ms = 0.0

    def tick(self) -> None:
        self.elapsed_ms = round((time.monotonic() - self.started) * 1000, 1)

    def tick_and_elapsed(self) -> float:
        self.tick()
        return self.elapsed_ms


def _route_turn(
    kwargs: Mapping[str, Any], *, client_factory: Any = None
) -> Optional[Dict[str, str]]:
    cfg, _config_errors = policy.load_router_config()
    home = _hermes_home()
    outcome = _Outcome(cfg, cfg.mode)

    def finish(status: str, reason_code: str, skill_id: str = "") -> Optional[Dict[str, str]]:
        return _finish(outcome, home, status, reason_code, skill_id)

    if not cfg.enabled:
        return None  # default off: no config reads beyond this, no records

    session_id = str(kwargs.get("session_id") or "")
    turn_id = str(kwargs.get("turn_id") or "")
    user_message = kwargs.get("user_message")
    profile = os.getenv("HERMES_PROFILE", "default")

    gate = policy.evaluate_turn_gate(
        cfg,
        session_id=session_id,
        turn_id=turn_id,
        user_message=user_message,
        parent_session_id=kwargs.get("parent_session_id"),
        platform_kwarg=str(kwargs.get("platform") or ""),
        profile=profile,
        dedup=_DEDUP,
    )
    if not gate.should_run:
        return finish("skipped", gate.reason_code)
    _DEDUP.mark(policy.TurnDedup.key(profile, session_id, turn_id))

    api_key = policy.api_key_from_env(cfg.api_key_env)
    if not api_key:
        # Explicitly enabled but the key is missing: diagnosable, not fatal
        # (plan §4). Skip every such turn, log once per process.
        logger.warning(
            "jev-skill-router: mode=%s but %s is not set; skipping turns",
            cfg.mode,
            cfg.api_key_env,
        )
        return finish("skipped", "missing_api_key")

    # Outbound scope is DECLARED, never defaulted: full-catalog bool or an
    # exact id list. If neither is set (only possible on a hand-built
    # config — config_from_mapping refuses to enable), the empty set keeps
    # every skill metadata local.
    compiled = catalog_mod.compile_catalog(
        None if cfg.outbound_catalog_full else (cfg.outbound_catalog or frozenset())
    )
    outcome.catalog_revision = _catalog_revision(compiled)
    outcome.candidate_ids = compiled.ids()
    if not compiled.complete:
        # An incomplete catalog can never back an abstain either (plan §3.2).
        return finish("unavailable", "catalog_incomplete")
    if not compiled.entries:
        return finish("skipped", "empty_catalog")
    if len(compiled.entries) + 1 > questions_mod.CHOICE_MAX_OPTIONS:
        return finish("skipped", "choice_limit_exceeded")

    request_text = str(user_message)
    if policy.mentions_explicit_skill(request_text, compiled.ids()):
        return finish("skipped", "explicit_skill_mention")

    if cfg.max_requests_per_turn < 2:
        # v1 has no verified single-phase decision path: without phase 2's
        # per-candidate re-check there is no suggestion we can defend, so
        # the turn is skipped BEFORE any spend, identically in both modes.
        return finish("skipped", "single_request_mode")

    try:
        return _run_two_phase(
            cfg, home, outcome, policy.BudgetLedger(home, cfg.daily_budget_usd),
            api_key, request_text, compiled, client_factory=client_factory,
        )
    finally:
        outcome.tick()


def _reserve_for_payload(
    cfg: policy.RouterConfig,
    ledger: policy.BudgetLedger,
    state: Mapping[str, Any],
    questions: Mapping[str, Any],
) -> Tuple[Optional[float], str]:
    """Reserve ONE request's budget slice before that request goes out.

    Returns ``(reservation_usd, "")`` on success; ``(None, reason)`` when
    the payload exceeds the outbound volume cap (``input_too_large``) or
    the daily cap cannot cover this request (``budget_exhausted``).
    """
    payload_text = _payload_text(state, questions)
    estimated_tokens = policy.conservative_token_estimate(payload_text)
    if cfg.max_input_tokens is not None and estimated_tokens > cfg.max_input_tokens:
        return None, "input_too_large"
    reservation_usd = estimated_tokens / 1_000_000.0 * cfg.price_input_per_mtok
    if not ledger.reserve(reservation_usd):
        return None, "budget_exhausted"
    return reservation_usd, ""


def _run_two_phase(
    cfg: policy.RouterConfig,
    home: Any,
    outcome: _Outcome,
    ledger: policy.BudgetLedger,
    api_key: str,
    request_text: str,
    compiled: catalog_mod.CompiledCatalog,
    client_factory: Any = None,
) -> Optional[Dict[str, str]]:
    def finish(status: str, reason_code: str, skill_id: str = "") -> Optional[Dict[str, str]]:
        return _finish(outcome, home, status, reason_code, skill_id)

    state = {"request": _STATE_TEMPLATE.format(request=request_text)}
    phase_one = questions_mod.build_phase_one(request_text, compiled.entries)

    # Each request is an independent reserve → ask → settle cycle: the
    # reservation covers ONLY the request about to be sent, and is settled
    # (or conservatively forfeited) as soon as that request resolves.
    reservation, reason = _reserve_for_payload(cfg, ledger, state, phase_one)
    if reservation is None:
        return finish("skipped", reason)
    # Client seam: production builds the real vendor client; the offline
    # evaluation runner injects a scripted responder here (same interface,
    # no network). Everything else — gating, budget, thresholds, records —
    # is the unmodified production path.
    api_client = (client_factory or _default_jev_client)(api_key, cfg)

    phase_one_ms = int(cfg.total_deadline_ms * _PHASE_ONE_DEADLINE_SHARE)
    try:
        response = api_client.ask(state, phase_one, phase_one_ms)
    except client_mod.JevError as exc:
        # Failed request: real usage is unknowable — the reservation stays
        # booked as the conservative charge (never released as $0).
        ledger.settle(reservation, None, cfg.price_input_per_mtok, cfg.price_output_per_mtok)
        return finish("unavailable", _error_reason(exc))
    outcome.usage = response.get("usage") if isinstance(response, dict) else None
    outcome.usage_log.append(outcome.usage)
    ledger.settle(reservation, outcome.usage, cfg.price_input_per_mtok, cfg.price_output_per_mtok)

    try:
        top, top_prob, any_match, probs = questions_mod.interpret_phase_one(
            response["answers"], compiled.ids()
        )
    except questions_mod.InvalidAnswersError as exc:
        return finish("unavailable", f"invalid_phase_one:{exc}")
    if any_match < cfg.min_any_match:
        return finish("abstain", "any_match_below_threshold")
    if top == questions_mod.NONE_OPTION:
        return finish("abstain", "phase1_none")
    if top_prob < cfg.min_choice_prob:
        return finish("abstain", "top_below_threshold")

    shortlist_ids = questions_mod.shortlist_from_probabilities(probs, cfg.shortlist_size)
    if not shortlist_ids:
        return finish("abstain", "empty_shortlist")

    outcome.tick()  # phase-2 timeout = deadline minus REAL phase-1 time
    remaining_ms = int(cfg.total_deadline_ms - outcome.elapsed_ms)
    shortlist = [
        (compiled.find(sid), _phase_two_detail(cfg, compiled.find(sid)))
        for sid in shortlist_ids
        if compiled.find(sid) is not None
    ]
    if not shortlist:
        return finish("abstain", "empty_shortlist")
    phase_two = questions_mod.build_phase_two(request_text, shortlist)
    # Phase 2 reserves its OWN slice before going out; when the daily cap
    # cannot cover it, the request is simply not sent and no suggestion
    # can exist without the phase-2 verification.
    reservation2, reason2 = _reserve_for_payload(cfg, ledger, state, phase_two)
    if reservation2 is None:
        return finish("skipped", f"phase2_{reason2}")
    try:
        response2 = api_client.ask(state, phase_two, remaining_ms)
    except client_mod.JevError as exc:
        ledger.settle(reservation2, None, cfg.price_input_per_mtok, cfg.price_output_per_mtok)
        return finish("unavailable", _error_reason(exc, "phase2"))
    outcome.usage = response2.get("usage") if isinstance(response2, dict) else None
    outcome.usage_log.append(outcome.usage)
    ledger.settle(reservation2, outcome.usage, cfg.price_input_per_mtok, cfg.price_output_per_mtok)

    try:
        winner, winner_prob, fits = questions_mod.interpret_phase_two(
            response2["answers"], [entry.skill_id for entry, _ in shortlist]
        )
    except questions_mod.InvalidAnswersError as exc:
        return finish("unavailable", f"invalid_phase_two:{exc}")
    if winner == questions_mod.NONE_OPTION:
        return finish("abstain", "phase2_none")
    if winner_prob < cfg.min_choice_prob:
        return finish("abstain", "winner_below_threshold")
    if fits.get(winner, 0.0) < cfg.min_fit:
        return finish("abstain", "fit_below_threshold")
    return _suggest(outcome, home, compiled, winner, "two_phase_confirmed")


def _suggest(
    outcome: _Outcome,
    home: Any,
    compiled: catalog_mod.CompiledCatalog,
    skill_id: str,
    reason_code: str,
) -> Optional[Dict[str, str]]:
    entry = compiled.find(skill_id)
    if entry is None or not catalog_mod.entry_still_available(entry):
        # Catalog went stale mid-turn (disabled/edited/deleted): never
        # inject a suggestion the host would refuse to load (plan §3.2).
        return _finish(outcome, home, "skipped", "stale_candidate", "")
    advice = {
        "schema_version": policy.ADVICE_SCHEMA_VERSION,
        "status": "suggested",
        "skill_id": skill_id,
        "reason_code": reason_code,
        "model": outcome.cfg.model,
        "question_version": outcome.question_version,
        "catalog_revision": outcome.catalog_revision,
        "elapsed_ms": outcome.tick_and_elapsed(),
    }
    try:
        policy.validate_advice_payload(advice, set(compiled.ids()))
    except policy.AdviceValidationError as exc:
        return _finish(outcome, home, "unavailable", f"invalid_advice:{exc}", "")
    if outcome.mode != policy.MODE_RECOMMEND:
        # Shadow: record only, inject nothing (plan §1).
        return _finish(outcome, home, "suggested", reason_code, skill_id)
    _finish(outcome, home, "suggested", reason_code, skill_id)
    return {"context": policy.render_advice_context(skill_id)}


def _finish(
    outcome: _Outcome,
    home: Any,
    status: str,
    reason_code: str,
    skill_id: str = "",
) -> Optional[Dict[str, str]]:
    outcome.tick()
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        "request_id": outcome.request_id,
        "profile": os.getenv("HERMES_PROFILE", "default"),
        "mode": outcome.mode,
        "model": outcome.cfg.model,
        "question_version": outcome.question_version,
        "catalog_revision": outcome.catalog_revision,
        "candidates": outcome.candidate_ids,
        "shortlist_or_skill": skill_id,
        "status": status,
        "reason_code": reason_code,
        "elapsed_ms": outcome.elapsed_ms,
        "usage": dict(outcome.usage) if outcome.usage else None,
        # Full per-call usage trail (no user text, plan §5): lets the
        # offline evaluation runner total tokens across both phases.
        "usage_all": [dict(u) if u else None for u in outcome.usage_log],
    }
    policy.append_decision_record(home, record)
    return None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _default_jev_client(api_key: str, cfg: policy.RouterConfig) -> Any:
    """Production client factory (the client seam's default)."""
    return client_mod.JevClient(api_key, cfg.model, base_url=cfg.base_url)


def _hermes_home():
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _payload_text(state: Mapping[str, Any], questions: Mapping[str, Any]) -> str:
    try:
        return json.dumps({"state": state, "questions": questions}, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def _catalog_revision(compiled: catalog_mod.CompiledCatalog) -> str:
    ids = "\n".join(compiled.ids())
    digest = hashlib.sha256(ids.encode("utf-8")).hexdigest()[:12]
    return f"n{len(compiled.entries)}-{digest}"


def _phase_two_detail(cfg: policy.RouterConfig, entry: Any) -> str:
    excerpt = ""
    if entry is not None and entry.skill_id in cfg.outbound_body_skills:
        excerpt = catalog_mod.body_excerpt(entry.skill_md, cfg.body_excerpt_chars)
    return entry.detail_text(excerpt) if entry is not None else ""


def _error_reason(exc: client_mod.JevError, phase: str = "phase1") -> str:
    kind = type(exc).__name__
    return f"{phase}_{kind}"
