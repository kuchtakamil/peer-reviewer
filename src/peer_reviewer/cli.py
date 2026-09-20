from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import stat
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from peer_reviewer.control import submit_command
from peer_reviewer.config import ConfigError, load_config, validate_resume_config
from peer_reviewer.report import _atomic_text, rebuild_views, render_report, render_status
from peer_reviewer.store import RoundConflict, SessionStore, content_hash


def _emit(value: str) -> None:
    try:
        print(value)
    except BrokenPipeError:
        pass


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _active_session(sessions: Path, marker: Path) -> Path:
    value = Path(_read_json(marker)["session"])
    return value if value.is_absolute() else sessions / value


def _is_final_state(state: dict[str, Any]) -> bool:
    phase = state.get("phase")
    return phase in {"CONSENSUS", "NO_CONSENSUS", "STOPPED"} or (
        phase == "ERROR" and state.get("error_code") != "attempts_exhausted"
    )


def _status(session: Path, as_json: bool) -> None:
    rebuild_views(session)
    state = _read_json(session / "runtime" / "state.json")
    _emit(json.dumps(state, sort_keys=True, ensure_ascii=False) if as_json else render_status(state))


def _config_path(value: Path | None) -> Path:
    return value or Path(os.environ.get("PEER_REVIEWER_CONFIG", "config/reviewer.toml"))


def _write_report(session: Path) -> Path:
    store = SessionStore(session)
    recovered = store.recover()
    state_view = _read_json(session / "runtime" / "state.json") if (session / "runtime" / "state.json").exists() else {}
    outcome = state_view.get("outcome")
    if outcome not in {"CONSENSUS", "NO_CONSENSUS", "STOPPED", "ERROR"}:
        outcome = "ERROR"
    rendered = render_report(recovered["state"], recovered["rounds"], outcome)
    target = session / "report.md"
    _atomic_text(target, rendered)
    report_hash = content_hash(rendered.encode("utf-8"))
    event_id = f"report-{outcome.lower()}-{report_hash.removeprefix('sha256:')[:12]}"
    SessionStore(session).write_event(
        {
            "event_id": event_id,
            "session_id": recovered["session"]["session_id"],
            "kind": "report",
            "created_at": state_view.get("updated_at", "unknown"),
            "outcome": outcome,
            "report_hash": report_hash,
            "message": f"Report written with outcome {outcome}.",
        }
    )
    return target


@contextlib.contextmanager
def _deployment_guard(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / ".peer-reviewer.lock").open("a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        yield root / ".peer-reviewer-active.json"
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextlib.contextmanager
def _engine_guard(session: Path):
    handle = (session / "runtime" / "engine.lock").open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise ValueError(f"engine already serves session: {session}") from exc
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _start(source: Path, session: Path, config_path: Path) -> None:
    if source.is_symlink() or not source.exists() or not stat.S_ISREG(source.stat().st_mode):
        raise ValueError("source must be a regular file")
    config = load_config(config_path)
    data = source.read_bytes()
    if len(data) > config["process"]["source_max_bytes"]:
        raise ValueError("source exceeds configured byte limit")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("source must be valid UTF-8") from exc
    if session.exists():
        raise ValueError("session already exists")
    with _deployment_guard(session.parent) as active_path:
        if active_path.exists():
            active_session = _active_session(session.parent, active_path)
            active_state_path = active_session / "runtime" / "state.json"
            active_state = _read_json(active_state_path) if active_state_path.exists() else {}
            if not _is_final_state(active_state):
                raise ValueError(f"active session already exists: {active_session}")
        SessionStore(session).create(data, {**config, "session_id": session.name})
        _atomic_text(active_path, json.dumps({"session": session.name}, sort_keys=True) + "\n")


def _doctor(config_path: Path, sessions: Path) -> tuple[int, dict[str, Any]]:
    issues: list[str] = []
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        return 1, {"ok": False, "issues": [str(exc)], "actions": []}
    for provider in ("claude", "codex"):
        reviewer = config["reviewers"][provider]
        actual_version = os.environ.get(f"{provider.upper()}_CLI_VERSION")
        if actual_version != reviewer["cli_version"]:
            issues.append(
                f"{provider} CLI version mismatch or unavailable: expected {reviewer['cli_version']}, got {actual_version or 'unknown'}"
            )
        if reviewer["account_fingerprint"].lower() in {"unconfigured", "unknown", "none"}:
            issues.append(f"{provider} subscription account is not configured")
        if os.environ.get(f"{provider.upper()}_LIMIT_CONFIDENCE") != "verified":
            issues.append(f"{provider} limit read unavailable or unverified")
    try:
        sessions.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".doctor-", dir=sessions)
        os.close(descriptor)
        Path(name).read_bytes()
        Path(name).unlink()
    except OSError as exc:
        issues.append(f"sessions directory is not readable/writable: {exc}")
    return (1 if issues else 0), {
        "ok": not issues,
        "issues": issues,
        "actions": ["provision and verify subscription OAuth", "verify provider limit telemetry"],
    }


def _serve(config_path: Path, sessions: Path, once: bool) -> int:
    from peer_reviewer.control import Control
    from peer_reviewer.engine import Engine
    from peer_reviewer.worker import WorkerClient, WorkerPool

    current = load_config(config_path)
    active_path = sessions / ".peer-reviewer-active.json"
    if not active_path.exists():
        return 0
    session = _active_session(sessions, active_path)
    with _engine_guard(session):
        saved = SessionStore(session).recover()["session"]["config"]
        validate_resume_config(saved, current)
        mailboxes = Path(os.environ.get("PEER_REVIEWER_MAILBOXES", "/mailboxes"))
        pool = WorkerPool(
            {
                "A": WorkerClient("A", mailboxes / "claude" / "inbox", mailboxes / "claude" / "outbox"),
                "B": WorkerClient("B", mailboxes / "codex" / "inbox", mailboxes / "codex" / "outbox"),
            }
        )

        class SystemClock:
            @staticmethod
            def now():
                return datetime.now(timezone.utc)

            @staticmethod
            def monotonic():
                return time.monotonic()

        engine = Engine(SessionStore(session), pool, Control(session, SystemClock()), SystemClock())
        engine.resume()
        while True:
            state = engine.tick()
            if state["phase"] in {"CONSENSUS", "NO_CONSENSUS", "STOPPED", "ERROR"}:
                _write_report(session)
                if _is_final_state(state):
                    with _deployment_guard(sessions) as marker:
                        if marker.exists() and _active_session(sessions, marker) == session:
                            marker.unlink()
                return 0 if state["phase"] != "ERROR" else 1
            if once:
                return 0
            time.sleep(0.1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="peer-reviewer")
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status")
    status.add_argument("--session", type=Path, required=True)
    status.add_argument("--json", action="store_true")
    status.add_argument("--watch", action="store_true")
    start = subparsers.add_parser("start")
    start.add_argument("source", type=Path)
    start.add_argument("--session", type=Path, required=True)
    start.add_argument("--config", type=Path)
    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--config", type=Path, required=True)
    doctor.add_argument("--sessions", type=Path, default=Path(os.environ.get("PEER_REVIEWER_SESSIONS", "/sessions")))
    serve = subparsers.add_parser("serve")
    serve.add_argument("--config", type=Path, required=True)
    serve.add_argument("--sessions", type=Path, default=Path(os.environ.get("PEER_REVIEWER_SESSIONS", "/sessions")))
    serve.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    report = subparsers.add_parser("report")
    report.add_argument("--session", type=Path, required=True)
    accept = subparsers.add_parser("accept")
    accept.add_argument("--session", type=Path, required=True)
    for name in ("stop", "resume"):
        command = subparsers.add_parser(name)
        command.add_argument("--session", type=Path, required=True)
        command.add_argument("--command-id")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            while True:
                _status(args.session, args.json)
                if not args.watch:
                    break
                time.sleep(1)
            return 0
        if args.command == "start":
            _start(args.source, args.session, _config_path(args.config))
            _emit(json.dumps({"status": "started", "session": str(args.session)}, sort_keys=True))
            return 0
        if args.command == "doctor":
            code, result = _doctor(args.config, args.sessions)
            _emit(json.dumps(result, sort_keys=True, ensure_ascii=False))
            return code
        if args.command == "serve":
            return _serve(args.config, args.sessions, args.once)
        if args.command == "report":
            target = _write_report(args.session)
            _emit(str(target))
            return 0
        if args.command == "accept":
            report_path = args.session / "report.md"
            if not report_path.exists():
                report_path = _write_report(args.session)
            acceptance = {
                "report_hash": content_hash(report_path.read_bytes()),
                "accepted_at": datetime.now(timezone.utc).isoformat(),
            }
            _atomic_text(
                args.session / "runtime" / "acceptance.json",
                json.dumps(acceptance, sort_keys=True, separators=(",", ":")) + "\n",
            )
            _emit(json.dumps(acceptance, sort_keys=True))
            return 0
        metadata = _read_json(args.session / "session.json")
        state = _read_json(args.session / "runtime" / "state.json")
        if args.command == "resume" and _is_final_state(state):
            rebuild_views(args.session)
            _write_report(args.session)
            _emit(json.dumps({"status": "terminal", "phase": state["phase"]}, sort_keys=True))
            return 0
        command = {
            "command_id": args.command_id or uuid.uuid4().hex,
            "session_id": metadata["session_id"],
            "kind": args.command,
            "round_no": int(state.get("last_completed_round", 0)),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _emit(json.dumps(submit_command(args.session, command), sort_keys=True, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, UnicodeError, ValueError, ConfigError, RoundConflict) as exc:
        try:
            print(str(exc), file=sys.stderr)
        except BrokenPipeError:
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
