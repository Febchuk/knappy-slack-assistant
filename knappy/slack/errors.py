"""Reading a Slack Web API failure: its error code and, for a rate limit, how long Slack asked us to wait."""

from __future__ import annotations

from typing import Any

# Distributed apps outside the Slack Marketplace get 1 conversations.history/replies call a minute (Spec 23 §2).
DEFAULT_RETRY_AFTER_S = 60.0


def slack_error(exc: BaseException) -> str:
    response: Any = getattr(exc, "response", None)
    if response is not None:
        try:
            error = response.get("error")
        except Exception:
            error = None
        if error:
            return str(error)
    return str(exc)


def retry_after(exc: BaseException) -> float | None:
    """Seconds to wait when `exc` is a rate limit, otherwise None."""
    if slack_error(exc) != "ratelimited":
        return None
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return float(raw) if raw is not None else DEFAULT_RETRY_AFTER_S
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_S
