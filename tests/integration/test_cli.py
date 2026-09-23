from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from peer_reviewer.cli import _engine_guard, main
from peer_reviewer.store import SessionStore, content_hash


def write_config(path: Path, *, source_max=1024):
    path.write_text(
        f"""
[session]
max_rounds = 5
veto_seconds = 0
criteria = ["correctness"]

[limits]
minimum_remaining_percent = 20
max_age_seconds = 60

[process]
source_max_bytes = {source_max}
prompt_max_bytes = 524288
output_max_bytes = 1048576
timeout_seconds = 900

[reviewers.claude]
model = "fixture-claude"
effort = "high"
cli_version = "fixture"
account_fingerprint = "claude:fixture"
model_bucket = "subscription"

[reviewers.codex]
model = "fixture-codex"
effort = "high"
cli_version = "fixture"
account_fingerprint = "codex:fixture"
model_bucket = "subscription"
""".strip()
        + "\n",
        encoding="utf-8",
    )


def start(tmp_path, monkeypatch, *, content="hello\n", source_max=1024):
    config = tmp_path / "reviewer.toml"
    source = tmp_path / "input.md"
    session = tmp_path / "sessions" / "s-001"
    write_config(config, source_max=source_max)
    source.write_text(content, encoding="utf-8")
    monkeypatch.setenv("PEER_REVIEWER_CONFIG", str(config))
    assert main(["start", str(source), "--session", str(session)]) == 0
    return source, session, config


def test_start_copies_utf8_source_hash_and_config_without_git(tmp_path, monkeypatch):
    source, session, _ = start(tmp_path, monkeypatch)
    metadata = json.loads((session / "session.json").read_text())
    assert (session / "source.txt").read_bytes() == source.read_bytes()
    assert metadata["source_hash"] == content_hash(source.read_bytes())
    assert metadata["config"]["reviewers"]["claude"]["model"] == "fixture-claude"
    assert not (tmp_path / ".git").exists()
    marker = json.loads((session.parent / ".peer-reviewer-active.json").read_text())
    assert marker == {"session": "s-001"}


def test_start_refuses_existing_session_and_second_active_session(tmp_path, monkeypatch):
    source, session, _ = start(tmp_path, monkeypatch)
    assert main(["start", str(source), "--session", str(session)]) == 2
    other = tmp_path / "sessions" / "s-002"
    assert main(["start", str(source), "--session", str(other)]) == 2
    assert not other.exists()


def test_start_rejects_directory_invalid_utf8_and_oversized_input(tmp_path, monkeypatch):
    config = tmp_path / "reviewer.toml"
    write_config(config, source_max=3)
    monkeypatch.setenv("PEER_REVIEWER_CONFIG", str(config))
    assert main(["start", str(tmp_path), "--session", str(tmp_path / "s-dir")]) == 2
    invalid = tmp_path / "invalid.txt"
    invalid.write_bytes(b"\xff")
    assert main(["start", str(invalid), "--session", str(tmp_path / "s-invalid")]) == 2
    large = tmp_path / "large.txt"
    large.write_text("four", encoding="utf-8")
    assert main(["start", str(large), "--session", str(tmp_path / "s-large")]) == 2


def test_report_and_accept_record_separate_human_decision(tmp_path, monkeypatch):
    _, session, _ = start(tmp_path, monkeypatch)
    store = SessionStore(session)
    store.write_status(
        {
            "phase": "STOPPED",
            "outcome": "STOPPED",
            "last_completed_round": 0,
            "debate_state": store.recover()["state"],
            "updated_at": "2026-09-20T10:00:00Z",
        }
    )
    assert main(["report", "--session", str(session)]) == 0
    report = (session / "report.md").read_bytes()
    assert b"PARTIAL" in report
    report_events = list((session / "runtime" / "events").glob("report-*.json"))
    assert len(report_events) == 1
    assert json.loads(report_events[0].read_text())["report_hash"] == content_hash(report)
    assert main(["accept", "--session", str(session)]) == 0
    acceptance = json.loads((session / "runtime" / "acceptance.json").read_text())
    assert acceptance["report_hash"] == content_hash(report)
    assert json.loads((session / "runtime" / "state.json").read_text())["outcome"] == "STOPPED"


def test_resume_terminal_session_only_rebuilds_views(tmp_path, monkeypatch):
    _, session, _ = start(tmp_path, monkeypatch)
    store = SessionStore(session)
    recovered = store.recover()
    store.write_status(
        {
            "phase": "CONSENSUS",
            "outcome": "CONSENSUS",
            "last_completed_round": 0,
            "debate_state": recovered["state"],
            "updated_at": "2026-09-20T10:00:00Z",
        }
    )
    assert main(["resume", "--session", str(session), "--command-id", "terminal"]) == 0
    assert not list((session / "runtime" / "commands" / "requests").glob("*.json"))
    assert (session / "report.md").is_file()


def test_resume_attempts_exhausted_publishes_retry_grant(tmp_path, monkeypatch, capsys):
    _, session, _ = start(tmp_path, monkeypatch)
    SessionStore(session).write_status(
        {
            "phase": "ERROR",
            "outcome": "ERROR",
            "error_code": "attempts_exhausted",
            "last_completed_round": 0,
            "attempts": {"1": 4},
            "updated_at": "2026-09-20T10:00:00Z",
        }
    )
    assert main(["resume", "--session", str(session), "--command-id", "grant-1"]) == 0
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["status"] == "submitted"
    requests = list((session / "runtime" / "commands" / "requests").glob("*.json"))
    assert len(requests) == 1
    assert json.loads(requests[0].read_text())["kind"] == "resume"


def test_doctor_reports_unverified_subscription_and_never_api_fallback(tmp_path, capsys):
    config = tmp_path / "reviewer.toml"
    write_config(config)
    assert main(["doctor", "--config", str(config), "--sessions", str(tmp_path / "sessions")]) == 1
    result = json.loads(capsys.readouterr().out)
    assert any("limit read unavailable" in item for item in result["issues"])
    assert any("CLI version" in item for item in result["issues"])
    assert "API" not in json.dumps(result.get("actions", []))


def test_doctor_reports_unpinned_model_placeholder(tmp_path, capsys):
    config = tmp_path / "reviewer.toml"
    write_config(config)
    config.write_text(
        config.read_text().replace('"fixture-codex"', '"PIN_AFTER_LIVE_PREFLIGHT"'), encoding="utf-8"
    )
    assert main(["doctor", "--config", str(config), "--sessions", str(tmp_path / "sessions")]) == 1
    issues = json.loads(capsys.readouterr().out)["issues"]
    assert "codex model is not pinned: PIN_AFTER_LIVE_PREFLIGHT" in issues
    assert not any(item.startswith("claude model") for item in issues)


class DoctorAdapter:
    def __init__(self, provider, confidence="verified"):
        self.provider = provider
        self.confidence = confidence

    def read_limits(self):
        now = datetime.now(timezone.utc)
        return {
            "provider": self.provider,
            "account_fingerprint": f"{self.provider}:fixture",
            "model_bucket": "subscription",
            "observed_at": now.isoformat(),
            "source": "fixture",
            "confidence": self.confidence,
            "five_hour": {"used_percent": 10.0, "resets_at": (now + timedelta(hours=1)).isoformat(), "window_seconds": 18000}
            if self.confidence == "verified"
            else None,
            "other_blockers": [] if self.confidence == "verified" else ["account mismatch"],
        } | ({} if self.confidence == "verified" else {"observed_account_fingerprint": f"{self.provider}:observed"})


def run_doctor_with_workers(tmp_path, monkeypatch, capsys, *, versions, confidence="verified"):
    from peer_reviewer.worker import Worker

    mailboxes = tmp_path / "mailboxes"
    monkeypatch.setenv("PEER_REVIEWER_MAILBOXES", str(mailboxes))
    stop = threading.Event()
    threads = []
    for reviewer, provider in (("A", "claude"), ("B", "codex")):
        for box in ("inbox", "outbox"):  # compose volumes exist before any message
            (mailboxes / provider / box).mkdir(parents=True)
        worker = Worker(
            reviewer,
            DoctorAdapter(provider, confidence),
            mailboxes / provider / "inbox",
            mailboxes / provider / "outbox",
            tmp_path / f"worker-{provider}",
            poll_seconds=0.001,
            cli_version=versions[provider],
        )

        def loop(worker=worker):
            while not stop.is_set():
                if not worker.run_once():
                    time.sleep(0.001)

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        threads.append(thread)
    config = tmp_path / "reviewer.toml"
    write_config(config)
    try:
        code = main(["doctor", "--config", str(config), "--sessions", str(tmp_path / "sessions")])
    finally:
        stop.set()
        for thread in threads:
            thread.join(1)
    return code, json.loads(capsys.readouterr().out)


def test_doctor_passes_with_verified_worker_readings_and_pinned_versions(tmp_path, monkeypatch, capsys):
    code, result = run_doctor_with_workers(
        tmp_path, monkeypatch, capsys, versions={"claude": "fixture", "codex": "fixture"}
    )
    assert result["issues"] == []
    assert code == 0


def test_doctor_reports_version_drift_and_observed_account(tmp_path, monkeypatch, capsys):
    code, result = run_doctor_with_workers(
        tmp_path,
        monkeypatch,
        capsys,
        versions={"claude": "9.9.9", "codex": "fixture"},
        confidence="unknown",
    )
    assert code == 1
    assert "claude CLI version mismatch: expected fixture, got 9.9.9" in result["issues"]
    assert any("observed account fingerprint claude:observed" in item for item in result["issues"])


def test_only_one_engine_can_own_a_session(tmp_path, monkeypatch):
    _, session, _ = start(tmp_path, monkeypatch)
    with _engine_guard(session):
        with pytest.raises(ValueError, match="already serves"):
            with _engine_guard(session):
                pass
