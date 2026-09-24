"""Policy layer for jev-skill-router: config, turn gating, budget, advice.

Everything deterministic lives here so the hook orchestrator
(:mod:`plugins jev-skill-router/__init__.py`) stays a straight pipeline:

* :class:`RouterConfig` — reads ``plugins.entries.jev-skill-router`` from
  config.yaml (mtime-cached ``load_config_readonly``), validating every
  field; anything malformed degrades to mode=off with a diagnosable log
  line instead of guessing.
* :func:`evaluate_turn_gate` — the deterministic skip checks (plan §3.1):
  mode, surface allowlist, cron/internal/delegated exclusion, id
  presence, per-turn dedup. No text-based identity guessing.
* :class:`BudgetLedger` — per-profile daily USD ledger: conservative
  reserve before each send, settle-by-usage after, refusing new calls
  once the daily cap is reached (plan §4).
* :func:`render_advice_context` — the ONLY text that may reach the main
  agent: a fixed template with a whitelisted skill id interpolated; no
  vendor free text, no skill bodies (plan §3.4).
* :func:`append_decision_record` — append-only audit line; never user
  text, skill bodies, or credentials (plan §5).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Set, Tuple

try:  # POSIX advisory locks; same cross-platform idiom as gateway/status.py
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt  # type: ignore[import-not-found]
except ImportError:
    msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

PLUGIN_CONFIG_KEY = "jev-skill-router"

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_RECOMMEND = "recommend"
VALID_MODES = (MODE_OFF, MODE_SHADOW, MODE_RECOMMEND)

ADVICE_SCHEMA_VERSION = "jev-skill-advice-v1"
ADVICE_STATUSES = ("suggested", "abstain", "skipped", "unavailable")

# Surfaces that are never user-interactive turns, regardless of config.
# kanban: dispatcher-spawned workers; tool: third-party integrations that
# self-tag HERMES_SESSION_SOURCE=tool; cron: scheduled jobs.
NON_INTERACTIVE_SOURCES = frozenset({"kanban", "tool", "cron", "scheduler"})

# Fixed advice template (plan §3.4 example). Only ``{skill_id}`` is
# interpolated — from the verified, whitelisted candidate set.
ADVICE_TEMPLATE = (
    "本轮可优先检查技能 {skill_id}；这是相关性建议，仍须核对用户意图与技能说明。"
)

# Pricing (plan §4): input USD per million tokens, output currently free.
# Config-overridable because prices are variables, not constants.
DEFAULT_PRICE_INPUT_PER_MTOk = 0.042
DEFAULT_PRICE_OUTPUT_PER_MTOk = 0.0

# Fixed protocol margin (tokens) added to the byte-based pre-send
# estimate: covers transport framing the serialized request body doesn't
# include. Byte count itself is already a hard token upper bound (see
# conservative_token_estimate), so no extra language-dependent factor.
PROTOCOL_OVERHEAD_TOKENS = 1024

# Decision-log retention: keep the jsonl bounded; rotate per day already,
# and cap in-memory dedup state separately.
DEDUP_MAX_ENTRIES = 512


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RouterConfig:
    """Validated plugin configuration. Defaults = plan §4 initial values."""

    mode: str = MODE_OFF
    model: str = "jev-1.13.0"
    api_key_env: str = "TYPESAFE_API_KEY"
    base_url: str = "https://api.typesafe.ai"
    shortlist_size: int = 3
    total_deadline_ms: int = 2000
    max_requests_per_turn: int = 2
    # None => not configured => plugin stays disabled (plan §4: 上线前填写，
    # 缺失则不启用). Enabling REQUIRES both this and a finite
    # daily_budget_usd — shadow/recommend both send data and cost money.
    max_input_tokens: Optional[int] = None
    daily_budget_usd: Optional[float] = None
    # Empty => no surface is allowed (phase 1: explicit gateway platforms
    # only; Cron/internal always excluded).
    allowed_platforms: frozenset = frozenset()
    # Outbound metadata scope must be DECLARED, never defaulted: either an
    # exact id list (outbound_catalog) or the explicit full-catalog bool
    # (outbound_catalog_full). Neither => not enabled; both => config
    # error. Body excerpts stay on their own allowlist below.
    outbound_catalog: Optional[frozenset] = None
    outbound_catalog_full: bool = False
    # Skill ids whose BODY excerpts may be sent; empty => never.
    outbound_body_skills: frozenset = frozenset()
    body_excerpt_chars: int = 600
    # Per-primitive thresholds (un-calibrated local initial values; frozen
    # for real use only after the D2 evaluation pass, plan §6).
    min_any_match: float = 0.5
    min_fit: float = 0.5
    min_choice_prob: float = 0.5
    price_input_per_mtok: float = DEFAULT_PRICE_INPUT_PER_MTOk
    price_output_per_mtok: float = DEFAULT_PRICE_OUTPUT_PER_MTOk

    @property
    def enabled(self) -> bool:
        return self.mode in (MODE_SHADOW, MODE_RECOMMEND)


def _as_str(value: Any, default: str, field_name: str, errors: List[str]) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field_name}: expected non-empty string")
        return default
    return value.strip()


def _as_int(value: Any, default: Optional[int], field_name: str, errors: List[str],
            minimum: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{field_name}: expected integer")
        return default
    if minimum is not None and value < minimum:
        errors.append(f"{field_name}: must be >= {minimum}")
        return default
    return value


def _as_float(value: Any, default: float, field_name: str, errors: List[str],
              minimum: float = 0.0, maximum: float = 1.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{field_name}: expected number")
        return default
    numeric = float(value)
    if not minimum <= numeric <= maximum:
        errors.append(f"{field_name}: must be within [{minimum}, {maximum}]")
        return default
    return numeric


def _as_id_set(value: Any, field_name: str, errors: List[str]) -> Optional[frozenset]:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"{field_name}: expected list of skill ids")
        return None
    return frozenset(v.strip() for v in value if v.strip())


def config_from_mapping(raw: Mapping[str, Any]) -> Tuple[RouterConfig, List[str]]:
    """Build a :class:`RouterConfig` from a plugin config mapping.

    Returns ``(config, errors)``. Invalid fields fall back to safe
    defaults and are reported; an invalid ``mode`` degrades to ``off``.
    Enabling (shadow/recommend) additionally REQUIRES explicit finite
    ``max_input_tokens`` + ``daily_budget_usd`` and a DECLARED outbound
    catalog scope (id list or the full-catalog bool); anything missing,
    non-finite, or conflicting stays OFF (plan §4: 缺失则不启用).
    """
    errors: List[str] = []
    mode_raw = raw.get("mode", MODE_OFF)
    mode = mode_raw if isinstance(mode_raw, str) and mode_raw in VALID_MODES else None
    if mode is None:
        errors.append(f"mode: expected one of {', '.join(VALID_MODES)} (got {mode_raw!r})")
        mode = MODE_OFF

    daily_budget = raw.get("daily_budget_usd")
    if daily_budget is not None:
        if isinstance(daily_budget, bool) or not isinstance(daily_budget, (int, float)) \
                or not math.isfinite(float(daily_budget)) or float(daily_budget) <= 0:
            errors.append("daily_budget_usd: expected positive finite number")
            daily_budget = None
        else:
            daily_budget = float(daily_budget)

    max_input_raw = raw.get("max_input_tokens")
    if max_input_raw is None:
        max_input = None
    else:
        max_input = _as_int(max_input_raw, None, "max_input_tokens", errors, minimum=1)

    full_raw = raw.get("outbound_catalog_full")
    outbound_full = False
    if full_raw is not None:
        if isinstance(full_raw, bool):
            outbound_full = full_raw
        else:
            errors.append("outbound_catalog_full: expected boolean")
    outbound_ids = _as_id_set(raw.get("outbound_catalog"), "outbound_catalog", errors)

    if mode in (MODE_SHADOW, MODE_RECOMMEND):
        # Enabling must be an explicit, fully-numbered act (plan §4 缺失则
        # 不启用): shadow sends data and costs money exactly like
        # recommend. Same for the outbound scope: undeclared means nothing
        # may leave, so the plugin stays off until one is chosen.
        missing: List[str] = []
        if max_input is None:
            missing.append("max_input_tokens")
        if daily_budget is None:
            missing.append("daily_budget_usd")
        if outbound_ids is None and not outbound_full:
            missing.append("outbound_catalog (id list) or outbound_catalog_full")
        if missing:
            errors.append(
                f"mode {mode}: missing required config: {', '.join(missing)}; staying off"
            )
            mode = MODE_OFF
        if outbound_ids is not None and outbound_full:
            errors.append(
                "outbound_catalog_full: conflicts with outbound_catalog; pick one"
            )
            mode = MODE_OFF

    cfg = RouterConfig(
        mode=mode,
        model=_as_str(raw.get("model"), "jev-1.13.0", "model", errors),
        api_key_env=_as_str(raw.get("api_key_env"), "TYPESAFE_API_KEY", "api_key_env", errors),
        base_url=_as_str(raw.get("base_url"), "https://api.typesafe.ai", "base_url", errors),
        shortlist_size=_as_int(raw.get("shortlist_size"), 3, "shortlist_size", errors, minimum=1),
        total_deadline_ms=_as_int(
            raw.get("total_deadline_ms"), 2000, "total_deadline_ms", errors, minimum=1
        ),
        max_requests_per_turn=_as_int(
            raw.get("max_requests_per_turn"), 2, "max_requests_per_turn", errors, minimum=1
        ),
        max_input_tokens=max_input,
        daily_budget_usd=daily_budget,
        allowed_platforms=_platform_set(raw.get("allowed_platforms"), errors),
        outbound_catalog=outbound_ids,
        outbound_catalog_full=outbound_full,
        outbound_body_skills=_as_id_set(
            raw.get("outbound_body_skills"), "outbound_body_skills", errors
        ) or frozenset(),
        body_excerpt_chars=_as_int(
            raw.get("body_excerpt_chars"), 600, "body_excerpt_chars", errors, minimum=0
        ),
        min_any_match=_as_float(raw.get("min_any_match"), 0.5, "min_any_match", errors),
        min_fit=_as_float(raw.get("min_fit"), 0.5, "min_fit", errors),
        min_choice_prob=_as_float(raw.get("min_choice_prob"), 0.5, "min_choice_prob", errors),
        price_input_per_mtok=_as_float(
            raw.get("price_input_per_mtok"),
            DEFAULT_PRICE_INPUT_PER_MTOk,
            "price_input_per_mtok",
            errors,
            minimum=0.0,
            maximum=1000.0,
        ),
        price_output_per_mtok=_as_float(
            raw.get("price_output_per_mtok"),
            DEFAULT_PRICE_OUTPUT_PER_MTOk,
            "price_output_per_mtok",
            errors,
            minimum=0.0,
            maximum=1000.0,
        ),
    )
    return cfg, errors


def _platform_set(value: Any, errors: List[str]) -> frozenset:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append("allowed_platforms: expected list of platform names")
        return frozenset()
    return frozenset(v.strip().lower() for v in value if v.strip())


def load_router_config() -> Tuple[RouterConfig, List[str]]:
    """Read the plugin config from ``plugins.entries.jev-skill-router``."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception as exc:
        logger.warning("jev-skill-router: config load failed (%s); staying off", exc)
        return RouterConfig(), [f"config load failed: {exc}"]
    plugins_cfg = config.get("plugins")
    if not isinstance(plugins_cfg, dict):
        return RouterConfig(), []
    entries = plugins_cfg.get("entries")
    if not isinstance(entries, dict):
        return RouterConfig(), []
    raw = entries.get(PLUGIN_CONFIG_KEY)
    if not isinstance(raw, Mapping):
        return RouterConfig(), []
    cfg, errors = config_from_mapping(raw)
    if errors:
        logger.warning(
            "jev-skill-router: invalid config (%s); affected fields use safe defaults",
            "; ".join(errors),
        )
    return cfg, errors


# ---------------------------------------------------------------------------
# Surface resolution & turn gate
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SurfaceInfo:
    """Resolved origin of the current turn (host-provided signals only)."""

    platform: str = ""
    source: str = ""
    cron_session: bool = False


def resolve_surface(platform_kwarg: str = "") -> SurfaceInfo:
    """Resolve the turn's surface from gateway session context.

    Uses only host-written markers (gateway ``set_session_vars``, cron /
    kanban / integration env tags) — never message text. Unresolvable
    means empty strings, and an empty platform is never allow-listed, so
    the safe default is skip.
    """
    try:
        from gateway.session_context import get_session_env

        platform = str(get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip().lower()
        source = str(get_session_env("HERMES_SESSION_SOURCE", "") or "").strip().lower()
        cron = str(get_session_env("HERMES_CRON_SESSION", "") or "").strip()
    except Exception:
        platform = str(os.getenv("HERMES_SESSION_PLATFORM", "") or "").strip().lower()
        source = str(os.getenv("HERMES_SESSION_SOURCE", "") or "").strip().lower()
        cron = str(os.getenv("HERMES_CRON_SESSION", "") or "").strip()
    if not platform:
        platform = str(platform_kwarg or "").strip().lower()
    return SurfaceInfo(platform=platform, source=source, cron_session=cron in {"1", "true", "yes"})


@dataclass(frozen=True)
class GateDecision:
    should_run: bool
    reason_code: str
    detail: str = ""


class TurnDedup:
    """Bounded per-(profile, session, turn) dedup (plan §3.1).

    One recommendation attempt per turn; state cleaned on session end and
    capped so a long-lived gateway process can't grow it unbounded.
    """

    def __init__(self) -> None:
        self._seen: Dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(profile: str, session_id: str, turn_id: str) -> str:
        return f"{profile}\x1f{session_id}\x1f{turn_id}"

    def seen(self, key: str) -> bool:
        with self._lock:
            return key in self._seen

    def mark(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._seen[key] = now
            if len(self._seen) > DEDUP_MAX_ENTRIES:
                oldest = sorted(self._seen.items(), key=lambda kv: kv[1])
                for stale_key, _ in oldest[: len(self._seen) - DEDUP_MAX_ENTRIES]:
                    self._seen.pop(stale_key, None)

    def clear_session(self, session_id: str) -> None:
        suffix = f"\x1f{session_id}\x1f"
        with self._lock:
            for key in [k for k in self._seen if suffix in k]:
                self._seen.pop(key, None)


def evaluate_turn_gate(
    cfg: RouterConfig,
    *,
    session_id: str,
    turn_id: str,
    user_message: Any,
    parent_session_id: Any,
    platform_kwarg: str = "",
    profile: str = "default",
    dedup: Optional[TurnDedup] = None,
    surface: Optional[SurfaceInfo] = None,
) -> GateDecision:
    """Deterministic pre-flight checks. Order cheapest/most-final first."""
    if not cfg.enabled:
        return GateDecision(False, "mode_off")
    if not session_id or not turn_id:
        return GateDecision(False, "missing_ids")
    if not isinstance(user_message, str) or not user_message.strip():
        return GateDecision(False, "non_text_message")
    if parent_session_id:
        # Delegated/subagent turns are not user turns.
        return GateDecision(False, "delegated_turn")
    info = surface or resolve_surface(platform_kwarg)
    if info.cron_session or info.source in NON_INTERACTIVE_SOURCES:
        return GateDecision(False, "non_interactive_surface", info.source or "cron")
    if not info.platform or info.platform not in cfg.allowed_platforms:
        return GateDecision(False, "platform_not_allowed", info.platform)
    if dedup is not None and dedup.seen(TurnDedup.key(profile, session_id, turn_id)):
        return GateDecision(False, "duplicate_turn")
    return GateDecision(True, "gate_passed")


def mentions_explicit_skill(request: str, offered_ids: List[str]) -> bool:
    """True when the user text explicitly names an offered skill.

    Word-boundary match against the candidate ids only — never a reason
    to run the model. A user-named skill always goes through the existing
    explicit-load path; the router must not second-guess it (plan §1).
    """
    for skill_id in offered_ids:
        if not skill_id:
            continue
        pattern = r"(?<![\w-])" + re.escape(skill_id) + r"(?![\w-])"
        if re.search(pattern, request):
            return True
    return False


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

def conservative_token_estimate(*payload_texts: str) -> int:
    """Hard pre-send token upper bound from the FULL request body's bytes.

    Byte-level BPE tokenisers (the family Jev/TypeSafe models belong to)
    never emit more than one token per input byte, so the UTF-8 length of
    the serialized request is a language-independent hard bound — unlike
    chars-per-word heuristics, which can undershoot real tokenisation on
    code, random ids, or rare scripts and let the daily reservation fall
    below actual cost. A fixed protocol margin covers transport framing
    the body JSON doesn't include (plan §4: 发送前保守预估并预留).
    """
    total_bytes = 0
    for text in payload_texts:
        total_bytes += len(text.encode("utf-8", "surrogatepass"))
    return total_bytes + PROTOCOL_OVERHEAD_TOKENS


class BudgetLedgerError(ValueError):
    """The day ledger exists but cannot be trusted (corrupt / malformed)."""


def _usage_valid(usage: Any) -> bool:
    """True only for a usage mapping with sane non-negative numbers."""
    if not isinstance(usage, Mapping):
        return False
    for field in ("input_tokens", "output_tokens"):
        value = usage.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return False
    return True


class BudgetLedger:
    """Per-profile daily USD ledger with reserve/settle accounting.

    ``reserve`` books a conservative estimate BEFORE the request so
    concurrent turns see the spend; ``settle`` replaces the reservation
    with actual usage. Once ``spent + reserved`` reaches the daily cap,
    ``reserve`` refuses new calls until the day rolls over (plan §4:
    预算耗尽后停止新调用).

    Atomicity: every read-modify-write runs under an OS-level advisory
    lock on ``$HERMES_HOME/jev-skill-router/usage/.ledger.lock`` — the
    lock is the FILE, shared by every process and thread touching this
    profile's day, so concurrent turns (each building their own ledger
    instance) still serialise. Fail CLOSED: a corrupt or unwritable
    ledger refuses new spend rather than guessing zeros. ``settle``
    never releases a reservation as $0 — missing or invalid usage
    charges the reservation itself (conservative until real cost is
    provable), and a failed ledger update leaves the reservation booked.
    """

    def __init__(self, home: Path, daily_budget_usd: Optional[float]) -> None:
        self.home = Path(home)
        self.daily_budget_usd = daily_budget_usd

    def _usage_dir(self) -> Path:
        return self.home / "jev-skill-router" / "usage"

    def _day_file(self) -> Path:
        return self._usage_dir() / f"{time.strftime('%Y-%m-%d')}.json"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self._usage_dir() / ".ledger.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+b")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:  # msvcrt locks need a byte to hold
                    handle.write(b"\n")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                raise OSError("no advisory file-lock primitive available")
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                handle.close()

    def _read(self) -> Dict[str, float]:
        """Load the day state. Missing file = fresh day; anything else
        unreadable raises :class:`BudgetLedgerError` (fail closed)."""
        path = self._day_file()
        if not path.exists():
            return {"reserved_usd": 0.0, "spent_usd": 0.0, "requests": 0.0}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BudgetLedgerError(f"day ledger unreadable: {exc}") from exc
        if not isinstance(data, dict):
            raise BudgetLedgerError("day ledger is not a JSON object")
        state: Dict[str, float] = {}
        for field in ("reserved_usd", "spent_usd", "requests"):
            value = data.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise BudgetLedgerError(f"day ledger field {field!r} invalid: {value!r}")
            state[field] = float(value)
        return state

    def _write(self, state: Mapping[str, float]) -> None:
        # Round to 12 decimals — far below any real budget granularity —
        # so a reserve/settle pair cancels to exactly 0.0 instead of
        # leaving binary floating-point dust on the persisted ledger.
        clean = {field: round(float(state[field]), 12) for field in state}
        path = self._day_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean), encoding="utf-8")
        tmp.replace(path)

    def snapshot(self) -> Dict[str, float]:
        """Best-effort read for display/tests; zeros on unreadable state."""
        try:
            with self._locked():
                return self._read()
        except (OSError, ValueError):
            return {"reserved_usd": 0.0, "spent_usd": 0.0, "requests": 0.0}

    def reserve(self, estimate_usd: float) -> bool:
        """Atomically book *estimate_usd* ahead of ONE request.

        False when the daily cap can't cover it, or the ledger is
        corrupt/unwritable — in every unreadable state we refuse new
        spend instead of guessing (fail closed).
        """
        try:
            with self._locked():
                state = self._read()
                if (
                    self.daily_budget_usd is not None
                    and state["spent_usd"] + state["reserved_usd"] + estimate_usd
                    > self.daily_budget_usd
                ):
                    return False
                state["reserved_usd"] += estimate_usd
                self._write(state)
                return True
        except (OSError, ValueError) as exc:
            logger.warning(
                "jev-skill-router: budget ledger unavailable (%s); failing closed", exc
            )
            return False

    def settle(
        self,
        reservation_usd: float,
        usage: Optional[Mapping[str, Any]],
        price_input_per_mtok: float,
        price_output_per_mtok: float,
    ) -> float:
        """Replace one reservation with the actual cost of its request.

        Missing or invalid usage NEVER settles to $0 — the reservation is
        the conservative charge until real usage is provable. A failed
        ledger update keeps the reservation booked on disk: under-counting
        spend is the one direction this ledger refuses to go.
        """
        if _usage_valid(usage):
            actual = usage_cost_usd(usage, price_input_per_mtok, price_output_per_mtok)
        else:
            actual = reservation_usd
        try:
            with self._locked():
                state = self._read()
                state["reserved_usd"] = max(0.0, state["reserved_usd"] - reservation_usd)
                state["spent_usd"] += actual
                state["requests"] += 1
                self._write(state)
        except (OSError, ValueError) as exc:
            logger.warning(
                "jev-skill-router: budget settle failed; reservation stays booked (%s)", exc
            )
        return actual


def usage_cost_usd(
    usage: Optional[Mapping[str, Any]],
    price_input_per_mtok: float,
    price_output_per_mtok: float,
) -> float:
    """Cost of one response's usage at configured prices (USD)."""
    if not isinstance(usage, Mapping):
        return 0.0
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")

    def _count(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return 0.0
        return float(value)

    return (
        _count(input_tokens) / 1_000_000.0 * price_input_per_mtok
        + _count(output_tokens) / 1_000_000.0 * price_output_per_mtok
    )


# ---------------------------------------------------------------------------
# Advice contract & records
# ---------------------------------------------------------------------------

def render_advice_context(skill_id: str) -> str:
    """Fixed-template context line; only a whitelisted id is interpolated."""
    return ADVICE_TEMPLATE.format(skill_id=skill_id)


class AdviceValidationError(ValueError):
    """The assembled advice violates the internal contract — drop it."""


def validate_advice_payload(payload: Mapping[str, Any], allowed_skill_ids: Set[str]) -> None:
    """Whole-payload validation of the internal advice record (plan §3.4).

    Only ``suggested`` may carry a ``skill_id``; the id must be in the
    verified candidate set; statuses/reasons must be the known enums;
    elapsed/usage must be sane numbers.
    """
    if payload.get("schema_version") != ADVICE_SCHEMA_VERSION:
        raise AdviceValidationError("wrong schema_version")
    status = payload.get("status")
    if status not in ADVICE_STATUSES:
        raise AdviceValidationError(f"unknown status {status!r}")
    skill_id = payload.get("skill_id")
    if status == "suggested":
        if not isinstance(skill_id, str) or not skill_id:
            raise AdviceValidationError("suggested without skill_id")
        if skill_id not in allowed_skill_ids:
            raise AdviceValidationError(f"skill_id {skill_id!r} not in verified candidate set")
    elif skill_id:
        raise AdviceValidationError(f"status {status!r} must not carry skill_id")
    elapsed = payload.get("elapsed_ms")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or elapsed < 0:
        raise AdviceValidationError("elapsed_ms missing or negative")
    reason = payload.get("reason_code")
    if not isinstance(reason, str) or not reason:
        raise AdviceValidationError("reason_code missing")


def append_decision_record(home: Path, record: Mapping[str, Any]) -> None:
    """Append one audit line to ``<home>/jev-skill-router/decisions.jsonl``.

    Best-effort: an audit failure must never break the turn. Records
    carry no user text, no skill bodies, no credentials (plan §5).
    """
    try:
        path = Path(home) / "jev-skill-router" / "decisions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(record), ensure_ascii=False)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        logger.debug("jev-skill-router: decision record append failed: %s", exc)


def new_request_id() -> str:
    return uuid.uuid4().hex


def api_key_from_env(api_key_env: str) -> str:
    """Read the API key from the profile credential layer (env). Never logged."""
    return (os.getenv(api_key_env) or "").strip()
