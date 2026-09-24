"""Thin TypeSafe (Jev) HTTP adapter for jev-skill-router.

One request shape only: ``POST {base_url}/v1/systemone`` with a Bearer
key, a ``state`` payload, a ``model`` and a ``questions`` map. Answers
come back keyed by the question ids we chose.

Deliberately boring:

* NO automatic retries — the online path decides fallback, not the
  client (plan §4: “在线路径不做自动重试”).
* NO SDK dependency — reuses the pinned ``requests`` package the
  image_gen plugins already lazily import.
* A ``transport`` seam so offline tests inject canned responses and
  prove error mapping without any network access.
* Structural response validation here; semantic validation (offered
  option ids, expected question set) lives in :mod:`.questions` /
  :mod:`.policy`.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.typesafe.ai"
SYSTEMONE_PATH = "/v1/systemone"

# Transport contract: (url, headers, payload, timeout_seconds) ->
# (status_code, parsed_json_or_None, raw_body_text). Raising is allowed;
# ask() maps transport exceptions onto the error classes below.
Transport = Callable[[str, Dict[str, str], Mapping[str, Any], float], Tuple[int, Optional[Any], str]]


class JevError(Exception):
    """Base class for every failure the router treats as 'fall back'."""


class JevTransportError(JevError):
    """Network-level failure (DNS, connection reset, ...)."""


class JevTimeoutError(JevError):
    """Request exceeded its timeout slice of the turn deadline."""


class JevAuthError(JevError):
    """401/403 — the configured API key is missing or invalid."""


class JevRateLimited(JevError):
    """429 — retrying later is allowed, but not inline (bounded backoff
    happens on later turns, driven by policy, never here)."""


class JevOverloaded(JevError):
    """529 — service temporarily overloaded."""


class JevBadRequest(JevError):
    """422 — request body failed vendor validation (our bug or drift)."""


class JevInvalidResponseError(JevError):
    """200 whose body failed structural validation — reject whole."""


def default_transport(
    url: str,
    headers: Dict[str, str],
    payload: Mapping[str, Any],
    timeout_s: float,
) -> Tuple[int, Optional[Any], str]:
    """Real HTTP transport over the pinned ``requests`` dependency."""
    import requests

    response = requests.post(url, json=dict(payload), headers=headers, timeout=timeout_s)
    text = response.text
    try:
        parsed = response.json()
    except ValueError:
        parsed = None
    return response.status_code, parsed, text


def _validate_response(payload: Any) -> Dict[str, Any]:
    """Structural validation of a 200 body. Raises on any violation —
    a partially-valid response is never returned to the caller."""
    if not isinstance(payload, dict):
        raise JevInvalidResponseError("response body is not a JSON object")
    answers = payload.get("answers")
    if not isinstance(answers, dict) or not answers:
        raise JevInvalidResponseError("response has no answers map")
    for qid, answer in answers.items():
        if not isinstance(qid, str) or not isinstance(answer, dict):
            raise JevInvalidResponseError(f"malformed answer entry {qid!r}")
        answer_type = answer.get("type")
        if answer_type == "noul":
            value = answer.get("noul")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise JevInvalidResponseError(f"{qid}: noul value missing/not numeric")
            if not 0.0 <= float(value) <= 1.0:
                raise JevInvalidResponseError(f"{qid}: noul value out of range")
        elif answer_type == "choice":
            option = answer.get("choice")
            if not isinstance(option, str) or not option:
                raise JevInvalidResponseError(f"{qid}: choice value missing")
            probs = answer.get("probabilities")
            if not isinstance(probs, dict) or not probs:
                raise JevInvalidResponseError(f"{qid}: probabilities map missing")
            for key, value in probs.items():
                if not isinstance(key, str):
                    raise JevInvalidResponseError(f"{qid}: non-string probability key")
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise JevInvalidResponseError(f"{qid}: non-numeric probability")
                if not 0.0 <= float(value) <= 1.0:
                    raise JevInvalidResponseError(f"{qid}: probability out of range")
        else:
            raise JevInvalidResponseError(f"{qid}: unknown answer type {answer_type!r}")
    usage = payload.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            raise JevInvalidResponseError("usage is not an object")
        for field in ("input_tokens", "output_tokens"):
            value = usage.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise JevInvalidResponseError(f"usage.{field} missing or negative")
    return payload


class JevClient:
    """One-method client: :meth:`ask` performs a single POST, no retry."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        transport: Optional[Transport] = None,
    ) -> None:
        if not api_key:
            raise ValueError("JevClient requires a non-empty API key")
        if not model:
            raise ValueError("JevClient requires a model id")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._transport: Transport = transport or default_transport

    def ask(
        self,
        state: Mapping[str, Any],
        questions: Mapping[str, Any],
        timeout_ms: int,
    ) -> Dict[str, Any]:
        """Send one ``systemone`` request and return the validated body.

        ``timeout_ms`` is the caller-granted slice of the turn deadline —
        the client never extends it and never retries inside this call.
        """
        if timeout_ms <= 0:
            raise JevTimeoutError("no time left inside the turn deadline")
        url = f"{self.base_url}{SYSTEMONE_PATH}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "state": dict(state),
            "model": self.model,
            "questions": dict(questions),
        }
        try:
            status, parsed, text = self._transport(
                url, headers, payload, timeout_ms / 1000.0
            )
        except JevError:
            raise
        except TimeoutError:
            raise JevTimeoutError("request timed out") from None
        except Exception as exc:
            # requests.Timeout subclasses OSError; normalise both classes
            # without importing requests here.
            if type(exc).__name__ in ("Timeout", "ConnectTimeout", "ReadTimeout"):
                raise JevTimeoutError(f"request timed out: {exc}") from exc
            raise JevTransportError(f"{type(exc).__name__}: {exc}") from exc

        if status == 200:
            return _validate_response(parsed)
        snippet = (text or "")[:200]
        if status in (401, 403):
            raise JevAuthError(f"HTTP {status}: authentication failed")
        if status == 429:
            raise JevRateLimited("HTTP 429: rate limited")
        if status == 529:
            raise JevOverloaded("HTTP 529: overloaded")
        if status == 422:
            raise JevBadRequest(f"HTTP 422: {snippet}")
        raise JevTransportError(f"HTTP {status}: {snippet}")
