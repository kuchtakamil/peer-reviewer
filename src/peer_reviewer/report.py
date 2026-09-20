from __future__ import annotations

import json
import os
import contextlib
from pathlib import Path
from typing import Any


def render_round(bundle: dict[str, Any]) -> str:
    lines = [
        f"# Round {bundle['round_no']}",
        "",
        f"Attempt: `{bundle['attempt_id']}`",
        f"Input state: `{bundle['input_state_hash']}`",
        "",
    ]
    for turn in sorted(bundle["turns"], key=lambda value: value["reviewer"]):
        lines.extend(
            [
                f"## Reviewer {turn['reviewer']}",
                "",
                "```json",
                json.dumps(turn, ensure_ascii=False, indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "## Resulting debate state",
            "",
            "```json",
            json.dumps(bundle["state_result"], ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def render_status(state: dict[str, Any]) -> str:
    lines = [
        "# Peer reviewer status",
        "",
        f"Phase: **{state.get('phase', 'UNKNOWN')}**",
        f"Last completed round: {state.get('last_completed_round', 0)}",
        f"Updated at: {state.get('updated_at', 'unknown')}",
    ]
    checkpoint = state.get("checkpoint")
    if isinstance(checkpoint, dict):
        lines.extend(
            [
                f"Event ID: `{checkpoint.get('event_id', 'unknown')}`",
                f"Veto deadline: {checkpoint.get('deadline', 'unknown')}",
            ]
        )
    pause = state.get("pause")
    if isinstance(pause, dict):
        lines.extend(
            [
                f"Pause reason: {pause.get('reason', 'unknown')}",
                f"Reset: {pause.get('wake_at') or 'reset time unknown'}",
                f"Next check: {pause.get('next_check_at', 'unknown')}",
            ]
        )
    if state.get("error_code"):
        lines.append(f"Error: `{state['error_code']}`")
    return "\n".join(lines) + "\n"


def render_event(event: dict[str, Any]) -> str:
    lines = [
        f"# Event {event['event_id']}",
        "",
        f"Session: `{event.get('session_id', 'unknown')}`",
        f"Kind: **{event.get('kind', 'unknown')}**",
        f"Updated at: {event.get('created_at', 'unknown')}",
    ]
    if "round_no" in event:
        lines.append(f"Round: {event['round_no']}")
    if event.get("kind") == "checkpoint":
        counts = event.get("counts", {})
        lines.extend(
            [
                f"Deadline: {event.get('deadline', 'unknown')}",
                f"agreed: {counts.get('agreed', 0)}",
                f"rejected: {counts.get('rejected', 0)}",
                f"disputed: {counts.get('disputed', 0)}",
                f"Veto command: `{event.get('veto_command', 'unknown')}`",
            ]
        )
    elif event.get("kind") == "pause":
        lines.extend(
            [
                f"Reason: {event.get('reason', 'unknown')}",
                f"Reset: {event.get('wake_at') or 'reset time unknown'}",
                f"Next check: {event.get('next_check_at', 'unknown')}",
                "Details: " + json.dumps(event.get("details", {}), sort_keys=True, ensure_ascii=False),
            ]
        )
    elif event.get("kind") == "error" or event.get("error_code"):
        lines.append(f"Error: `{event.get('error_code', 'unknown')}`")
    if event.get("message"):
        lines.extend(["", str(event["message"])])
    return "\n".join(lines) + "\n"


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def rebuild_views(session: Path) -> None:
    session = Path(session)
    state_path = session / "runtime" / "state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        _atomic_text(session / "runtime" / "status.md", render_status(state))
    events = session / "runtime" / "events"
    if events.exists():
        for path in sorted(events.glob("*.json")):
            event = json.loads(path.read_text(encoding="utf-8"))
            _atomic_text(path.with_suffix(".md"), render_event(event))


def _round_link(round_no: Any) -> str:
    try:
        value = int(round_no)
    except (TypeError, ValueError):
        value = 0
    return f"[round {value}](rounds/{value:04d}/round.md)" if value > 0 else "round unavailable"


def render_report(state: dict[str, Any], history: list[dict[str, Any]], outcome: str) -> str:
    if outcome not in {"CONSENSUS", "NO_CONSENSUS", "STOPPED", "ERROR"}:
        raise ValueError("unsupported report outcome")
    partial = outcome in {"STOPPED", "ERROR"}
    lines = [
        "# Peer review report",
        "",
        f"Session: `{state.get('session_id', 'unknown')}`",
        f"Outcome: **{outcome}**" + (" — **PARTIAL**" if partial else ""),
        f"Completed rounds: {state.get('round_no', len(history))}",
        "",
        "## Round history",
        "",
    ]
    if history:
        lines.extend(f"- {_round_link(item.get('round_no'))}" for item in history)
    else:
        lines.append("No committed rounds.")

    issues = state.get("issues", {})
    versions = state.get("versions", {})
    resolved = state.get("resolved", {})
    categories: dict[str, list[tuple[str, dict[str, Any], dict[str, Any]]]] = {
        "accepted": [],
        "rejected": [],
        "duplicate": [],
    }
    disputed: list[tuple[str, dict[str, Any]]] = []
    for issue_id, issue in sorted(issues.items()):
        reference = resolved.get(issue_id)
        version = versions.get(reference) if reference else None
        resolution = version.get("payload", {}).get("resolution") if isinstance(version, dict) else None
        if resolution in categories:
            categories[resolution].append((issue_id, issue, version))
        else:
            disputed.append((issue_id, issue))

    lines.extend(["", "## Agreed issues", ""])
    if not categories["accepted"]:
        lines.append("No agreed issues.")
    for issue_id, issue, version in categories["accepted"]:
        payload = version["payload"]
        lines.extend(
            [
                f"### {issue_id}: {payload.get('problem') or issue.get('problem', '')}",
                "",
                f"Fix: {payload.get('fix') or 'No textual fix recorded.'}",
                f"Reason: {payload.get('rationale', '')}",
                f"Source: {_round_link(version.get('round_no'))}",
                "",
            ]
        )

    lines.extend(["## Jointly rejected issues", ""])
    if not categories["rejected"]:
        lines.append("No jointly rejected issues.")
    for issue_id, issue, version in categories["rejected"]:
        lines.extend(
            [
                f"### {issue_id}: {version['payload'].get('problem') or issue.get('problem', '')}",
                "",
                f"Withdrawal/rejection reason: {version['payload'].get('rationale', '')}",
                f"Source: {_round_link(version.get('round_no'))}",
                "",
            ]
        )

    lines.extend(["## Disputed issues", ""])
    if not disputed:
        lines.append("No disputed issues.")
    positions = state.get("positions", {})
    arguments = state.get("arguments", {})
    for issue_id, issue in disputed:
        lines.extend([f"### {issue_id}: {issue.get('problem', '')}", ""])
        for reviewer in ("A", "B"):
            position = positions.get(reviewer, {}).get(issue_id)
            if not isinstance(position, dict):
                lines.append(f"Reviewer {reviewer}: no final position recorded.")
                continue
            argument = arguments.get(position.get("argument_id"), {})
            reason = position.get("reason") or argument.get("reason", "")
            lines.append(
                f"Reviewer {reviewer} ({position.get('action', 'unknown')}, "
                f"`{position.get('argument_id', 'no-argument')}`): {reason} — "
                f"{_round_link(argument.get('round_no'))}"
            )
        lines.append("")

    lines.extend(["## Duplicate issues", ""])
    if not categories["duplicate"]:
        lines.append("No duplicate issues.")
    for issue_id, issue, version in categories["duplicate"]:
        payload = version["payload"]
        lines.extend(
            [
                f"### {issue_id}: {payload.get('problem') or issue.get('problem', '')}",
                "",
                f"Duplicate link: {issue_id} -> {payload.get('duplicate_of', 'unknown')}",
                f"Reason: {payload.get('rationale', '')}",
                f"Source: {_round_link(version.get('round_no'))}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
