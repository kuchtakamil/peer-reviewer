from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from peer_reviewer.debate import version_id
from peer_reviewer.engine import Engine
from peer_reviewer.store import SessionStore


class Clock:
    def __init__(self):
        self.current = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
        self.elapsed = 0.0

    def now(self):
        return self.current

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.current += timedelta(seconds=seconds)
        self.elapsed += seconds


def sample(provider: str, now: datetime, used=10.0, confidence="verified"):
    return {
        "provider": provider,
        "account_fingerprint": f"{provider}:fixture",
        "model_bucket": "subscription",
        "observed_at": now.isoformat(),
        "source": "fixture",
        "confidence": confidence,
        "five_hour": None
        if confidence != "verified"
        else {
            "used_percent": used,
            "resets_at": (now + timedelta(minutes=5)).isoformat(),
            "window_seconds": 18000,
        },
        "other_blockers": [] if confidence == "verified" else ["unavailable"],
    }


def issue():
    return {
        "id": "A-1",
        "author": "A",
        "anchor": {"line_start": 1, "line_end": 1, "quote": "text"},
        "problem": "P",
        "significance": "S",
        "reasoning": "R",
        "suggested_fix": "F",
    }


PAYLOAD = {
    "problem": "P",
    "fix": "F",
    "resolution": "accepted",
    "rationale": "R",
    "duplicate_of": None,
    "closes": [],
}


def make_turn(job, *, settle_round=3):
    payload = job["payload"]
    state = payload["state"]
    reviewer = job["reviewer"]
    round_no = job["round_no"]
    result = {
        "schema_version": 1,
        "session_id": job["session_id"],
        "round_no": round_no,
        "reviewer": reviewer,
        "input_state_hash": job["input_state_hash"],
        "review_complete": True,
        "new_issues": [],
        "proposals": [],
        "positions": [],
    }
    if round_no == 1 and reviewer == "A":
        result["new_issues"] = [issue()]
        result["proposals"] = [{"local_ref": "v1", "issue_id": "A-1", "payload": PAYLOAD}]
        result["positions"] = [
            {
                "issue_id": "A-1",
                "action": "propose",
                "version_ref": "v1",
                "argument_id": "A-r1-1",
                "reason": "initial",
                "responds_to": [],
                "evidence": [],
            }
        ]
    elif round_no > 1:
        ref = next(iter(state["versions"]))
        action = "accept" if reviewer == "A" or round_no >= settle_round else "oppose"
        result["positions"] = [
            {
                "issue_id": "A-1",
                "action": action,
                "version_ref": ref,
                "argument_id": f"{reviewer}-r{round_no}-1",
                "reason": "stable position",
                "responds_to": [],
                "evidence": [],
            }
        ]
    return result


class Workers:
    def __init__(self, clock, *, settle_round=3):
        self.clock = clock
        self.settle_round = settle_round
        self.limit_values = None
        self.submissions = []
        self.responses = {}
        self.failures = []
        self.cancelled = []
        self.limit_calls = 0

    def limits(self):
        self.limit_calls += 1
        if self.limit_values is not None:
            return copy.deepcopy(self.limit_values)
        return {
            "A": sample("claude", self.clock.now()),
            "B": sample("codex", self.clock.now()),
        }

    def submit(self, job):
        self.submissions.append(copy.deepcopy(job))
        failure = self.failures.pop(0) if self.failures else None
        self.responses[job["job_id"]] = {
            **{key: job[key] for key in (
                "job_id", "session_id", "round_no", "attempt_id", "reviewer", "kind", "input_state_hash"
            )},
            "ok": failure is None,
            "result": None if failure else make_turn(job, settle_round=self.settle_round),
            "error_code": failure,
        }
        return job["job_id"]

    def poll(self, job_id):
        return self.responses.get(job_id)

    def cancel(self, job_id):
        self.cancelled.append(job_id)


class Control:
    def __init__(self):
        self.commands = []

    def poll(self):
        commands, self.commands = self.commands, []
        return commands


def store_for(tmp_path, *, max_rounds=5, veto_seconds=5, minimum_remaining=20, fault=None):
    store = SessionStore(tmp_path, fault=fault)
    store.create(
        b"text\n",
        {
            "session_id": "s-001",
            "session": {"max_rounds": max_rounds, "veto_seconds": veto_seconds, "criteria": ["correct"]},
            "limits": {"max_age_seconds": 60, "minimum_remaining_percent": minimum_remaining},
            "process": {
                "source_max_bytes": 65536,
                "prompt_max_bytes": 524288,
                "output_max_bytes": 1048576,
                "timeout_seconds": 900,
            },
            "reviewers": {
                "claude": {
                    "model": "fixture-a",
                    "cli_version": "fixture",
                    "account_fingerprint": "claude:fixture",
                    "model_bucket": "subscription",
                },
                "codex": {
                    "model": "fixture-b",
                    "cli_version": "fixture",
                    "account_fingerprint": "codex:fixture",
                    "model_bucket": "subscription",
                },
            },
        },
    )
    return store


def run_until(engine, clock, target, limit=100):
    state = {}
    for _ in range(limit):
        state = engine.tick()
        if state["phase"] == "CHECKPOINT":
            clock.advance(6)
        if state["phase"] == target:
            return state
    raise AssertionError(f"did not reach {target}: {state}")


def test_low_limit_pauses_without_starting_and_rechecks_after_reset(tmp_path):
    clock, control = Clock(), Control()
    workers = Workers(clock)
    workers.limit_values = {
        "A": sample("claude", clock.now(), used=81),
        "B": sample("codex", clock.now()),
    }
    engine = Engine(store_for(tmp_path), workers, control, clock)
    assert engine.tick()["phase"] == "PAUSED_LIMIT_LOW"
    assert workers.submissions == []
    clock.advance(306)
    workers.limit_values = None
    assert engine.tick()["phase"] == "REVIEWING_A"


def test_unknown_limit_stays_paused_and_never_inferrs(tmp_path):
    clock = Clock()
    workers = Workers(clock)
    workers.limit_values = {
        "A": sample("claude", clock.now(), confidence="unknown"),
        "B": sample("codex", clock.now()),
    }
    state = Engine(store_for(tmp_path), workers, Control(), clock).tick()
    assert state["phase"] == "PAUSED_LIMIT_UNKNOWN"
    assert workers.submissions == []


def test_engine_uses_configured_remaining_threshold(tmp_path):
    clock = Clock()
    workers = Workers(clock)
    workers.limit_values = {
        "A": sample("claude", clock.now(), used=75),
        "B": sample("codex", clock.now()),
    }
    state = Engine(store_for(tmp_path, minimum_remaining=30), workers, Control(), clock).tick()
    assert state["phase"] == "PAUSED_LIMIT_LOW"


def test_restart_during_pause_preserves_next_check_and_does_not_submit(tmp_path):
    clock = Clock()
    workers = Workers(clock)
    workers.limit_values = {
        "A": sample("claude", clock.now(), confidence="unknown"),
        "B": sample("codex", clock.now()),
    }
    Engine(store_for(tmp_path), workers, Control(), clock).tick()
    before = len(workers.submissions)
    limit_calls = workers.limit_calls
    resumed = Engine(SessionStore(tmp_path), workers, Control(), clock).resume()
    assert resumed["phase"] == "PAUSED_LIMIT_UNKNOWN"
    assert len(workers.submissions) == before
    assert workers.limit_calls == limit_calls


def test_b_failure_after_a_success_schedules_durable_retry_without_commit(tmp_path):
    clock = Clock()
    workers = Workers(clock)
    workers.failures = [None, "timeout"]
    engine = Engine(store_for(tmp_path), workers, Control(), clock)
    engine.tick()
    engine.tick()
    state = engine.tick()
    assert state["phase"] == "RETRY_WAIT"
    assert state["attempts"]["1"] == 1
    assert SessionStore(tmp_path).recover()["last_completed_round"] == 0


def test_fresh_low_limit_before_b_abandons_attempt_without_spending_b(tmp_path):
    clock = Clock()
    workers = Workers(clock)
    engine = Engine(store_for(tmp_path), workers, Control(), clock)
    engine.tick()
    workers.limit_values = {
        "A": sample("claude", clock.now()),
        "B": sample("codex", clock.now(), used=81),
    }
    state = engine.tick()
    assert state["phase"] == "PAUSED_LIMIT_LOW"
    assert [job["reviewer"] for job in workers.submissions] == ["A"]


def test_consensus_in_round_three_and_no_extra_submission(tmp_path):
    clock = Clock()
    workers = Workers(clock, settle_round=3)
    engine = Engine(store_for(tmp_path), workers, Control(), clock)
    state = run_until(engine, clock, "CONSENSUS")
    assert state["last_completed_round"] == 3
    count = len(workers.submissions)
    engine.tick()
    assert len(workers.submissions) == count == 6


def test_checkpoint_event_contains_mechanical_issue_counts(tmp_path):
    clock = Clock()
    workers = Workers(clock)
    engine = Engine(store_for(tmp_path), workers, Control(), clock)
    engine.tick()
    engine.tick()
    engine.tick()
    event = __import__("json").loads(
        (tmp_path / "runtime" / "events" / "checkpoint-0001.json").read_text()
    )
    assert event["counts"] == {"agreed": 0, "rejected": 0, "disputed": 1}


def test_five_disputed_rounds_end_without_round_six(tmp_path):
    clock = Clock()
    workers = Workers(clock, settle_round=99)
    engine = Engine(store_for(tmp_path, max_rounds=5), workers, Control(), clock)
    state = run_until(engine, clock, "NO_CONSENSUS")
    assert state["last_completed_round"] == 5
    assert max(item["round_no"] for item in workers.submissions) == 5


def test_resume_after_crash_post_commit_creates_checkpoint_without_resubmit(tmp_path):
    crashed = False

    def fault(stage):
        nonlocal crashed
        if stage == "after_round_rename" and not crashed:
            crashed = True
            raise SystemExit("power loss")

    clock = Clock()
    workers = Workers(clock)
    engine = Engine(store_for(tmp_path, fault=fault), workers, Control(), clock)
    engine.tick()
    engine.tick()
    with pytest.raises(SystemExit):
        engine.tick()
    assert SessionStore(tmp_path).recover()["last_completed_round"] == 1
    submitted = len(workers.submissions)

    resumed = Engine(SessionStore(tmp_path), workers, Control(), clock)
    state = resumed.resume()
    assert state["phase"] == "CHECKPOINT"
    assert len(workers.submissions) == submitted


def test_restart_reuses_durable_checkpoint_deadline(tmp_path):
    clock = Clock()
    workers = Workers(clock)
    engine = Engine(store_for(tmp_path), workers, Control(), clock)
    engine.tick()
    engine.tick()
    checkpoint = engine.tick()
    deadline = checkpoint["checkpoint"]["deadline"]
    # Simulate loss of the replaceable state view after the durable event.
    SessionStore(tmp_path).write_status(
        {"phase": "READY", "last_completed_round": 0, "attempts": {"1": 1}}
    )
    clock.advance(2)
    resumed = Engine(SessionStore(tmp_path), workers, Control(), clock).resume()
    assert resumed["checkpoint"]["deadline"] == deadline


def test_crash_after_store_state_write_still_finishes_checkpoint_before_round_two(tmp_path):
    crashed = False

    def fault(stage):
        nonlocal crashed
        if stage == "after_state_write" and not crashed:
            crashed = True
            raise SystemExit("power loss")

    clock = Clock()
    workers = Workers(clock)
    engine = Engine(store_for(tmp_path, max_rounds=1, fault=fault), workers, Control(), clock)
    engine.tick()
    engine.tick()
    with pytest.raises(SystemExit):
        engine.tick()
    resumed = Engine(SessionStore(tmp_path), workers, Control(), clock).resume()
    assert resumed["phase"] == "CHECKPOINT"
    assert max(job["round_no"] for job in workers.submissions) == 1


def test_attempt_is_durable_before_worker_submission(tmp_path):
    class LosingWorkers(Workers):
        def submit(self, job):
            raise SystemExit("engine lost while publishing")

    clock = Clock()
    with pytest.raises(SystemExit):
        Engine(store_for(tmp_path), LosingWorkers(clock), Control(), clock).tick()
    state = __import__("json").loads((tmp_path / "runtime" / "state.json").read_text())
    assert state["attempts"] == {"1": 1}
    assert state["active"]["job_id"] == "s-001-r1-r1-g0-a1-A"


def test_stop_during_review_cancels_pending_job_and_never_commits(tmp_path):
    clock, control = Clock(), Control()
    workers = Workers(clock)
    engine = Engine(store_for(tmp_path), workers, control, clock)
    assert engine.tick()["phase"] == "REVIEWING_A"
    control.commands.append({"kind": "stop", "command_id": "stop-1"})
    state = engine.tick()
    assert state["phase"] == "STOPPED"
    assert workers.cancelled == [workers.submissions[0]["job_id"]]
    assert SessionStore(tmp_path).recover()["last_completed_round"] == 0


def test_restart_cancels_active_job_before_new_attempt(tmp_path):
    clock = Clock()
    workers = Workers(clock)
    engine = Engine(store_for(tmp_path), workers, Control(), clock)
    assert engine.tick()["phase"] == "REVIEWING_A"
    replacement = Workers(clock)
    resumed = Engine(SessionStore(tmp_path), replacement, Control(), clock)
    state = resumed.resume()
    assert replacement.cancelled == [workers.submissions[0]["job_id"]]
    assert state["phase"] == "RETRY_WAIT"


def test_missing_worker_response_hits_deadline_and_schedules_retry(tmp_path):
    class SilentWorkers(Workers):
        def poll(self, job_id):
            return None

    clock = Clock()
    workers = SilentWorkers(clock)
    engine = Engine(store_for(tmp_path), workers, Control(), clock)
    state = engine.tick()
    clock.advance(901)
    state = engine.tick()
    assert state["phase"] == "RETRY_WAIT"
    assert len(workers.cancelled) == 1


def test_attempt_identity_is_unique_across_rounds_and_human_retry_grants(tmp_path):
    clock = Clock()
    workers = Workers(clock, settle_round=99)
    engine = Engine(store_for(tmp_path), workers, Control(), clock)
    run_until(engine, clock, "NO_PROGRESS")
    attempt_ids = [job["attempt_id"] for job in workers.submissions]
    assert len(set(attempt_ids)) >= 3


def test_human_retry_grant_starts_with_a_new_attempt_identity(tmp_path):
    clock, control = Clock(), Control()
    workers = Workers(clock)
    workers.failures = ["timeout"] * 4
    engine = Engine(store_for(tmp_path), workers, control, clock)
    for expected in range(1, 5):
        engine.tick()
        state = engine.tick()
        if expected < 4:
            clock.advance((10, 30, 90)[expected - 1])
    assert state["error_code"] == "attempts_exhausted"
    previous_ids = {job["attempt_id"] for job in workers.submissions}

    control.commands.append({"kind": "resume", "command_id": "grant-1"})
    assert engine.tick()["phase"] == "READY"
    assert engine.tick()["phase"] == "REVIEWING_A"
    assert workers.submissions[-1]["attempt_id"] == "r1-g1-a1"
    assert workers.submissions[-1]["attempt_id"] not in previous_ids


def test_four_failed_attempts_exhaust_round_budget_across_restarts(tmp_path):
    clock = Clock()
    workers = Workers(clock)
    workers.failures = ["timeout"] * 4
    engine = Engine(store_for(tmp_path), workers, Control(), clock)
    for expected in range(1, 5):
        engine.tick()  # gate + submit A
        state = engine.tick()  # failure
        if expected < 4:
            assert state["phase"] == "RETRY_WAIT"
            clock.advance((10, 30, 90)[expected - 1])
        else:
            assert state["phase"] == "ERROR"
    assert state["attempts"]["1"] == 4


def test_nontransient_failure_writes_durable_error_event(tmp_path):
    clock = Clock()
    workers = Workers(clock)
    workers.failures = ["authentication"]
    engine = Engine(store_for(tmp_path), workers, Control(), clock)
    engine.tick()
    state = engine.tick()
    assert state["phase"] == "ERROR"
    events = list((tmp_path / "runtime" / "events").glob("error-*.json"))
    assert len(events) == 1
    assert __import__("json").loads(events[0].read_text())["error_code"] == "authentication"


def test_limit_sample_identity_must_match_immutable_reviewer_config(tmp_path):
    clock = Clock()
    workers = Workers(clock)
    workers.limit_values = {
        "A": sample("claude", clock.now()),
        "B": sample("codex", clock.now()),
    }
    workers.limit_values["A"]["account_fingerprint"] = "wrong-account"
    state = Engine(store_for(tmp_path), workers, Control(), clock).tick()
    assert state["phase"] == "PAUSED_LIMIT_UNKNOWN"


def test_identical_material_states_signal_no_progress(tmp_path):
    clock = Clock()
    workers = Workers(clock, settle_round=99)
    engine = Engine(store_for(tmp_path), workers, Control(), clock)
    state = run_until(engine, clock, "NO_PROGRESS")
    assert state["last_completed_round"] == 3
    assert state["outcome"] is None
