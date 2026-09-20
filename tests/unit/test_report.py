from __future__ import annotations

from peer_reviewer.report import render_report


def report_state():
    issues = {
        key: {"id": key, "problem": problem, "author": key[0]}
        for key, problem in {
            "A-1": "accepted issue",
            "A-2": "rejected issue",
            "B-1": "disputed issue",
            "B-2": "duplicate issue",
        }.items()
    }
    versions = {
        "v-a1": {
            "issue_id": "A-1",
            "round_no": 1,
            "payload": {"resolution": "accepted", "fix": "apply fix", "rationale": "valid", "duplicate_of": None},
        },
        "v-a2": {
            "issue_id": "A-2",
            "round_no": 2,
            "payload": {
                "resolution": "rejected",
                "fix": "",
                "rationale": "withdrawn because B-r2-1 proved the source already qualifies it",
                "duplicate_of": None,
            },
        },
        "v-b2": {
            "issue_id": "B-2",
            "round_no": 2,
            "payload": {"resolution": "duplicate", "fix": "", "rationale": "same root cause", "duplicate_of": "A-1"},
        },
    }
    return {
        "session_id": "s-001",
        "round_no": 3,
        "issues": issues,
        "versions": versions,
        "resolved": {"A-1": "v-a1", "A-2": "v-a2", "B-2": "v-b2"},
        "open_proposals": {"B-1": []},
        "positions": {
            "A": {
                "B-1": {
                    "action": "accept",
                    "version_ref": "missing-v",
                    "argument_id": "A-r3-1",
                    "reason": "A considers the issue demonstrated",
                }
            },
            "B": {
                "B-1": {
                    "action": "oppose",
                    "version_ref": "missing-v",
                    "argument_id": "B-r3-1",
                    "reason": "B considers the evidence insufficient",
                }
            },
        },
        "arguments": {
            "A-r3-1": {"round_no": 3, "reason": "A considers the issue demonstrated"},
            "B-r3-1": {"round_no": 3, "reason": "B considers the evidence insufficient"},
            "B-r2-1": {"round_no": 2, "reason": "source already qualifies it"},
        },
    }


def test_report_covers_accepted_rejected_disputed_and_duplicate_issues():
    state = report_state()
    state["issues"]["A-1"]["problem"] = "original wording"
    state["versions"]["v-a1"]["payload"]["problem"] = "agreed revised wording"
    history = [{"round_no": value} for value in (1, 2, 3)]
    rendered = render_report(state, history, "NO_CONSENSUS")
    for issue_id in ("A-1", "A-2", "B-1", "B-2"):
        assert issue_id in rendered
    assert "apply fix" in rendered
    assert "A-1: agreed revised wording" in rendered
    assert "withdrawn because B-r2-1" in rendered
    assert "A considers the issue demonstrated" in rendered
    assert "B considers the evidence insufficient" in rendered
    assert "B-2 -> A-1" in rendered
    assert "substantively refuted" not in rendered
    assert "[round 2](rounds/0002/round.md)" in rendered


def test_partial_report_is_explicit_and_deterministic():
    state = report_state()
    first = render_report(state, [], "STOPPED")
    second = render_report(state, [], "STOPPED")
    assert first == second
    assert "PARTIAL" in first
    assert "STOPPED" in first


def test_empty_debate_has_explicit_empty_sections():
    state = {
        "session_id": "s-empty",
        "round_no": 2,
        "issues": {},
        "versions": {},
        "resolved": {},
        "positions": {"A": {}, "B": {}},
        "arguments": {},
    }
    rendered = render_report(state, [], "CONSENSUS")
    assert "No agreed issues." in rendered
    assert "No jointly rejected issues." in rendered
    assert "No disputed issues." in rendered
    assert "No duplicate issues." in rendered
