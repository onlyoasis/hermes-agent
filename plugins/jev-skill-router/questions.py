"""Two-phase Choice/Noul question definitions for jev-skill-router.

Phase 1 (rank, one request):
  * ``which``     — Choice over every outbound candidate + a reserved
                    ``none`` option (the plan reserves one slot for it).
  * ``any_match`` — Noul asking whether the request actually needs one of
                    the provided skills at all, judged independently from
                    "which is closest".

Phase 2 (verify, one request, depends on phase-1 output):
  * ``which``     — Choice over the shortlist (fuller evidence text) +
                    ``none``; reduces near-name/near-function confusion.
  * ``fits::<id>``— one Noul per shortlisted candidate; ALL candidates may
                    come back unsupported (全部拒绝).

Interpretation rules (plan §3.3/§3.4):

* A response missing any expected question, naming an option that was
  never offered, or carrying out-of-range numbers is rejected WHOLE —
  never partially trusted (raises :class:`InvalidAnswersError`).
* A Choice answer must be a complete distribution over exactly the
  offered options (plus ``none``): every option present, values in
  [0, 1], mass summing to ~1, and ``choice`` being the (or a tied)
  argmax — otherwise the whole response is rejected.
* Choice probability and Noul values are separate, un-mixed signals;
  thresholds are applied by the caller per primitive, never averaged
  together.
* ``none`` is a first-class answer: a phase-2 ``none`` winner or a
  below-threshold fit yields "no suggestion", not an error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Iterable, List, Mapping, Sequence, Tuple

if TYPE_CHECKING:  # runtime keeps questions.py import-light
    from .catalog import CatalogEntry

# Bumped whenever question wording/semantics change — recorded in every
# advice record so evaluation runs stay attributable (plan §3.4).
QUESTION_VERSION = "skill-routing-v1"

# Reserved Choice option meaning "no skill applies". Costs one of the 255
# Choice slots (plan §3.2).
NONE_OPTION = "none"

# TypeSafe Choice hard limit.
CHOICE_MAX_OPTIONS = 255

# Question ids.
P1_WHICH = "which"
P1_ANY_MATCH = "any_match"
P2_FITS_PREFIX = "fits::"

# A Choice answer is only trustworthy as a WHOLE distribution: every
# offered option must appear, the mass must sum to ~1 (vendors round),
# and the reported ``choice`` must be the (or a tied) highest-probability
# option. This tolerance absorbs rounding, nothing more.
CHOICE_SUM_TOLERANCE = 0.05

_P1_WHICH_INSTRUCTIONS = (
    "以下是当前可加载的技能目录（id 与简要说明）。"
    "用户本轮请求最适合加载其中哪一个技能？"
    "若没有任何技能真正适合，选择 none。"
    "只依据说明判断，不要凭名称相似猜测。"
)
_P1_NONE_CRITERIA = "没有任何已列出的技能适合当前请求。"
_P1_ANY_MATCH_INSTRUCTIONS = (
    "用户本轮请求是否确实需要加载已提供目录中的某个技能才能完成"
    "（而不是仅靠通用知识解释或建议即可满足）？"
)
_P2_WHICH_INSTRUCTIONS = (
    "下面是前几名候选技能的更完整说明。阅读每个候选实际做什么之后，"
    "判断其中哪一个确实支持当前用户请求要做的操作。"
    "若全部都不适合，选择 none。依据实际功能而非名称相近。"
)
_P2_NONE_CRITERIA = "阅读详细说明后，没有候选确实支持当前请求。"
_P2_FITS_INSTRUCTIONS = (
    "候选技能 '{skill_id}' 的说明如下：\n{detail}\n"
    "它是否确实支持当前用户请求所要求的操作？所有候选都可以不支持。"
)


class InvalidAnswersError(ValueError):
    """Vendor response failed structural/semantic validation — reject whole."""


def build_phase_one(request: str, entries: Sequence[CatalogEntry]) -> Dict[str, dict]:
    """Build the phase-1 question set. ``request`` is the only outbound
    user data (input minimisation, plan §3.2)."""
    criteria: Dict[str, str] = {e.skill_id: e.description for e in entries}
    criteria[NONE_OPTION] = _P1_NONE_CRITERIA
    return {
        P1_WHICH: {
            "type": "choice",
            "instructions": _P1_WHICH_INSTRUCTIONS,
            "criteria": criteria,
        },
        P1_ANY_MATCH: {
            "type": "noul",
            "instructions": _P1_ANY_MATCH_INSTRUCTIONS,
        },
    }


def build_phase_two(
    request: str,
    shortlist: Sequence[Tuple[CatalogEntry, str]],
) -> Dict[str, dict]:
    """Build the phase-2 question set.

    ``shortlist`` pairs each candidate with its vendor-bound detail text
    (the CALLER enforces the outbound-body allowlist when building that
    text — see catalog.body_excerpt).
    """
    criteria: Dict[str, str] = {e.skill_id: detail for e, detail in shortlist}
    criteria[NONE_OPTION] = _P2_NONE_CRITERIA
    questions: Dict[str, dict] = {
        P1_WHICH: {
            "type": "choice",
            "instructions": _P2_WHICH_INSTRUCTIONS,
            "criteria": criteria,
        },
    }
    for entry, detail in shortlist:
        questions[f"{P2_FITS_PREFIX}{entry.skill_id}"] = {
            "type": "noul",
            "instructions": _P2_FITS_INSTRUCTIONS.format(
                skill_id=entry.skill_id, detail=detail
            ),
        }
    return questions


# ---------------------------------------------------------------------------
# Answer interpretation — all-or-nothing validation
# ---------------------------------------------------------------------------


def _require_answer(answers: Mapping[str, dict], qid: str) -> dict:
    answer = answers.get(qid)
    if not isinstance(answer, dict):
        raise InvalidAnswersError(f"missing answer for question {qid!r}")
    return answer


def _unit_interval(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidAnswersError(f"{label}: not a number ({value!r})")
    numeric = float(value)
    if not 0.0 <= numeric <= 1.0:
        raise InvalidAnswersError(f"{label}: out of range ({numeric})")
    return numeric


def _choice_distribution(
    answer: Mapping[str, object],
    qid: str,
    offered: Iterable[str],
) -> Tuple[str, Dict[str, float], float]:
    """Validate a Choice answer as a COMPLETE distribution over *offered*.

    Returns ``(chosen_option, probabilities, chosen_probability)``.
    Rejects the whole answer when the probability set doesn't cover every
    offered option (plus ``none``) exactly, a value falls outside
    [0, 1], the mass doesn't sum to ~1, or the chosen option isn't the
    (or a tied) highest-probability option.
    """
    if answer.get("type") != "choice":
        raise InvalidAnswersError(f"{qid}: expected choice answer, got {answer.get('type')!r}")
    option = answer.get("choice")
    if not isinstance(option, str) or not option:
        raise InvalidAnswersError(f"{qid}: missing choice value")
    expected = set(offered) | {NONE_OPTION}
    probs_raw = answer.get("probabilities")
    if not isinstance(probs_raw, Mapping):
        raise InvalidAnswersError(f"{qid}: missing probabilities map")
    probs: Dict[str, float] = {}
    for key, value in probs_raw.items():
        if key not in expected:
            raise InvalidAnswersError(f"{qid}: probability for unknown option {key!r}")
        probs[str(key)] = _unit_interval(value, f"{qid}.probabilities[{key!r}]")
    missing = expected - set(probs)
    if missing:
        raise InvalidAnswersError(
            f"{qid}: missing probabilities for options {sorted(missing)}"
        )
    total = sum(probs.values())
    if abs(total - 1.0) > CHOICE_SUM_TOLERANCE:
        raise InvalidAnswersError(f"{qid}: probabilities sum to {total:.3f}, not ~1")
    if option not in expected:
        raise InvalidAnswersError(f"{qid}: unknown option {option!r}")
    if probs[option] < max(probs.values()) - 1e-9:
        raise InvalidAnswersError(
            f"{qid}: chosen option {option!r} is not the top probability"
        )
    return option, probs, probs[option]


def _noul_value(answers: Mapping[str, dict], qid: str) -> float:
    answer = _require_answer(answers, qid)
    if answer.get("type") != "noul":
        raise InvalidAnswersError(f"{qid}: expected noul answer, got {answer.get('type')!r}")
    return _unit_interval(answer.get("noul"), f"{qid}.noul")


def interpret_phase_one(
    answers: Mapping[str, dict],
    offered_ids: Sequence[str],
) -> Tuple[str, float, float, Dict[str, float]]:
    """Validate & interpret phase-1 answers.

    Returns ``(top_option, top_probability, any_match_noul,
    all_probabilities)``. ``top_option`` may be ``NONE_OPTION``. Raises
    :class:`InvalidAnswersError` on any structural violation — including
    an incomplete distribution, mass that doesn't sum to ~1, or a chosen
    option that isn't the argmax.
    """
    which = _require_answer(answers, P1_WHICH)
    top, probs, top_prob = _choice_distribution(which, P1_WHICH, offered_ids)
    any_match = _noul_value(answers, P1_ANY_MATCH)
    return top, top_prob, any_match, probs


def shortlist_from_probabilities(
    probabilities: Mapping[str, float],
    size: int,
) -> List[str]:
    """Rank offered ids (never ``none``) by probability, keep top *size*."""
    ranked = sorted(
        ((sid, p) for sid, p in probabilities.items() if sid != NONE_OPTION),
        key=lambda kv: (-kv[1], kv[0]),
    )
    return [sid for sid, _ in ranked[: max(0, size)]]


def interpret_phase_two(
    answers: Mapping[str, dict],
    shortlist_ids: Sequence[str],
) -> Tuple[str, float, Dict[str, float]]:
    """Validate & interpret phase-2 answers.

    Returns ``(winner_option, winner_probability, fits_nouls_by_id)``.
    ``winner_option`` may be ``NONE_OPTION``. The Choice answer must be a
    complete, ~1-summing distribution whose winner is the argmax; every
    ``fits::<id>`` question must be present — any violation rejects the
    whole response.
    """
    which = _require_answer(answers, P1_WHICH)
    winner, _, winner_prob = _choice_distribution(which, P1_WHICH, shortlist_ids)
    fits: Dict[str, float] = {}
    for sid in shortlist_ids:
        fits[sid] = _noul_value(answers, f"{P2_FITS_PREFIX}{sid}")
    return winner, winner_prob, fits
