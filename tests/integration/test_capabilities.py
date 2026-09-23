import importlib.util
import json
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
PROBE_PATH = ROOT / "scripts" / "probe_capabilities.py"


def load_probe_module():
    spec = importlib.util.spec_from_file_location("probe_capabilities", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reading(provider, *, confidence="verified", source=None):
    return {
        "provider": provider,
        "account_fingerprint": f"{provider}:fixture",
        "model_bucket": "subscription",
        "observed_at": "2026-09-23T10:00:00Z",
        "source": source or {"claude": "usage-command", "codex": "app-server"}[provider],
        "confidence": confidence,
        "five_hour": None
        if confidence != "verified"
        else {"used_percent": 4.0, "resets_at": "2026-09-23T14:20:00Z", "window_seconds": 18000},
        "other_blockers": [] if confidence == "verified" else [f"{provider} usage read failed: authentication"],
    }


VERSIONS = {"claude": "2.1.280", "codex": "0.156.1"}


def test_verified_non_generative_readings_pass_the_live_contract():
    probe = load_probe_module()
    result = probe.probe_capabilities({"claude": reading("claude"), "codex": reading("codex")}, VERSIONS)
    assert result["blocking_reasons"] == []
    assert result["preflight_before_first_turn"] == {"A": True, "B": True}
    assert result["preflight_after_reset"] == {"A": True, "B": True}
    assert result["subscription_auth"] is True
    assert result["statusline_in_print_mode"] is False


def test_unverified_claude_reading_blocks_live_go_without_exposing_secrets():
    probe = load_probe_module()
    result = probe.probe_capabilities(
        {"claude": reading("claude", confidence="unknown"), "codex": reading("codex")}, VERSIONS
    )
    assert result["preflight_before_first_turn"] == {"A": False, "B": True}
    assert result["subscription_auth"] is False
    assert result["blocking_reasons"] == ["claude: claude usage read failed: authentication"]
    assert "token" not in json.dumps(result).lower()


def test_generative_or_statusline_source_is_never_accepted_as_preflight():
    probe = load_probe_module()
    result = probe.probe_capabilities(
        {"claude": reading("claude", source="statusline"), "codex": reading("codex")}, VERSIONS
    )
    assert result["preflight_before_first_turn"]["A"] is False


def test_probe_result_contains_only_the_public_capability_contract():
    probe = load_probe_module()

    result = probe.probe_capabilities({})

    assert set(result) == {
        "versions",
        "subscription_auth",
        "statusline_in_print_mode",
        "preflight_before_first_turn",
        "preflight_after_reset",
        "headless",
        "blocking_reasons",
    }
    assert result["versions"] == {"claude": None, "codex": None}
    assert "codex probe has not run" in result["blocking_reasons"]


@pytest.mark.live
def test_real_preflight_contract():
    result_path = os.environ.get("PEER_REVIEWER_CAPABILITY_RESULT")
    if result_path is None:
        pytest.skip("PEER_REVIEWER_CAPABILITY_RESULT does not name a concrete live run")

    capability_result = json.loads(Path(result_path).read_text(encoding="utf-8"))
    assert capability_result["subscription_auth"] is True
    assert capability_result["preflight_before_first_turn"] == {"A": True, "B": True}
    assert capability_result["preflight_after_reset"] == {"A": True, "B": True}
    assert capability_result["headless"] is True
    assert capability_result["blocking_reasons"] == []
