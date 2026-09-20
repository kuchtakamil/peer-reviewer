from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable


class AdapterError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RateLimitError(AdapterError):
    pass


def check_process_result(result: dict[str, Any]) -> None:
    if result.get("cancelled"):
        raise AdapterError("cancelled", "Provider process was cancelled")
    if result["timed_out"]:
        raise AdapterError("timeout", "Reviewer CLI exceeded its deadline")
    if result.get("output_too_large"):
        raise AdapterError("output_too_large", "Reviewer CLI exceeded the output byte limit")
    if result["returncode"] != 0:
        diagnostic = result["stderr"].lower()
        if b"rate_limit" in diagnostic or b"rate limit" in diagnostic or b"429" in diagnostic:
            raise RateLimitError("rate_limit", "Reviewer CLI reported a subscription rate limit")
        if b"auth" in diagnostic or b"login" in diagnostic:
            raise AdapterError("authentication", "Reviewer CLI authentication failed")
        raise AdapterError("process_exit", f"Reviewer CLI exited with code {result['returncode']}")


def unknown_limit_sample(
    provider: str,
    account_fingerprint: str,
    model_bucket: str,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    observed_at = (clock or (lambda: datetime.now(timezone.utc)))()
    return {
        "provider": provider,
        "account_fingerprint": account_fingerprint,
        "model_bucket": model_bucket,
        "observed_at": observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "unavailable-stub",
        "confidence": "unknown",
        "five_hour": None,
        "other_blockers": [f"{provider} limit reader is not connected"],
    }
