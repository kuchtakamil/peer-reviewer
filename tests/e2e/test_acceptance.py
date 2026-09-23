from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from peer_reviewer.cli import main
from peer_reviewer.debate import version_id
from peer_reviewer.store import content_hash
from peer_reviewer.worker import Worker


def write_config(path: Path, *, max_rounds=5, veto_seconds=0):
    path.write_text(
        f"""
[session]
max_rounds = {max_rounds}
veto_seconds = {veto_seconds}
criteria = ["correctness"]
[limits]
minimum_remaining_percent = 20
max_age_seconds = 60
[process]
source_max_bytes = 65536
prompt_max_bytes = 524288
output_max_bytes = 1048576
timeout_seconds = 30
[reviewers.claude]
model = "fixture-a"
effort = "high"
cli_version = "fixture"
account_fingerprint = "claude:fixture"
model_bucket = "subscription"
[reviewers.codex]
model = "fixture-b"
effort = "high"
cli_version = "fixture"
account_fingerprint = "codex:fixture"
model_bucket = "subscription"
""".strip()
        + "\n",
        encoding="utf-8",
    )


ACCEPTED = {
    "problem": "Unqualified claim",
    "fix": "Qualify the claim",
    "resolution": "accepted",
    "rationale": "The qualification makes it testable",
    "duplicate_of": None,
    "closes": [],
}
INITIAL_REJECTED = {
    "problem": "Possible missing condition",
    "fix": "Add the condition",
    "resolution": "accepted",
    "rationale": "Initially appears absent",
    "duplicate_of": None,
    "closes": [],
}
DUPLICATE = {
    "problem": "Same unqualified claim",
    "fix": "",
    "resolution": "duplicate",
    "rationale": "Same root cause as A-1",
    "duplicate_of": "A-1",
    "closes": [],
}


def issue(issue_id, author, problem):
    return {
        "id": issue_id,
        "author": author,
        "anchor": {"line_start": 1, "line_end": 1, "quote": "Claim."},
        "problem": problem,
        "significance": "Material",
        "reasoning": "Fixture reasoning",
        "suggested_fix": "Fixture fix",
    }


def position(reviewer, round_no, issue_id, action, reference, suffix, reason):
    return {
        "issue_id": issue_id,
        "action": action,
        "version_ref": reference,
        "argument_id": f"{reviewer}-r{round_no}-{suffix}",
        "reason": reason,
        "responds_to": [],
        "evidence": [],
    }


def base_turn(request):
    expected = request["expected"]
    return {
        "schema_version": 1,
        **expected,
        "review_complete": True,
        "new_issues": [],
        "proposals": [],
        "positions": [],
    }


class FixtureAdapter:
    def __init__(self, provider, mode="acceptance", *, limit_confidence="verified"):
        self.provider = provider
        self.mode = mode
        self.limit_confidence = limit_confidence
        self.used = 10.0
        self.reset_delay = 300.0
        self.review_delay = 0.0

    def read_limits(self):
        now = datetime.now(timezone.utc)
        return {
            "provider": self.provider,
            "account_fingerprint": f"{self.provider}:fixture",
            "model_bucket": "subscription",
            "observed_at": now.isoformat(),
            "source": "fixture",
            "confidence": self.limit_confidence,
            "five_hour": None
            if self.limit_confidence != "verified"
            else {
                "used_percent": self.used,
                "resets_at": (now + timedelta(seconds=self.reset_delay)).isoformat(),
                "window_seconds": 18000,
            },
            "other_blockers": [] if self.limit_confidence == "verified" else ["fixture unavailable"],
        }

    def review(self, request):
        if self.review_delay:
            request["_cancel_event"].wait(self.review_delay)
        turn = base_turn(request)
        reviewer = request["reviewer"]
        round_no = request["expected"]["round_no"]
        state = request["state"]
        if self.mode == "dispute":
            if round_no == 1 and reviewer == "A":
                turn["new_issues"] = [issue("A-1", "A", "Persistent dispute")]
                turn["proposals"] = [{"local_ref": "a1", "issue_id": "A-1", "payload": ACCEPTED}]
                turn["positions"] = [position("A", 1, "A-1", "propose", "a1", 1, "initial")]
            elif round_no > 1:
                reference = next(iter(state["versions"]))
                action = "accept" if reviewer == "A" else "oppose"
                turn["positions"] = [
                    position(reviewer, round_no, "A-1", action, reference, 1, "stable dispute")
                ]
            return turn

        if round_no == 1:
            if reviewer == "A":
                turn["new_issues"] = [
                    issue("A-1", "A", "Unqualified claim"),
                    issue("A-2", "A", "Possible missing condition"),
                ]
                turn["proposals"] = [
                    {"local_ref": "a1", "issue_id": "A-1", "payload": ACCEPTED},
                    {"local_ref": "a2", "issue_id": "A-2", "payload": INITIAL_REJECTED},
                ]
                turn["positions"] = [
                    position("A", 1, "A-1", "propose", "a1", 1, "initial finding"),
                    position("A", 1, "A-2", "propose", "a2", 2, "initial finding"),
                ]
            else:
                turn["new_issues"] = [issue("B-1", "B", "Same unqualified claim")]
                turn["proposals"] = [{"local_ref": "b1", "issue_id": "B-1", "payload": DUPLICATE}]
                turn["positions"] = [position("B", 1, "B-1", "propose", "b1", 1, "initial duplicate")]
            return turn

        references = {
            item["issue_id"]: reference for reference, item in state["versions"].items()
        }
        if round_no == 2 and reviewer == "A":
            rejected = {
                "problem": "Possible missing condition",
                "fix": "",
                "resolution": "rejected",
                "rationale": "Withdrawn because B-r2-2 identified the condition in the source",
                "duplicate_of": None,
                "closes": [references["A-2"]],
            }
            turn["proposals"] = [{"local_ref": "reject-a2", "issue_id": "A-2", "payload": rejected}]
            turn["positions"] = [
                position("A", 2, "A-1", "accept", references["A-1"], 1, "confirmed"),
                position("A", 2, "A-2", "propose", "reject-a2", 2, "counterargument accepted"),
                position("A", 2, "B-1", "accept", references["B-1"], 3, "duplicate confirmed"),
            ]
            return turn
        if round_no == 2:
            turn["positions"] = [
                position("B", 2, "A-1", "accept", references["A-1"], 1, "confirmed"),
                position("B", 2, "A-2", "oppose", references["A-2"], 2, "condition already present"),
                position("B", 2, "B-1", "accept", references["B-1"], 3, "duplicate confirmed"),
            ]
            return turn
        rejected_ref = next(
            reference
            for reference, item in state["versions"].items()
            if item["issue_id"] == "A-2" and item["payload"]["resolution"] == "rejected"
        )
        turn["positions"] = [
            position(reviewer, 3, "A-1", "accept", references["A-1"], 1, "confirmed"),
            position(reviewer, 3, "A-2", "accept", rejected_ref, 2, "withdrawal confirmed"),
            position(reviewer, 3, "B-1", "accept", references["B-1"], 3, "duplicate confirmed"),
        ]
        return turn


class Harness:
    def __init__(self, tmp_path, monkeypatch, *, mode="acceptance", max_rounds=5, veto_seconds=0, unknown=False):
        self.root = tmp_path
        self.sessions = tmp_path / "sessions"
        self.session = self.sessions / "s-001"
        self.mailboxes = tmp_path / "mailboxes"
        self.config = tmp_path / "reviewer.toml"
        self.source = tmp_path / "source.md"
        self.source.write_text("Claim.\n", encoding="utf-8")
        write_config(self.config, max_rounds=max_rounds, veto_seconds=veto_seconds)
        monkeypatch.setenv("PEER_REVIEWER_CONFIG", str(self.config))
        monkeypatch.setenv("PEER_REVIEWER_MAILBOXES", str(self.mailboxes))
        monkeypatch.setenv("PEER_REVIEWER_SESSIONS", str(self.sessions))
        assert main(["start", str(self.source), "--session", str(self.session)]) == 0
        confidence = "unknown" if unknown else "verified"
        self.adapters = {
            "A": FixtureAdapter("claude", mode, limit_confidence=confidence),
            "B": FixtureAdapter("codex", mode, limit_confidence=confidence),
        }
        self.stop = threading.Event()
        self.threads = []
        for reviewer, provider in (("A", "claude"), ("B", "codex")):
            worker = Worker(
                reviewer,
                self.adapters[reviewer],
                self.mailboxes / provider / "inbox",
                self.mailboxes / provider / "outbox",
                tmp_path / f"worker-{reviewer}",
                poll_seconds=0.001,
            )
            thread = threading.Thread(target=self._worker_loop, args=(worker,), daemon=True)
            thread.start()
            self.threads.append(thread)

    def _worker_loop(self, worker):
        while not self.stop.is_set():
            if not worker.run_once():
                time.sleep(0.001)

    def serve(self):
        return main(["serve", "--config", str(self.config), "--sessions", str(self.sessions)])

    def close(self):
        self.stop.set()
        for thread in self.threads:
            thread.join(1)


@pytest.mark.docker
def test_three_round_acceptance_uses_public_cli_and_preserves_source(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    original = harness.source.read_bytes()
    try:
        assert harness.serve() == 0
        state = json.loads((harness.session / "runtime" / "state.json").read_text())
        assert state["outcome"] == "CONSENSUS"
        assert state["last_completed_round"] == 3
        assert len(list((harness.session / "rounds").glob("[0-9][0-9][0-9][0-9]"))) == 3
        assert (harness.session / "source.txt").read_bytes() == original
        report = (harness.session / "report.md").read_text()
        assert "A-1" in report and "A-2" in report and "B-1 -> A-1" in report
        assert "B-r2-2" in report
    finally:
        harness.close()


@pytest.mark.docker
def test_five_round_dispute_never_submits_round_six(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch, mode="dispute", max_rounds=5)
    try:
        assert harness.serve() == 0
        state = json.loads((harness.session / "runtime" / "state.json").read_text())
        assert state["outcome"] == "NO_CONSENSUS"
        assert state["last_completed_round"] == 5
        round_dirs = sorted((harness.session / "rounds").glob("[0-9][0-9][0-9][0-9]"))
        assert [path.name for path in round_dirs] == ["0001", "0002", "0003", "0004", "0005"]
        assert not (harness.session / "rounds" / "0006").exists()
    finally:
        harness.close()


@pytest.mark.docker
def test_unknown_limits_do_no_inference_and_stop_remains_available(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch, unknown=True)
    service = threading.Thread(target=harness.serve, daemon=True)
    service.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state_path = harness.session / "runtime" / "state.json"
            if state_path.exists() and json.loads(state_path.read_text()).get("phase") == "PAUSED_LIMIT_UNKNOWN":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("service did not enter unknown-limit pause")
        assert not list((harness.session / "rounds").glob("[0-9][0-9][0-9][0-9]"))
        assert main(["stop", "--session", str(harness.session), "--command-id", "stop-unknown"]) == 0
        service.join(5)
        assert not service.is_alive()
        state = json.loads((harness.session / "runtime" / "state.json").read_text())
        assert state["outcome"] == "STOPPED"
    finally:
        harness.close()


@pytest.mark.docker
def test_low_limit_waits_for_reset_then_finishes_automatically(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.adapters["A"].used = 81.0
    harness.adapters["A"].reset_delay = 0.1
    service = threading.Thread(target=harness.serve, daemon=True)
    service.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state_path = harness.session / "runtime" / "state.json"
            if state_path.exists() and json.loads(state_path.read_text()).get("phase") == "PAUSED_LIMIT_LOW":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("service did not enter low-limit pause")
        harness.adapters["A"].used = 10.0
        service.join(10)
        assert not service.is_alive()
        state = json.loads((harness.session / "runtime" / "state.json").read_text())
        assert state["outcome"] == "CONSENSUS"
    finally:
        harness.close()


@pytest.mark.docker
def test_engine_restart_during_b_cancels_old_attempt_before_stop(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    harness.adapters["B"].review_delay = 5.0
    command = [
        sys.executable,
        "-m",
        "peer_reviewer.cli",
        "serve",
        "--config",
        str(harness.config),
        "--sessions",
        str(harness.sessions),
    ]
    first = subprocess.Popen(command, env=os.environ.copy())
    second = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state_path = harness.session / "runtime" / "state.json"
            if state_path.exists() and json.loads(state_path.read_text()).get("phase") == "REVIEWING_B":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("engine did not reach reviewer B")
        first.kill()
        first.wait(3)
        second = subprocess.Popen(command, env=os.environ.copy())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = json.loads((harness.session / "runtime" / "state.json").read_text())
            if state.get("phase") == "RETRY_WAIT":
                break
            time.sleep(0.01)
        else:
            raise AssertionError(f"restarted engine did not cancel into retry wait: {state}")
        assert not list((harness.session / "rounds").glob("[0-9][0-9][0-9][0-9]"))
        assert main(["stop", "--session", str(harness.session), "--command-id", "stop-after-crash"]) == 0
        second.wait(5)
        state = json.loads((harness.session / "runtime" / "state.json").read_text())
        assert state["outcome"] == "STOPPED"
        assert state["attempts"] == {"1": 1}
    finally:
        if first.poll() is None:
            first.kill()
        if second is not None and second.poll() is None:
            second.kill()
        harness.close()
