from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterator

from peer_reviewer.debate import initial_state, reduce_round
from peer_reviewer.report import render_event, render_round, render_status


class StoreError(RuntimeError):
    pass


class SourceChanged(StoreError):
    pass


class IntegrityError(StoreError):
    pass


class RoundConflict(StoreError):
    pass


class SessionLocked(StoreError):
    pass


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    raw = value if isinstance(value, bytes) else _canonical_json(value)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _file_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_fsynced(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


class SessionStore:
    def __init__(
        self,
        root: Path,
        *,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.root = Path(root)
        self._fault = fault or (lambda stage: None)

    def create(self, source: bytes, config: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        session_path = self.root / "session.json"
        if session_path.exists() or (self.root / "source.txt").exists():
            raise RoundConflict("Session already exists")
        for directory in (
            self.root / "rounds",
            self.root / "runtime" / "events",
            self.root / "runtime" / "commands",
            self.root / "attempts",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        session_id = config.get("session_id", self.root.name)
        metadata = {
            "schema_version": 1,
            "session_id": session_id,
            "source_hash": content_hash(source),
            "config": config,
        }
        _atomic_write(self.root / "source.txt", source)
        _atomic_write(session_path, _canonical_json(metadata) + b"\n")
        (self.root / "session.lock").touch(exist_ok=True)
        _fsync_directory(self.root)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        lock_path = self.root / "session.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise SessionLocked("Session is already locked") from exc
                raise
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _session_metadata(self) -> dict[str, Any]:
        try:
            metadata = json.loads((self.root / "session.json").read_text(encoding="utf-8"))
            source = (self.root / "source.txt").read_bytes()
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise IntegrityError(f"Cannot read session metadata: {exc}") from exc
        if content_hash(source) != metadata.get("source_hash"):
            raise SourceChanged("Immutable source hash no longer matches session.json")
        return metadata

    def commit_round(self, bundle: dict[str, Any]) -> None:
        metadata = self._session_metadata()
        round_no = bundle.get("round_no")
        if not isinstance(round_no, int) or round_no < 1:
            raise IntegrityError("round_no must be a positive integer")
        if bundle.get("source_hash") != metadata["source_hash"]:
            raise SourceChanged("Round source hash differs from immutable source")
        if bundle.get("input_state_hash") is None or bundle.get("state_result") is None:
            raise IntegrityError("Round bundle lacks state hashes/results")

        encoded_round = _canonical_json(bundle) + b"\n"
        round_markdown = render_round(bundle).encode("utf-8")
        target = self.root / "rounds" / f"{round_no:04d}"

        with self.lock():
            if target.exists():
                existing = (target / "round.json").read_bytes()
                if existing == encoded_round:
                    return
                raise RoundConflict(f"Round {round_no} already exists with different content")
            completed = sorted(
                int(path.name)
                for path in (self.root / "rounds").iterdir()
                if path.is_dir() and re.fullmatch(r"\d{4}", path.name)
            )
            expected_round = (completed[-1] + 1) if completed else 1
            if round_no != expected_round:
                raise RoundConflict(f"Expected round {expected_round}, got {round_no}")

            staging = self.root / "rounds" / f".staging-{bundle['attempt_id']}"
            try:
                staging.mkdir()
            except FileExistsError as exc:
                raise RoundConflict(f"Staging already exists for {bundle['attempt_id']}") from exc
            _write_fsynced(staging / "round.json", encoded_round)
            _write_fsynced(staging / "round.md", round_markdown)
            checksums = {
                "round.json": _file_digest(encoded_round),
                "round.md": _file_digest(round_markdown),
            }
            _write_fsynced(staging / "checksums.json", _canonical_json(checksums) + b"\n")
            self._fault("before_staging_fsync")
            _fsync_directory(staging)
            self._fault("after_staging_fsync")
            os.rename(staging, target)
            self._fault("after_round_rename")
            _fsync_directory(self.root / "rounds")
            self._fault("after_rounds_fsync")
            self._fault("before_state_write")
            self.write_status(
                {
                    "last_completed_round": round_no,
                    "debate_state": bundle["state_result"],
                }
            )
            self._fault("after_state_write")

    def _read_round(self, path: Path) -> dict[str, Any]:
        required = {"round.json", "round.md", "checksums.json"}
        if not required <= {item.name for item in path.iterdir() if item.is_file()}:
            raise IntegrityError(f"Incomplete committed round {path.name}")
        try:
            checksums = json.loads((path / "checksums.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise IntegrityError(f"Invalid checksums for round {path.name}") from exc
        for name in ("round.json", "round.md"):
            data = (path / name).read_bytes()
            if checksums.get(name) != _file_digest(data):
                raise IntegrityError(f"checksum mismatch in {path.name}/{name}")
        try:
            return json.loads((path / "round.json").read_text(encoding="utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise IntegrityError(f"Invalid round JSON in {path.name}") from exc

    def recover(self) -> dict[str, Any]:
        metadata = self._session_metadata()
        round_paths = sorted(
            (
                path
                for path in (self.root / "rounds").iterdir()
                if path.is_dir() and re.fullmatch(r"\d{4}", path.name)
            ),
            key=lambda path: path.name,
        )
        numbers = [int(path.name) for path in round_paths]
        if numbers != list(range(1, len(numbers) + 1)):
            raise IntegrityError("Committed round sequence contains a gap")

        state = initial_state(metadata["session_id"])
        bundles: list[dict[str, Any]] = []
        for expected_round, path in enumerate(round_paths, start=1):
            bundle = self._read_round(path)
            if bundle.get("round_no") != expected_round:
                raise IntegrityError("Round directory and payload number differ")
            if bundle.get("source_hash") != metadata["source_hash"]:
                raise IntegrityError("Round source hash differs from session")
            if bundle.get("input_state_hash") != content_hash(state):
                raise IntegrityError("Round input state hash breaks the chain")
            turns = bundle.get("turns")
            if not isinstance(turns, list) or len(turns) != 2:
                raise IntegrityError("Round does not contain two turns")
            if any(turn.get("input_state_hash") != bundle["input_state_hash"] for turn in turns):
                raise IntegrityError("Turn input state hash differs from bundle")
            try:
                replayed = reduce_round(state, (turns[0], turns[1]))
            except Exception as exc:
                raise IntegrityError(f"Round {expected_round} cannot be replayed: {exc}") from exc
            if replayed != bundle.get("state_result"):
                raise IntegrityError("Stored resulting state differs from deterministic replay")
            state = replayed
            bundles.append(bundle)

        return {
            "session": metadata,
            "state": state,
            "last_completed_round": len(bundles),
            "rounds": bundles,
        }

    def write_event(self, event: dict[str, Any]) -> None:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise IntegrityError("event_id is required")
        target = self.root / "runtime" / "events" / f"{event_id}.json"
        data = _canonical_json(event) + b"\n"
        if target.exists():
            if target.read_bytes() == data:
                _atomic_write(target.with_suffix(".md"), render_event(event).encode("utf-8"))
                return
            raise RoundConflict(f"Event {event_id} already exists with different content")
        _atomic_write(target, data)
        _atomic_write(target.with_suffix(".md"), render_event(event).encode("utf-8"))

    def write_status(self, status: dict[str, Any]) -> None:
        _atomic_write(self.root / "runtime" / "state.json", _canonical_json(status) + b"\n")
        _atomic_write(self.root / "runtime" / "status.md", render_status(status).encode("utf-8"))
