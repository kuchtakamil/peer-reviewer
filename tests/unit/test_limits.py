import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from peer_reviewer.limits import decide_round_start, normalize_limit


NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parents[1] / "fixtures" / "limits"


def sample(
    provider: str,
    used: float,
    *,
    observed_at: datetime = NOW,
    resets_at: datetime | None = None,
    confidence: str = "verified",
    window_seconds: int = 18000,
    blockers=None,
):
    return {
        "provider": provider,
        "account_fingerprint": f"fixture:{provider}",
        "model_bucket": "subscription",
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "source": "fixture",
        "confidence": confidence,
        "five_hour": {
            "used_percent": used,
            "resets_at": (resets_at or NOW + timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
            "window_seconds": window_seconds,
        },
        "other_blockers": [] if blockers is None else blockers,
    }


@pytest.mark.parametrize(
    ("used", "allow"),
    [(79.9, True), (80.0, True), (80.1, False)],
)
def test_five_hour_threshold_blocks_only_below_twenty_percent_remaining(used, allow):
    decision = decide_round_start({"A": sample("claude", used), "B": sample("codex", 0)}, NOW)
    assert decision["allow"] is allow


def test_unknown_provider_blocks_round():
    decision = decide_round_start({}, NOW)
    assert decision["allow"] is False
    assert decision["wake_at"] is None
    assert decision["next_check_at"] >= NOW + timedelta(seconds=60)
    assert decision["reason"] == "PAUSED_LIMIT_UNKNOWN"


@pytest.mark.parametrize(
    "bad_sample",
    [
        sample("claude", 10, observed_at=NOW - timedelta(seconds=61)),
        sample("claude", 10, resets_at=NOW - timedelta(seconds=1)),
        sample("claude", float("nan")),
        sample("claude", float("inf")),
        sample("claude", -1),
        sample("claude", 101),
        sample("claude", 10, window_seconds=900),
        sample("claude", 10, confidence="unknown"),
    ],
)
def test_stale_or_invalid_required_sample_blocks_as_unknown(bad_sample):
    decision = decide_round_start({"A": bad_sample, "B": sample("codex", 0)}, NOW)
    assert decision["allow"] is False
    assert decision["reason"] == "PAUSED_LIMIT_UNKNOWN"
    assert decision["wake_at"] is None


def test_low_limits_wait_for_latest_blocking_reset_plus_guard():
    reset_a = NOW + timedelta(minutes=20)
    reset_b = NOW + timedelta(minutes=40)
    decision = decide_round_start(
        {
            "A": sample("claude", 90, resets_at=reset_a),
            "B": sample("codex", 95, resets_at=reset_b),
        },
        NOW,
    )
    assert decision["allow"] is False
    assert decision["reason"] == "PAUSED_LIMIT_LOW"
    assert decision["wake_at"] == reset_b + timedelta(seconds=5)


def test_exhausted_weekly_window_blocks_even_when_five_hour_is_free():
    weekly_reset = NOW + timedelta(days=2)
    decision = decide_round_start(
        {
            "A": sample(
                "claude",
                10,
                blockers=[
                    {
                        "kind": "seven_day",
                        "exhausted": True,
                        "resets_at": weekly_reset.isoformat().replace("+00:00", "Z"),
                    }
                ],
            ),
            "B": sample("codex", 10),
        },
        NOW,
    )
    assert decision["allow"] is False
    assert decision["wake_at"] == weekly_reset + timedelta(seconds=5)


def test_retry_after_extends_unknown_next_check():
    unknown = sample("claude", 0, confidence="unknown")
    unknown["retry_after_seconds"] = 300
    decision = decide_round_start({"A": unknown, "B": sample("codex", 0)}, NOW)
    assert decision["next_check_at"] == NOW + timedelta(seconds=300)


def test_elapsed_wake_time_with_old_payload_still_blocks():
    old_now = NOW - timedelta(hours=5)
    old = sample(
        "claude",
        90,
        observed_at=old_now,
        resets_at=NOW - timedelta(seconds=5),
    )
    decision = decide_round_start({"A": old, "B": sample("codex", 0)}, NOW)
    assert decision["allow"] is False
    assert decision["reason"] == "PAUSED_LIMIT_UNKNOWN"


def test_claude_statusline_payload_normalizes_five_hour_window():
    raw = {
        "account_fingerprint": "fixture:claude",
        "rate_limits": {
            "five_hour": {"used_percentage": 23.5, "resets_at": int((NOW + timedelta(hours=2)).timestamp())}
        },
    }
    normalized = normalize_limit("claude", raw, NOW, "fixture:claude", "subscription")
    assert normalized["confidence"] == "verified"
    assert normalized["five_hour"]["used_percent"] == 23.5
    assert normalized["five_hour"]["window_seconds"] == 18000


def test_codex_selects_300_minute_bucket_instead_of_primary_label():
    raw = {
        "account_fingerprint": "fixture:codex",
        "rate_limits": [
            {"name": "primary", "model_bucket": "fixture-bucket", "windowDurationMins": 15, "usedPercent": 1, "resetsAt": int((NOW + timedelta(minutes=15)).timestamp())},
            {"name": "secondary", "model_bucket": "fixture-bucket", "windowDurationMins": 300, "usedPercent": 31, "resetsAt": int((NOW + timedelta(hours=3)).timestamp())},
        ],
    }
    normalized = normalize_limit("codex", raw, NOW, "fixture:codex", "fixture-bucket")
    assert normalized["confidence"] == "verified"
    assert normalized["five_hour"]["used_percent"] == 31.0


@pytest.mark.parametrize(
    ("account", "bucket"),
    [("fixture:other", "fixture-bucket"), ("fixture:codex", "other-bucket")],
)
def test_account_or_bucket_mismatch_is_unknown(account, bucket):
    raw = {
        "account_fingerprint": account,
        "rate_limits": [
            {"model_bucket": bucket, "windowDurationMins": 300, "usedPercent": 5, "resetsAt": int((NOW + timedelta(hours=2)).timestamp())}
        ],
    }
    normalized = normalize_limit("codex", raw, NOW, "fixture:codex", "fixture-bucket")
    assert normalized["confidence"] == "unknown"
    assert normalized["five_hour"] is None


def test_unavailable_claude_fixture_can_never_allow_round():
    unavailable = json.loads((FIXTURES / "claude_unavailable.json").read_text())
    decision = decide_round_start({"A": unavailable, "B": sample("codex", 0)}, NOW)
    assert decision["allow"] is False
    assert decision["reason"] == "PAUSED_LIMIT_UNKNOWN"
