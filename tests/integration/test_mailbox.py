from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from peer_reviewer.mailbox import MailboxConflict, MailboxError, publish, receive
from peer_reviewer.worker import Worker, WorkerClient


def job(job_id: str = "s1-r1-a1-A", *, kind: str = "review", attempt_id: str = "a1") -> dict:
    return {
        "job_id": job_id,
        "session_id": "s1",
        "round_no": 1 if kind == "review" else None,
        "attempt_id": attempt_id if kind == "review" else None,
        "reviewer": "A",
        "kind": kind,
        "input_state_hash": "sha256:" + "1" * 64 if kind == "review" else None,
        "payload": {"value": 1},
        "deadline": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
    }


def test_unpublished_message_is_invisible(tmp_path):
    (tmp_path / "job.part").write_text('{"job_id":', encoding="utf-8")
    assert receive(tmp_path) == []
    publish(tmp_path, {"job_id": "s1-r1-a1-A", "kind": "limits"})
    assert receive(tmp_path) == [{"job_id": "s1-r1-a1-A", "kind": "limits"}]


def test_publish_is_idempotent_and_rejects_changed_duplicate(tmp_path):
    message = {"job_id": "same", "kind": "limits"}
    first = publish(tmp_path, message)
    assert publish(tmp_path, message) == first
    with pytest.raises(MailboxConflict):
        publish(tmp_path, {**message, "kind": "review"})


def test_receive_rejects_corrupt_committed_json(tmp_path):
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(MailboxError):
        receive(tmp_path)


class RecordingAdapter:
    def __init__(self):
        self.calls = 0

    def review(self, value):
        self.calls += 1
        return {"reviewed": value["value"]}

    def read_limits(self):
        self.calls += 1
        return {"confidence": "verified"}


def test_worker_does_not_repeat_completed_job_after_restart(tmp_path):
    inbox, outbox, state = tmp_path / "in", tmp_path / "out", tmp_path / "state"
    adapter = RecordingAdapter()
    publish(inbox, job())

    assert Worker("A", adapter, inbox, outbox, state).run_once() is True
    assert Worker("A", adapter, inbox, outbox, state).run_once() is False
    assert adapter.calls == 1
    response = receive(outbox)[0]
    assert response["job_id"] == "s1-r1-a1-A"
    assert response["attempt_id"] == "a1"
    assert response["ok"] is True


def test_restart_reconciles_response_published_before_completion_record(tmp_path):
    inbox, outbox, state = tmp_path / "in", tmp_path / "out", tmp_path / "state"
    value = job()
    publish(inbox, value)
    worker = Worker("A", RecordingAdapter(), inbox, outbox, state)
    from peer_reviewer.worker import _response, _write_json

    _write_json(worker._running_path(value["job_id"]), value)
    successful = _response(value, ok=True, result={"reviewed": 1}, error_code=None)
    publish(outbox, successful)
    replacement = RecordingAdapter()
    assert Worker("A", replacement, inbox, outbox, state).run_once() is True
    assert replacement.calls == 0
    assert receive(outbox) == [successful]


class CrashingAdapter(RecordingAdapter):
    def review(self, value):
        raise SystemExit("simulated worker loss")


def test_restart_reports_interrupted_instead_of_restarting_inference(tmp_path):
    inbox, outbox, state = tmp_path / "in", tmp_path / "out", tmp_path / "state"
    publish(inbox, job())
    with pytest.raises(SystemExit):
        Worker("A", CrashingAdapter(), inbox, outbox, state).run_once()

    replacement = RecordingAdapter()
    assert Worker("A", replacement, inbox, outbox, state).run_once() is True
    assert replacement.calls == 0
    response = receive(outbox)[0]
    assert response["ok"] is False
    assert response["error_code"] == "interrupted"


def test_expired_job_never_invokes_adapter(tmp_path):
    inbox, outbox, state = tmp_path / "in", tmp_path / "out", tmp_path / "state"
    adapter = RecordingAdapter()
    expired = job()
    expired["deadline"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    publish(inbox, expired)
    Worker("A", adapter, inbox, outbox, state).run_once()
    assert adapter.calls == 0
    assert receive(outbox)[0]["error_code"] == "deadline"


class CancellableAdapter(RecordingAdapter):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.stopped = threading.Event()

    def review(self, value):
        self.calls += 1
        self.started.set()
        assert value["_cancel_event"].wait(2)
        self.stopped.set()
        return {"too_late": True}


def test_worker_acknowledges_cancel_only_after_active_work_stops(tmp_path):
    inbox, outbox, state = tmp_path / "in", tmp_path / "out", tmp_path / "state"
    adapter = CancellableAdapter()
    publish(inbox, job())
    worker = Worker("A", adapter, inbox, outbox, state, poll_seconds=0.005)
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    assert adapter.started.wait(1)
    cancel = job("cancel-1", kind="cancel")
    cancel["payload"] = {"target_job_id": "s1-r1-a1-A"}
    publish(inbox, cancel)
    thread.join(2)
    assert not thread.is_alive()
    responses = {item["job_id"]: item for item in receive(outbox)}
    assert adapter.stopped.is_set()
    assert responses["s1-r1-a1-A"]["error_code"] == "cancelled"
    assert responses["cancel-1"]["ok"] is True


def test_active_job_is_cancelled_when_its_deadline_passes(tmp_path):
    inbox, outbox, state = tmp_path / "in", tmp_path / "out", tmp_path / "state"
    adapter = CancellableAdapter()
    expiring = job()
    expiring["deadline"] = (datetime.now(timezone.utc) + timedelta(seconds=0.05)).isoformat()
    publish(inbox, expiring)
    Worker("A", adapter, inbox, outbox, state, poll_seconds=0.005).run_once()
    response = receive(outbox)[0]
    assert adapter.stopped.is_set()
    assert response["ok"] is False
    assert response["error_code"] == "deadline"


def test_cancel_of_completed_target_acknowledges_proven_quiescence(tmp_path):
    inbox, outbox, state = tmp_path / "in", tmp_path / "out", tmp_path / "state"
    target = job()
    publish(inbox, target)
    worker = Worker("A", RecordingAdapter(), inbox, outbox, state)
    worker.run_once()
    cancel = job("cancel-finished", kind="cancel")
    cancel["payload"] = {"target_job_id": target["job_id"]}
    publish(inbox, cancel)
    assert worker.run_once() is True
    responses = {item["job_id"]: item for item in receive(outbox)}
    assert responses["cancel-finished"]["ok"] is True
    assert responses["cancel-finished"]["result"]["already_stopped"] is True


def test_cancel_of_never_seen_target_acknowledges_and_prevents_late_start(tmp_path):
    inbox, outbox, state = tmp_path / "in", tmp_path / "out", tmp_path / "state"
    adapter = RecordingAdapter()
    worker = Worker("A", adapter, inbox, outbox, state)
    target = job()
    cancel = job("cancel-unseen", kind="cancel")
    cancel["payload"] = {"target_job_id": target["job_id"]}
    publish(inbox, cancel)
    assert worker.run_once() is True
    responses = {item["job_id"]: item for item in receive(outbox)}
    assert responses["cancel-unseen"]["ok"] is True
    assert responses["cancel-unseen"]["result"]["never_started"] is True

    publish(inbox, target)
    assert worker.run_once() is False
    assert adapter.calls == 0


def test_worker_client_rejects_late_attempt_response(tmp_path):
    inbox, outbox = tmp_path / "in", tmp_path / "out"
    client = WorkerClient("A", inbox, outbox, timeout_seconds=0.1)
    current = job(attempt_id="a2")
    client.submit(current)
    publish(
        outbox,
        {
            "job_id": current["job_id"],
            "session_id": "s1",
            "round_no": 1,
            "attempt_id": "a1",
            "reviewer": "A",
            "kind": "review",
            "input_state_hash": current["input_state_hash"],
            "ok": True,
            "result": {},
            "error_code": None,
        },
    )
    assert client.poll(current["job_id"]) is None


def test_clients_cannot_see_partner_mailbox(tmp_path):
    a_in, a_out = tmp_path / "a-in", tmp_path / "a-out"
    b_in, b_out = tmp_path / "b-in", tmp_path / "b-out"
    publish(b_out, {"job_id": "b-secret", "kind": "limits"})
    client_a = WorkerClient("A", a_in, a_out)
    assert client_a.poll("b-secret") is None
    assert receive(b_out)[0]["job_id"] == "b-secret"


def test_invalid_job_is_rejected_before_publish(tmp_path):
    client = WorkerClient("A", tmp_path / "in", tmp_path / "out")
    invalid = job()
    del invalid["deadline"]
    with pytest.raises(MailboxError):
        client.submit(invalid)


def test_provider_env_passes_auth_locations_but_never_api_keys():
    from peer_reviewer.worker import provider_env

    env = provider_env(
        {
            "PATH": "/usr/bin",
            "TZ": "UTC",
            "CLAUDE_CONFIG_DIR": "/auth/claude",
            "CODEX_HOME": "/auth/codex",
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "OPENAI_API_KEY": "sk-secret",
        }
    )
    assert env == {
        "HOME": "/work/home",
        "PATH": "/usr/bin",
        "TZ": "UTC",
        "CLAUDE_CONFIG_DIR": "/auth/claude",
        "CODEX_HOME": "/auth/codex",
    }
