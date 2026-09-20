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


def test_unavailable_claude_probe_blocks_live_go_without_exposing_secrets():
    probe = load_probe_module()

    result = probe.probe_capabilities(
        claude_probe=probe.UnavailableClaudeProbe("claude executable unavailable"),
        codex_result={
            "version": "fixture-codex",
            "subscription_auth": True,
            "preflight_before_first_turn": True,
            "preflight_after_reset": True,
            "headless": True,
        },
    )

    assert result["preflight_before_first_turn"] == {"A": False, "B": True}
    assert result["preflight_after_reset"] == {"A": False, "B": True}
    assert result["subscription_auth"] is False
    assert result["headless"] is False
    assert result["statusline_in_print_mode"] is False
    assert result["blocking_reasons"] == ["claude executable unavailable"]
    assert "token" not in json.dumps(result).lower()


def test_probe_result_contains_only_the_public_capability_contract():
    probe = load_probe_module()

    result = probe.probe_capabilities(
        claude_probe=probe.UnavailableClaudeProbe("not tested"),
        codex_result=None,
    )

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
    assert "Codex capability probe has not run" in result["blocking_reasons"]


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
