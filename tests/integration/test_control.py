from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from peer_reviewer.cli import main
from peer_reviewer.control import Control, submit_command
from peer_reviewer.engine import Engine
from peer_reviewer.store import SessionStore


NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


class Clock:
    def now(self):
        return NOW


def session(tmp_path):
    store = SessionStore(tmp_path)
    store.create(b"text\n", {"session_id": "s-001"})
    store.write_status(
        {
            "phase": "PAUSED_LIMIT_UNKNOWN",
            "last_completed_round": 2,
            "updated_at": NOW.isoformat(),
        }
    )
    return store


def command(command_id="stop-42", *, session_id="s-001", round_no=2, kind="stop"):
    return {
        "command_id": command_id,
        "session_id": session_id,
        "kind": kind,
        "round_no": round_no,
        "created_at": NOW.isoformat(),
    }


def test_duplicate_command_returns_same_final_acknowledgement(tmp_path):
    session(tmp_path)
    assert submit_command(tmp_path, command())["status"] == "submitted"
    control = Control(tmp_path, Clock())
    accepted = control.poll()
    control.acknowledge(accepted[0], "STOPPED")
    first = submit_command(tmp_path, command())
    second = submit_command(tmp_path, command())
    assert first == second
    assert first["status"] == "applied"
    assert Control(tmp_path, Clock()).poll() == []


def test_foreign_session_and_stale_round_are_rejected_without_delivery(tmp_path):
    session(tmp_path)
    submit_command(tmp_path, command("foreign", session_id="other"))
    submit_command(tmp_path, command("stale", round_no=1))
    control = Control(tmp_path, Clock())
    assert control.poll() == []
    assert submit_command(tmp_path, command("foreign", session_id="other"))["ack"]["outcome"] == "rejected_foreign_session"
    assert submit_command(tmp_path, command("stale", round_no=1))["ack"]["outcome"] == "rejected_stale_round"


def test_received_command_is_redelivered_after_crash_until_applied(tmp_path):
    session(tmp_path)
    submit_command(tmp_path, command())
    assert Control(tmp_path, Clock()).poll() == [command()]
    # A new process sees the durable receipt but no final acknowledgement.
    assert Control(tmp_path, Clock()).poll() == [command()]


def test_missing_ack_is_not_reported_as_stopped_and_stale_heartbeat_warns(tmp_path):
    store = session(tmp_path)
    store.write_status(
        {
            "phase": "PAUSED_LIMIT_UNKNOWN",
            "last_completed_round": 2,
            "updated_at": (NOW - timedelta(minutes=10)).isoformat(),
        }
    )
    result = submit_command(tmp_path, command())
    assert result["status"] == "submitted"
    assert result["session_stopped"] is False
    assert result["warning"] == "engine heartbeat is stale or unavailable"


def test_cli_control_and_status_work_without_model_adapter(tmp_path, capsys):
    session(tmp_path)
    assert main(["status", "--session", str(tmp_path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["phase"] == "PAUSED_LIMIT_UNKNOWN"
    assert main(["stop", "--session", str(tmp_path), "--command-id", "cli-stop"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "submitted"


def test_closed_stdout_does_not_change_control_result(tmp_path, monkeypatch):
    session(tmp_path)

    class Closed(io.StringIO):
        def write(self, value):
            raise BrokenPipeError

    monkeypatch.setattr("sys.stdout", Closed())
    assert main(["stop", "--session", str(tmp_path), "--command-id", "closed-pipe"]) == 0
    assert Control(tmp_path, Clock()).poll()[0]["command_id"] == "closed-pipe"


def test_cli_retry_with_same_command_id_reuses_pending_request(tmp_path, capsys):
    session(tmp_path)
    args = ["stop", "--session", str(tmp_path), "--command-id", "repeat"]
    assert main(args) == 0
    capsys.readouterr()
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] in {"submitted", "received"}


def test_stop_replay_after_crash_before_ack_is_idempotent(tmp_path):
    store = session(tmp_path)
    submit_command(tmp_path, command("crash-stop", round_no=2))

    class CrashAck(Control):
        def acknowledge(self, value, outcome):
            raise SystemExit("lost after apply")

    class NoWorkers:
        def cancel(self, job_id):
            pass

    class EngineClock(Clock):
        def monotonic(self):
            return 0.0

    with pytest.raises(SystemExit):
        Engine(store, NoWorkers(), CrashAck(tmp_path, EngineClock()), EngineClock()).tick()
    recovered = Engine(
        SessionStore(tmp_path), NoWorkers(), Control(tmp_path, EngineClock()), EngineClock()
    ).tick()
    assert recovered["phase"] == "STOPPED"
    assert submit_command(tmp_path, command("crash-stop", round_no=2))["status"] == "applied"
