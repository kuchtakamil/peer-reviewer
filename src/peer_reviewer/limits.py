from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise ValueError("timestamp must be finite")
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        return _utc(datetime.fromisoformat(normalized))
    raise ValueError("unsupported timestamp")


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _unknown_sample(
    provider: str,
    observed_at: datetime,
    account: str,
    bucket: str,
    source: str,
    reason: str,
    raw: dict[str, Any],
) -> dict[str, Any]:
    result = {
        "provider": provider,
        "account_fingerprint": account,
        "model_bucket": bucket,
        "observed_at": _iso(observed_at),
        "source": source,
        "confidence": "unknown",
        "five_hour": None,
        "other_blockers": [reason],
    }
    if isinstance(raw.get("retry_after_seconds"), (int, float)):
        result["retry_after_seconds"] = raw["retry_after_seconds"]
    return result


def normalize_limit(
    provider: str,
    raw: dict[str, Any],
    observed_at: datetime,
    account: str,
    bucket: str,
) -> dict[str, Any]:
    observed_at = _utc(observed_at)
    source = "statusline" if provider == "claude" else "app-server"
    if provider not in {"claude", "codex"}:
        return _unknown_sample(provider, observed_at, account, bucket, source, "unknown provider", raw)
    if raw.get("account_fingerprint") != account:
        return _unknown_sample(provider, observed_at, account, bucket, source, "account mismatch", raw)

    try:
        if provider == "claude":
            rate_limits = raw.get("rate_limits")
            if not isinstance(rate_limits, dict):
                raise ValueError("missing rate_limits")
            window = rate_limits.get("five_hour")
            if not isinstance(window, dict):
                raise ValueError("missing five_hour")
            used = float(window["used_percentage"])
            reset = _timestamp(window["resets_at"])
            other_blockers: list[dict[str, Any]] = []
            weekly = rate_limits.get("seven_day")
            if isinstance(weekly, dict) and float(weekly.get("used_percentage", 0)) >= 100:
                other_blockers.append(
                    {
                        "kind": "seven_day",
                        "exhausted": True,
                        "resets_at": _iso(_timestamp(weekly["resets_at"])),
                    }
                )
        else:
            windows = raw.get("rate_limits")
            if not isinstance(windows, list):
                raise ValueError("missing rate_limits")
            matching = [
                item
                for item in windows
                if isinstance(item, dict)
                and item.get("model_bucket") == bucket
                and item.get("windowDurationMins") == 300
            ]
            if len(matching) != 1:
                raise ValueError("missing unique 300-minute model bucket")
            window = matching[0]
            used = float(window["usedPercent"])
            reset = _timestamp(window["resetsAt"])
            other_blockers = list(raw.get("other_blockers", []))
        if not math.isfinite(used) or not 0 <= used <= 100:
            raise ValueError("used percentage outside 0..100")
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return _unknown_sample(provider, observed_at, account, bucket, source, str(exc), raw)

    return {
        "provider": provider,
        "account_fingerprint": account,
        "model_bucket": bucket,
        "observed_at": _iso(observed_at),
        "source": source,
        "confidence": "verified",
        "five_hour": {
            "used_percent": used,
            "resets_at": _iso(reset),
            "window_seconds": 18000,
        },
        "other_blockers": other_blockers,
    }


def _required_sample_error(sample: Any, now: datetime, max_age_seconds: int) -> str | None:
    if not isinstance(sample, dict):
        return "missing sample"
    if sample.get("confidence") != "verified":
        return "unverified sample"
    try:
        observed_at = _timestamp(sample["observed_at"])
        age = (now - observed_at).total_seconds()
        if age < -5 or age > max_age_seconds:
            return "stale sample"
        window = sample["five_hour"]
        if not isinstance(window, dict) or window.get("window_seconds") != 18000:
            return "unknown five-hour window"
        used = float(window["used_percent"])
        if not math.isfinite(used) or not 0 <= used <= 100:
            return "invalid used percentage"
        if _timestamp(window["resets_at"]) <= now:
            return "reset is not in the future"
    except (KeyError, TypeError, ValueError, OverflowError):
        return "malformed sample"
    return None


def decide_round_start(
    samples: dict[str, dict[str, Any]],
    now: datetime,
    max_age_seconds: int = 60,
    minimum_remaining_percent: float = 20.0,
) -> dict[str, Any]:
    now = _utc(now)
    if not math.isfinite(float(minimum_remaining_percent)) or not 0 <= float(minimum_remaining_percent) <= 100:
        raise ValueError("minimum remaining percentage must be between 0 and 100")
    errors = {
        reviewer: error
        for reviewer in ("A", "B")
        if (error := _required_sample_error(samples.get(reviewer), now, max_age_seconds)) is not None
    }
    if errors:
        attempts = [
            int(sample.get("unknown_attempt", 0))
            for sample in samples.values()
            if isinstance(sample, dict) and isinstance(sample.get("unknown_attempt", 0), int)
        ]
        retry_delays = [
            float(sample.get("retry_after_seconds", 0))
            for sample in samples.values()
            if isinstance(sample, dict)
            and isinstance(sample.get("retry_after_seconds", 0), (int, float))
            and math.isfinite(float(sample.get("retry_after_seconds", 0)))
        ]
        attempt = max(attempts, default=0)
        backoff = min(60 * (2**max(0, attempt)), 900)
        delay = max([float(backoff), *retry_delays])
        return {
            "allow": False,
            "reason": "PAUSED_LIMIT_UNKNOWN",
            "wake_at": None,
            "next_check_at": now + timedelta(seconds=delay),
            "samples": samples,
            "details": errors,
        }

    blocking_resets: list[datetime] = []
    details: dict[str, str] = {}
    for reviewer in ("A", "B"):
        sample = samples[reviewer]
        window = sample["five_hour"]
        used = float(window["used_percent"])
        if 100.0 - used < float(minimum_remaining_percent):
            blocking_resets.append(_timestamp(window["resets_at"]))
            details[reviewer] = f"five-hour remaining below {float(minimum_remaining_percent):g}%"
        for blocker in sample.get("other_blockers", []):
            if not isinstance(blocker, dict):
                return {
                    "allow": False,
                    "reason": "PAUSED_LIMIT_UNKNOWN",
                    "wake_at": None,
                    "next_check_at": now + timedelta(seconds=60),
                    "samples": samples,
                    "details": {reviewer: "unstructured other blocker"},
                }
            if blocker.get("exhausted") is True:
                try:
                    reset = _timestamp(blocker["resets_at"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    return {
                        "allow": False,
                        "reason": "PAUSED_LIMIT_UNKNOWN",
                        "wake_at": None,
                        "next_check_at": now + timedelta(seconds=60),
                        "samples": samples,
                        "details": {reviewer: "invalid blocker reset"},
                    }
                if reset <= now:
                    return {
                        "allow": False,
                        "reason": "PAUSED_LIMIT_UNKNOWN",
                        "wake_at": None,
                        "next_check_at": now + timedelta(seconds=60),
                        "samples": samples,
                        "details": {reviewer: "expired blocker telemetry"},
                    }
                blocking_resets.append(reset)
                details[reviewer] = f"{blocker.get('kind', 'other')} exhausted"

    if blocking_resets:
        wake_at = max(blocking_resets) + timedelta(seconds=5)
        return {
            "allow": False,
            "reason": "PAUSED_LIMIT_LOW",
            "wake_at": wake_at,
            "next_check_at": wake_at,
            "samples": samples,
            "details": details,
        }
    return {
        "allow": True,
        "reason": "ALLOW",
        "wake_at": None,
        "next_check_at": None,
        "samples": samples,
        "details": {},
    }
