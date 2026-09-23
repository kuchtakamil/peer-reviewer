import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from peer_reviewer.limits import (
    account_fingerprint,
    decide_round_start,
    normalize_limit,
    parse_claude_usage,
)


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


def codex_snapshot(windows, *, bucket="codex", **extra):
    primary, secondary = (list(windows) + [None, None])[:2]
    snapshot = {"limitId": bucket, "primary": primary, "secondary": secondary, **extra}
    return {"ordinaryUsageAllowed": True, "rateLimits": snapshot, "rateLimitsByLimitId": {bucket: snapshot}}


def window(minutes, used, resets_at):
    return {"windowDurationMins": minutes, "usedPercent": used, "resetsAt": int(resets_at.timestamp())}


def test_codex_selects_300_minute_window_instead_of_primary_label():
    raw = {
        "account_fingerprint": "fixture:codex",
        "rate_limits": codex_snapshot(
            [window(15, 1, NOW + timedelta(minutes=15)), window(300, 31, NOW + timedelta(hours=3))]
        ),
    }
    normalized = normalize_limit("codex", raw, NOW, "fixture:codex", "codex")
    assert normalized["confidence"] == "verified"
    assert normalized["source"] == "app-server"
    assert normalized["five_hour"]["used_percent"] == 31.0


def test_codex_real_app_server_fixture_normalizes_codex_bucket():
    observed = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    raw = {
        "account_fingerprint": "fixture:codex",
        "rate_limits": json.loads((FIXTURES / "codex_app_server_rate_limits.json").read_text()),
    }
    normalized = normalize_limit("codex", raw, observed, "fixture:codex", "codex")
    assert normalized["confidence"] == "verified"
    assert normalized["five_hour"] == {
        "used_percent": 73.0,
        "resets_at": "2026-09-23T14:32:01Z",
        "window_seconds": 18000,
    }
    assert normalized["other_blockers"] == []


def test_codex_exhausted_weekly_window_is_a_blocker():
    raw = {
        "account_fingerprint": "fixture:codex",
        "rate_limits": codex_snapshot(
            [window(300, 10, NOW + timedelta(hours=3)), window(10080, 100, NOW + timedelta(days=2))]
        ),
    }
    normalized = normalize_limit("codex", raw, NOW, "fixture:codex", "codex")
    assert normalized["other_blockers"] == [
        {
            "kind": "10080_minute",
            "exhausted": True,
            "resets_at": (NOW + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
        }
    ]
    assert decide_round_start({"A": sample("claude", 0), "B": normalized}, NOW)["reason"] == "PAUSED_LIMIT_LOW"


def test_codex_exhausted_five_hour_waits_for_known_reset_instead_of_unknown():
    observed = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    raw = {
        "account_fingerprint": "fixture:codex",
        "rate_limits": json.loads((FIXTURES / "codex_app_server_exhausted.json").read_text()),
    }
    normalized = normalize_limit("codex", raw, observed, "fixture:codex", "codex")
    assert normalized["confidence"] == "verified"
    assert normalized["five_hour"]["used_percent"] == 100.0
    claude = sample("claude", 0, observed_at=observed, resets_at=observed + timedelta(hours=1))
    decision = decide_round_start({"A": claude, "B": normalized}, observed)
    assert decision["reason"] == "PAUSED_LIMIT_LOW"
    assert decision["wake_at"] == datetime(2026, 9, 23, 14, 32, 6, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "limits",
    [
        {**codex_snapshot([window(300, 5, NOW + timedelta(hours=2))]), "ordinaryUsageAllowed": False},
        codex_snapshot([window(300, 5, NOW + timedelta(hours=2))], spendControlReached=True),
        codex_snapshot([window(10080, 5, NOW + timedelta(days=2))]),
        {"rateLimitsByLimitId": {}},
    ],
)
def test_codex_unusable_or_missing_window_is_unknown(limits):
    raw = {"account_fingerprint": "fixture:codex", "rate_limits": limits}
    normalized = normalize_limit("codex", raw, NOW, "fixture:codex", "codex")
    assert normalized["confidence"] == "unknown"
    assert normalized["five_hour"] is None


@pytest.mark.parametrize(
    ("account", "bucket"),
    [("fixture:other", "codex"), ("fixture:codex", "other-bucket")],
)
def test_account_or_bucket_mismatch_is_unknown(account, bucket):
    raw = {
        "account_fingerprint": account,
        "rate_limits": codex_snapshot([window(300, 5, NOW + timedelta(hours=2))], bucket=bucket),
    }
    normalized = normalize_limit("codex", raw, NOW, "fixture:codex", "codex")
    assert normalized["confidence"] == "unknown"
    assert normalized["five_hour"] is None


def test_account_mismatch_reports_observed_fingerprint_for_setup():
    raw = {"account_fingerprint": "codex:observed", "rate_limits": {}}
    normalized = normalize_limit("codex", raw, NOW, "unconfigured", "codex")
    assert normalized["observed_account_fingerprint"] == "codex:observed"


def test_account_fingerprint_is_stable_and_does_not_contain_identifier():
    identifier = "00000000-0000-4000-8000-00000000c1a0"
    fingerprint = account_fingerprint("claude", identifier)
    assert fingerprint == account_fingerprint("claude", identifier)
    assert fingerprint.startswith("claude:")
    assert identifier not in fingerprint
    assert fingerprint != account_fingerprint("codex", identifier)


WARSAW_NOON = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)


def test_claude_usage_text_from_print_mode_fixture_parses_both_windows():
    envelope = json.loads((FIXTURES / "claude_usage_print_mode.json").read_text())
    windows = parse_claude_usage(envelope["result"], WARSAW_NOON)
    # Display is truncated to the minute; the parser rounds the reset up.
    assert windows["five_hour"] == {
        "used_percentage": 4.0,
        "resets_at": datetime(2026, 9, 23, 14, 20, tzinfo=timezone.utc),
    }
    assert windows["seven_day"] == {
        "used_percentage": 0.0,
        "resets_at": datetime(2026, 9, 25, 23, 0, tzinfo=timezone.utc),
    }
    normalized = normalize_limit(
        "claude",
        {"account_fingerprint": "fixture:claude", "rate_limits": windows},
        WARSAW_NOON,
        "fixture:claude",
        "subscription",
        source="usage-command",
    )
    assert normalized["confidence"] == "verified"
    assert normalized["source"] == "usage-command"
    assert normalized["five_hour"]["resets_at"] == "2026-09-23T14:20:00Z"


@pytest.mark.parametrize(
    ("line", "expected_reset"),
    [
        ("Current session: 12% used · resets 4:19pm (Europe/Warsaw)", datetime(2026, 9, 23, 14, 20, tzinfo=timezone.utc)),
        ("Current session: 12% used · resets 4pm (UTC)", datetime(2026, 9, 23, 16, 1, tzinfo=timezone.utc)),
        ("Current session: 12% used · resets 16:19 (Europe/Warsaw)", datetime(2026, 9, 23, 14, 20, tzinfo=timezone.utc)),
        ("Current session: 12% used · resets Sep 23 at 4:19pm", datetime(2026, 9, 23, 16, 20, tzinfo=timezone.utc)),
        ("Current session: 12% used · resets 9:00am (UTC)", datetime(2026, 9, 24, 9, 1, tzinfo=timezone.utc)),
    ],
)
def test_claude_usage_reset_formats(line, expected_reset):
    windows = parse_claude_usage(line, WARSAW_NOON)
    assert windows["five_hour"]["used_percentage"] == 12.0
    assert windows["five_hour"]["resets_at"] == expected_reset


def test_claude_usage_year_rollover_picks_nearest_future_date():
    now = datetime(2026, 12, 31, 22, 0, tzinfo=timezone.utc)
    windows = parse_claude_usage("Current session: 5% used · resets Jan 1, 2:00am (UTC)", now)
    assert windows["five_hour"]["resets_at"] == datetime(2027, 1, 1, 2, 1, tzinfo=timezone.utc)


def test_claude_usage_idle_window_without_reset_is_a_fresh_window():
    windows = parse_claude_usage("Current session: 0% used", WARSAW_NOON)
    assert windows["five_hour"] == {
        "used_percentage": 0.0,
        "resets_at": WARSAW_NOON + timedelta(hours=5),
    }


@pytest.mark.parametrize(
    "text",
    [
        "",
        "/usage isn't available in this environment.",
        "Current session: 30% used",
        "Current session: 130% used · resets 4pm (UTC)",
        "Current session: 30% used · resets sometime soon",
        "Current session: 30% used · resets 4pm (Mars/Olympus)",
        "Current session: 30% used · resets Feb 30, 4pm (UTC)",
    ],
)
def test_claude_usage_unrecognized_text_is_rejected(text):
    with pytest.raises(ValueError):
        parse_claude_usage(text, WARSAW_NOON)


def test_claude_usage_exhausted_week_becomes_blocker():
    text = (
        "Current session: 10% used · resets 4pm (UTC)\n"
        "Current week (all models): 100% used · resets Sep 26, 12:59am (UTC)"
    )
    windows = parse_claude_usage(text, WARSAW_NOON)
    normalized = normalize_limit(
        "claude",
        {"account_fingerprint": "fixture:claude", "rate_limits": windows},
        WARSAW_NOON,
        "fixture:claude",
        "subscription",
    )
    assert normalized["other_blockers"][0]["kind"] == "seven_day"


def test_unavailable_claude_fixture_can_never_allow_round():
    unavailable = json.loads((FIXTURES / "claude_unavailable.json").read_text())
    decision = decide_round_start({"A": unavailable, "B": sample("codex", 0)}, NOW)
    assert decision["allow"] is False
    assert decision["reason"] == "PAUSED_LIMIT_UNKNOWN"
