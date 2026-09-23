from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FIVE_HOURS = timedelta(hours=5)
# `/usage` shows reset times truncated to the minute.
DISPLAY_RESOLUTION = timedelta(minutes=1)
_USAGE_LINE = re.compile(
    r"^Current (?P<label>session|week \(all models\)):\s*(?P<used>\d+(?:\.\d+)?)%\s*used"
    r"(?:\s*·\s*resets\s+(?P<reset>.+?))?\s*$",
    re.MULTILINE,
)
_RESET = re.compile(
    r"^(?:(?P<month>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})(?:,|\s+at)\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?"
    r"(?:\s+\((?P<zone>[^)]+)\))?$",
    re.IGNORECASE,
)
_MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1
)}


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


def account_fingerprint(provider: str, account_id: str) -> str:
    """Stable, non-reversible identifier of the provider account behind a worker."""
    digest = hashlib.sha256(f"{provider}\0{account_id}".encode("utf-8")).hexdigest()
    return f"{provider}:{digest[:16]}"


def _parse_reset(text: str, now: datetime) -> datetime:
    match = _RESET.match(text.strip())
    if match is None:
        raise ValueError(f"unrecognized reset time: {text!r}")
    try:
        zone = ZoneInfo(match["zone"]) if match["zone"] else timezone.utc
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown time zone: {match['zone']!r}") from exc
    hour, minute = int(match["hour"]), int(match["minute"] or 0)
    if match["ampm"]:
        if not 1 <= hour <= 12:
            raise ValueError("invalid 12-hour clock value")
        hour = hour % 12 + (12 if match["ampm"].lower() == "pm" else 0)
    local_now = now.astimezone(zone)
    if match["month"]:
        month = _MONTHS.get(match["month"].lower())
        if month is None:
            raise ValueError(f"unknown month: {match['month']!r}")
        candidates = [
            datetime(year, month, int(match["day"]), hour, minute, tzinfo=zone)
            for year in (local_now.year - 1, local_now.year, local_now.year + 1)
        ]
        plausible = [c for c in candidates if -timedelta(days=1) <= c - local_now <= timedelta(days=8)]
        if not plausible:
            raise ValueError("reset date is not near the current time")
        reset = plausible[0]
    else:
        reset = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset + DISPLAY_RESOLUTION <= local_now:
            reset += timedelta(days=1)
    return (reset + DISPLAY_RESOLUTION).astimezone(timezone.utc)


def parse_claude_usage(text: str, now: datetime) -> dict[str, dict[str, Any]]:
    """Parse the human-readable `claude -p /usage` report into rate-limit windows.

    Any text that does not match the known layout raises ValueError, so a changed
    CLI output becomes an unknown limit instead of a guess.
    """
    now = _utc(now)
    windows: dict[str, dict[str, Any]] = {}
    for match in _USAGE_LINE.finditer(text):
        key = "five_hour" if match["label"] == "session" else "seven_day"
        used = float(match["used"])
        if not 0 <= used <= 100:
            raise ValueError("used percentage outside 0..100")
        if match["reset"]:
            reset = _parse_reset(match["reset"], now)
        elif used == 0 and key == "five_hour":
            # No active window yet: the next one starts with the first request.
            reset = now + FIVE_HOURS
        else:
            raise ValueError(f"{key} usage lacks a reset time")
        windows[key] = {"used_percentage": used, "resets_at": reset}
    if "five_hour" not in windows:
        raise ValueError("usage report lacks the current session window")
    return windows


def _codex_five_hour(
    limits: Any, bucket: str
) -> tuple[float, datetime, list[dict[str, Any]]]:
    if not isinstance(limits, dict):
        raise ValueError("missing rate_limits")
    by_id = limits.get("rateLimitsByLimitId")
    snapshot = by_id.get(bucket) if isinstance(by_id, dict) else None
    fallback = limits.get("rateLimits")
    if snapshot is None and isinstance(fallback, dict) and fallback.get("limitId") == bucket:
        snapshot = fallback
    if not isinstance(snapshot, dict):
        raise ValueError("missing model bucket")
    if snapshot.get("spendControlReached") is True:
        raise ValueError("spend control reached")
    windows = [w for w in (snapshot.get("primary"), snapshot.get("secondary")) if isinstance(w, dict)]
    matching = [w for w in windows if w.get("windowDurationMins") == 300]
    if len(matching) != 1:
        raise ValueError("missing unique 300-minute window")
    five_hour = matching[0]
    if limits.get("ordinaryUsageAllowed") is False and not any(
        float(w.get("usedPercent", 0)) >= 100 for w in windows
    ):
        # Blocked for a reason no window explains, so there is no reset to wait for.
        raise ValueError("ordinary usage is not allowed")
    blockers = [
        {
            "kind": f"{w.get('windowDurationMins')}_minute",
            "exhausted": True,
            "resets_at": _iso(_timestamp(w["resetsAt"])),
        }
        for w in windows
        if w is not five_hour and float(w.get("usedPercent", 0)) >= 100
    ]
    return float(five_hour["usedPercent"]), _timestamp(five_hour["resetsAt"]), blockers


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
    *,
    source: str | None = None,
) -> dict[str, Any]:
    observed_at = _utc(observed_at)
    source = source or ("usage-command" if provider == "claude" else "app-server")
    if provider not in {"claude", "codex"}:
        return _unknown_sample(provider, observed_at, account, bucket, source, "unknown provider", raw)
    if raw.get("account_fingerprint") != account:
        mismatch = _unknown_sample(provider, observed_at, account, bucket, source, "account mismatch", raw)
        if isinstance(raw.get("account_fingerprint"), str):
            mismatch["observed_account_fingerprint"] = raw["account_fingerprint"]
        return mismatch

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
            used, reset, other_blockers = _codex_five_hour(raw.get("rate_limits"), bucket)
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
