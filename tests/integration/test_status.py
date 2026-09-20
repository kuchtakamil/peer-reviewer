from __future__ import annotations

import json
from datetime import datetime, timezone

from peer_reviewer.report import rebuild_views, render_event, render_status
from peer_reviewer.store import SessionStore


def test_status_and_event_views_include_identity_and_update_time(tmp_path):
    state = {
        "phase": "CHECKPOINT",
        "last_completed_round": 2,
        "updated_at": "2026-09-19T12:00:00Z",
        "checkpoint": {"event_id": "checkpoint-0002", "deadline": "2026-09-19T12:01:00Z"},
    }
    event = {
        "event_id": "checkpoint-0002",
        "session_id": "s-001",
        "kind": "checkpoint",
        "created_at": "2026-09-19T12:00:00Z",
        "round_no": 2,
        "deadline": "2026-09-19T12:01:00Z",
        "counts": {"agreed": 1, "rejected": 1, "disputed": 2},
        "veto_command": "peer-reviewer stop --session /sessions/s-001",
    }
    status = render_status(state)
    rendered = render_event(event)
    assert "checkpoint-0002" in status
    assert "2026-09-19T12:00:00Z" in status
    assert "agreed: 1" in rendered
    assert "rejected: 1" in rendered
    assert "disputed: 2" in rendered
    assert event["veto_command"] in rendered


def test_pause_view_says_when_reset_time_is_unknown():
    event = {
        "event_id": "pause-1",
        "session_id": "s-001",
        "kind": "pause",
        "created_at": "2026-09-19T12:00:00Z",
        "round_no": 1,
        "reason": "PAUSED_LIMIT_UNKNOWN",
        "details": {"A": "unverified sample"},
        "wake_at": None,
        "next_check_at": "2026-09-19T12:01:00Z",
    }
    rendered = render_event(event)
    assert "reset time unknown" in rendered
    assert "2026-09-19T12:01:00Z" in rendered


def test_missing_markdown_views_are_rebuilt_from_json(tmp_path):
    store = SessionStore(tmp_path)
    store.create(b"text\n", {"session_id": "s-001"})
    store.write_status(
        {"phase": "READY", "last_completed_round": 0, "updated_at": "2026-09-19T12:00:00Z"}
    )
    store.write_event(
        {
            "event_id": "event-1",
            "session_id": "s-001",
            "kind": "error",
            "created_at": "2026-09-19T12:00:00Z",
            "error_code": "fixture",
        }
    )
    (tmp_path / "runtime" / "status.md").unlink(missing_ok=True)
    (tmp_path / "runtime" / "events" / "event-1.md").unlink(missing_ok=True)
    rebuild_views(tmp_path)
    assert "READY" in (tmp_path / "runtime" / "status.md").read_text()
    assert "fixture" in (tmp_path / "runtime" / "events" / "event-1.md").read_text()


def test_store_creates_markdown_views_after_authoritative_json(tmp_path):
    store = SessionStore(tmp_path)
    store.create(b"text\n", {"session_id": "s-001"})
    state = {"phase": "READY", "last_completed_round": 0, "updated_at": datetime.now(timezone.utc).isoformat()}
    store.write_status(state)
    assert json.loads((tmp_path / "runtime" / "state.json").read_text()) == state
    assert (tmp_path / "runtime" / "status.md").is_file()
