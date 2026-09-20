import copy
import json
from pathlib import Path

import pytest

from peer_reviewer.debate import initial_state, reduce_round
from peer_reviewer.store import (
    IntegrityError,
    RoundConflict,
    SessionLocked,
    SessionStore,
    SourceChanged,
    content_hash,
)


def empty_turn(reviewer: str, round_no: int, input_hash: str) -> dict:
    return {
        "schema_version": 1,
        "session_id": "s-001",
        "round_no": round_no,
        "reviewer": reviewer,
        "input_state_hash": input_hash,
        "review_complete": True,
        "new_issues": [],
        "proposals": [],
        "positions": [],
    }


def round_bundle(previous: dict, source: bytes, attempt_id="attempt-1") -> dict:
    round_no = previous["round_no"] + 1
    input_hash = content_hash(previous)
    turns = (empty_turn("A", round_no, input_hash), empty_turn("B", round_no, input_hash))
    result = reduce_round(previous, turns)
    return {
        "round_no": round_no,
        "attempt_id": attempt_id,
        "source_hash": content_hash(source),
        "input_state_hash": input_hash,
        "turns": list(turns),
        "state_result": result,
    }


def created_store(tmp_path: Path, *, fault=None) -> tuple[SessionStore, bytes, dict]:
    source = b"tekst\n"
    store = SessionStore(tmp_path, fault=fault)
    store.create(source, {"max_rounds": 5, "session_id": "s-001"})
    return store, source, initial_state("s-001")


def test_source_is_verified_on_recovery(tmp_path):
    store = SessionStore(tmp_path)
    store.create(b"tekst\n", {"max_rounds": 5, "session_id": "s-001"})
    (tmp_path / "source.txt").write_bytes(b"inny tekst\n")

    with pytest.raises(SourceChanged):
        store.recover()


def test_committed_round_is_authoritative_over_stale_state_view(tmp_path):
    store, source, previous = created_store(tmp_path)
    bundle = round_bundle(previous, source)
    store.commit_round(bundle)
    store.write_status({"last_completed_round": 0, "stale": True})

    recovered = store.recover()

    assert recovered["last_completed_round"] == 1
    assert recovered["state"] == bundle["state_result"]
    assert (tmp_path / "rounds" / "0001" / "round.md").is_file()


@pytest.mark.parametrize(
    ("fault_stage", "visible_rounds"),
    [
        ("before_staging_fsync", 0),
        ("after_staging_fsync", 0),
        ("after_round_rename", 1),
        ("after_rounds_fsync", 1),
        ("before_state_write", 1),
        ("after_state_write", 1),
    ],
)
def test_fault_boundaries_expose_only_whole_old_or_new_round(tmp_path, fault_stage, visible_rounds):
    def fail(stage):
        if stage == fault_stage:
            raise RuntimeError(stage)

    store, source, previous = created_store(tmp_path, fault=fail)
    with pytest.raises(RuntimeError, match=fault_stage):
        store.commit_round(round_bundle(previous, source))

    recovered = SessionStore(tmp_path).recover()
    assert recovered["last_completed_round"] == visible_rounds
    assert not list((tmp_path / "rounds").glob("*.part"))


def test_incomplete_staging_directory_is_ignored(tmp_path):
    store, _, _ = created_store(tmp_path)
    staging = tmp_path / "rounds" / ".staging-attempt"
    staging.mkdir()
    (staging / "round.json").write_text("{}", encoding="utf-8")

    assert store.recover()["last_completed_round"] == 0


def test_corrupted_completed_round_stops_recovery(tmp_path):
    store, source, previous = created_store(tmp_path)
    store.commit_round(round_bundle(previous, source))
    path = tmp_path / "rounds" / "0001" / "round.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(IntegrityError, match="checksum"):
        store.recover()


def test_identical_recommit_is_noop_but_different_content_conflicts(tmp_path):
    store, source, previous = created_store(tmp_path)
    bundle = round_bundle(previous, source)
    store.commit_round(bundle)
    store.commit_round(copy.deepcopy(bundle))
    changed = copy.deepcopy(bundle)
    changed["attempt_id"] = "attempt-other"

    with pytest.raises(RoundConflict):
        store.commit_round(changed)


def test_session_lock_cannot_be_acquired_twice(tmp_path):
    store, _, _ = created_store(tmp_path)

    with store.lock():
        with pytest.raises(SessionLocked):
            with SessionStore(tmp_path).lock():
                pass


def test_events_are_append_only_and_status_is_replaceable(tmp_path):
    store, _, _ = created_store(tmp_path)
    event = {"event_id": "event-1", "kind": "checkpoint"}
    store.write_event(event)
    with pytest.raises(RoundConflict):
        store.write_event({**event, "kind": "changed"})

    store.write_status({"phase": "ONE"})
    store.write_status({"phase": "TWO"})
    assert json.loads((tmp_path / "runtime" / "state.json").read_text())["phase"] == "TWO"
