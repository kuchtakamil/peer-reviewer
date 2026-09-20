from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any


class MailboxError(ValueError):
    pass


class MailboxConflict(MailboxError):
    pass


def _canonical(message: dict[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                message,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MailboxError("message must be a finite JSON object") from exc


def _message_id(message: dict[str, Any]) -> str:
    for key in ("job_id", "command_id", "event_id"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    raise MailboxError("message requires a non-empty job_id, command_id, or event_id")


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish(directory: Path, message: dict[str, Any]) -> Path:
    if not isinstance(message, dict):
        raise MailboxError("message must be an object")
    identifier = _message_id(message)
    data = _canonical(message)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
    target = directory / f"{digest}.json"
    lock_path = directory / ".mailbox.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if target.exists():
                if target.read_bytes() == data:
                    return target
                raise MailboxConflict(f"message id {identifier!r} already has different content")
            temporary = directory / f".{digest}.{uuid.uuid4().hex}.part"
            try:
                with temporary.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.rename(temporary, target)
                _fsync_directory(directory)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    temporary.unlink()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return target


def receive(directory: Path) -> list[dict[str, Any]]:
    directory = Path(directory)
    if not directory.exists():
        return []
    messages: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MailboxError(f"invalid committed mailbox message: {path.name}") from exc
        if not isinstance(value, dict):
            raise MailboxError(f"committed mailbox message is not an object: {path.name}")
        _message_id(value)
        messages.append(value)
    return messages

