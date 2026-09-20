from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from peer_reviewer.mailbox import MailboxError, publish, receive


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: Any) -> datetime:
    if not isinstance(value, str):
        raise MailboxError("created_at must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise MailboxError("created_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MailboxError("created_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validate(command: dict[str, Any]) -> None:
    required = {"command_id", "session_id", "kind", "round_no", "created_at"}
    if not isinstance(command, dict) or not required <= set(command):
        raise MailboxError("command lacks required fields")
    if not isinstance(command["command_id"], str) or not command["command_id"]:
        raise MailboxError("command_id must be non-empty")
    if not isinstance(command["session_id"], str) or not command["session_id"]:
        raise MailboxError("session_id must be non-empty")
    if command["kind"] not in {"stop", "resume", "accept"}:
        raise MailboxError("unsupported command kind")
    if not isinstance(command["round_no"], int) or isinstance(command["round_no"], bool) or command["round_no"] < 0:
        raise MailboxError("round_no must be a non-negative integer")
    _parse(command["created_at"])


def _paths(session: Path) -> tuple[Path, Path, Path]:
    base = Path(session) / "runtime" / "commands"
    return base / "requests", base / "receipts", base / "acknowledgements"


def _find(directory: Path, command_id: str) -> dict[str, Any] | None:
    return next((item for item in receive(directory) if item.get("command_id") == command_id), None)


def _state(session: Path) -> dict[str, Any]:
    path = Path(session) / "runtime" / "state.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def submit_command(session: Path, command: dict[str, Any]) -> dict[str, Any]:
    _validate(command)
    requests, receipts, acknowledgements = _paths(session)
    if acknowledgement := _find(acknowledgements, command["command_id"]):
        return {"status": "applied", "session_stopped": acknowledgement["outcome"] == "STOPPED", "ack": acknowledgement}
    existing = _find(requests, command["command_id"])
    if existing is not None:
        stable = ("command_id", "session_id", "kind", "round_no")
        if any(existing.get(field) != command.get(field) for field in stable):
            raise MailboxError("command_id already belongs to a different command")
        command = existing
    else:
        publish(requests, command)
    receipt = _find(receipts, command["command_id"])
    state = _state(session)
    warning = None
    try:
        updated = _parse(state.get("updated_at"))
        if (_now() - updated).total_seconds() > 120:
            warning = "engine heartbeat is stale or unavailable"
    except MailboxError:
        warning = "engine heartbeat is stale or unavailable"
    return {
        "status": "received" if receipt else "submitted",
        "session_stopped": False,
        "request_path": str(next(path for path in requests.glob("*.json") if json.loads(path.read_text())["command_id"] == command["command_id"])),
        "warning": warning,
    }


class Control:
    def __init__(self, session: Path, clock: Any | None = None) -> None:
        self.session = Path(session)
        self.clock = clock

    def _now(self) -> datetime:
        return self.clock.now() if self.clock is not None else _now()

    def _reject(self, command: dict[str, Any], outcome: str) -> None:
        requests, receipts, acknowledgements = _paths(self.session)
        received_at = _iso(self._now())
        receipt = _find(receipts, command["command_id"])
        if receipt is None:
            receipt = {"command_id": command["command_id"], "received_at": received_at}
            publish(receipts, receipt)
        publish(
            acknowledgements,
            {
                "command_id": command["command_id"],
                "session_id": command["session_id"],
                "received_at": receipt["received_at"],
                "applied_at": _iso(self._now()),
                "outcome": outcome,
            },
        )

    def poll(self) -> list[dict[str, Any]]:
        requests, receipts, acknowledgements = _paths(self.session)
        try:
            metadata = json.loads((self.session / "session.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MailboxError("cannot read session identity") from exc
        current_round = int(_state(self.session).get("last_completed_round", 0))
        delivered: list[dict[str, Any]] = []
        for command in receive(requests):
            _validate(command)
            if _find(acknowledgements, command["command_id"]):
                continue
            if command["session_id"] != metadata["session_id"]:
                self._reject(command, "rejected_foreign_session")
                continue
            if command["round_no"] != current_round:
                self._reject(command, "rejected_stale_round")
                continue
            if _find(receipts, command["command_id"]) is None:
                publish(
                    receipts,
                    {"command_id": command["command_id"], "received_at": _iso(self._now())},
                )
            delivered.append(command)
        return delivered

    def acknowledge(self, command: dict[str, Any], outcome: str) -> dict[str, Any]:
        _, receipts, acknowledgements = _paths(self.session)
        receipt = _find(receipts, command["command_id"])
        if receipt is None:
            raise MailboxError("cannot acknowledge a command before receipt")
        ack = {
            "command_id": command["command_id"],
            "session_id": command["session_id"],
            "received_at": receipt["received_at"],
            "applied_at": _iso(self._now()),
            "outcome": outcome,
        }
        publish(acknowledgements, ack)
        return ack
