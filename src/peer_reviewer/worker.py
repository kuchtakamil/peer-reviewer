from __future__ import annotations

import hashlib
import argparse
import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from peer_reviewer.mailbox import MailboxError, publish, receive


class WorkerError(RuntimeError):
    pass


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise MailboxError("deadline must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise MailboxError("deadline must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MailboxError("deadline must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_job(job: dict[str, Any]) -> None:
    required = {
        "job_id",
        "session_id",
        "round_no",
        "attempt_id",
        "reviewer",
        "kind",
        "input_state_hash",
        "payload",
        "deadline",
    }
    if not isinstance(job, dict) or not required <= set(job):
        raise MailboxError("job lacks required fields")
    if not isinstance(job["job_id"], str) or not job["job_id"]:
        raise MailboxError("job_id must be non-empty")
    if not isinstance(job["session_id"], str) or not job["session_id"]:
        raise MailboxError("session_id must be non-empty")
    if job["reviewer"] not in {"A", "B"}:
        raise MailboxError("reviewer must be A or B")
    if job["kind"] not in {"review", "limits", "cancel"}:
        raise MailboxError("unsupported job kind")
    if not isinstance(job["payload"], dict):
        raise MailboxError("payload must be an object")
    _parse_time(job["deadline"])
    if job["kind"] == "review":
        if not isinstance(job["round_no"], int) or isinstance(job["round_no"], bool) or job["round_no"] < 1:
            raise MailboxError("review round_no must be positive")
        if not isinstance(job["attempt_id"], str) or not job["attempt_id"]:
            raise MailboxError("review attempt_id must be non-empty")
        if not isinstance(job["input_state_hash"], str) or not job["input_state_hash"]:
            raise MailboxError("review input_state_hash must be non-empty")
    elif any(job[field] is not None for field in ("round_no", "attempt_id", "input_state_hash")):
        raise MailboxError("metadata and cancel jobs require null round, attempt, and state fields")
    if job["kind"] == "cancel" and (
        not isinstance(job["payload"].get("target_job_id"), str)
        or not job["payload"]["target_job_id"]
    ):
        raise MailboxError("cancel job requires payload.target_job_id")


def _key(identifier: str) -> str:
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    with temporary.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _response(job: dict[str, Any], *, ok: bool, result: Any, error_code: str | None) -> dict[str, Any]:
    return {
        field: job[field]
        for field in (
            "job_id",
            "session_id",
            "round_no",
            "attempt_id",
            "reviewer",
            "kind",
            "input_state_hash",
        )
    } | {"ok": ok, "result": result, "error_code": error_code}


class Worker:
    def __init__(
        self,
        reviewer: str,
        adapter: Any,
        inbox: Path,
        outbox: Path,
        state_directory: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        poll_seconds: float = 0.05,
        cli_version: str | None = None,
    ) -> None:
        if reviewer not in {"A", "B"}:
            raise ValueError("reviewer must be A or B")
        self.reviewer = reviewer
        self.adapter = adapter
        self.inbox = Path(inbox)
        self.outbox = Path(outbox)
        self.state_directory = Path(state_directory)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.poll_seconds = poll_seconds
        self.cli_version = cli_version
        (self.state_directory / "running").mkdir(parents=True, exist_ok=True)
        (self.state_directory / "completed").mkdir(parents=True, exist_ok=True)

    def _running_path(self, job_id: str) -> Path:
        return self.state_directory / "running" / f"{_key(job_id)}.json"

    def _completed_path(self, job_id: str) -> Path:
        return self.state_directory / "completed" / f"{_key(job_id)}.json"

    def _finish(self, job: dict[str, Any], response: dict[str, Any]) -> None:
        publish(self.outbox, response)
        _write_json(self._completed_path(job["job_id"]), response)
        self._running_path(job["job_id"]).unlink(missing_ok=True)

    def _recover_interrupted(self) -> bool:
        running = sorted((self.state_directory / "running").glob("*.json"))
        if not running:
            return False
        path = running[0]
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            validate_job(job)
        except (OSError, UnicodeError, json.JSONDecodeError, MailboxError) as exc:
            raise WorkerError("invalid durable running-job record") from exc
        if self._completed_path(job["job_id"]).exists():
            path.unlink(missing_ok=True)
            return True
        existing = next(
            (item for item in receive(self.outbox) if item.get("job_id") == job["job_id"]),
            None,
        )
        if existing is not None:
            _write_json(self._completed_path(job["job_id"]), existing)
            path.unlink(missing_ok=True)
            return True
        self._finish(job, _response(job, ok=False, result=None, error_code="interrupted"))
        return True

    def _run_review(self, job: dict[str, Any]) -> None:
        cancel_event = threading.Event()
        deadline_expired = False
        outcome: dict[str, Any] = {}

        def invoke() -> None:
            try:
                payload = dict(job["payload"])
                payload["_cancel_event"] = cancel_event
                outcome["result"] = self.adapter.review(payload)
            except BaseException as exc:  # retained so simulated/real worker loss is not hidden
                outcome["exception"] = exc

        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        cancels: list[dict[str, Any]] = []
        while thread.is_alive():
            if _parse_time(job["deadline"]) <= self.clock().astimezone(timezone.utc):
                deadline_expired = True
                cancel_event.set()
            for candidate in receive(self.inbox):
                if candidate.get("kind") != "cancel":
                    continue
                try:
                    validate_job(candidate)
                except MailboxError:
                    continue
                if candidate["payload"]["target_job_id"] == job["job_id"]:
                    if not self._completed_path(candidate["job_id"]).exists():
                        cancels.append(candidate)
                    cancel_event.set()
            thread.join(self.poll_seconds)

        exception = outcome.get("exception")
        if isinstance(exception, BaseException) and not isinstance(exception, Exception):
            raise exception
        if deadline_expired:
            response = _response(job, ok=False, result=None, error_code="deadline")
        elif cancels:
            response = _response(job, ok=False, result=None, error_code="cancelled")
        elif isinstance(exception, Exception):
            response = _response(
                job,
                ok=False,
                result=None,
                error_code=getattr(exception, "code", "worker_error"),
            )
        else:
            response = _response(job, ok=True, result=outcome.get("result"), error_code=None)
        self._finish(job, response)
        for cancel in {item["job_id"]: item for item in cancels}.values():
            self._finish(cancel, _response(cancel, ok=True, result={"cancelled": job["job_id"]}, error_code=None))

    def run_once(self) -> bool:
        if self._recover_interrupted():
            return True
        for job in receive(self.inbox):
            try:
                validate_job(job)
            except MailboxError:
                continue
            if job["reviewer"] != self.reviewer or self._completed_path(job["job_id"]).exists():
                continue
            if job["kind"] == "cancel":
                target_id = job["payload"]["target_job_id"]
                completed = self._completed_path(target_id)
                if completed.exists():
                    response = _response(
                        job,
                        ok=True,
                        result={"cancelled": target_id, "already_stopped": True},
                        error_code=None,
                    )
                else:
                    # Jobs run one at a time, so an unknown target has not started here.
                    # The tombstone keeps a late-published target from ever starting.
                    _write_json(
                        completed,
                        {"job_id": target_id, "ok": False, "result": None, "error_code": "cancelled"},
                    )
                    response = _response(
                        job,
                        ok=True,
                        result={"cancelled": target_id, "never_started": True},
                        error_code=None,
                    )
                self._finish(job, response)
                return True
            _write_json(self._running_path(job["job_id"]), job)
            if _parse_time(job["deadline"]) <= self.clock().astimezone(timezone.utc):
                self._finish(job, _response(job, ok=False, result=None, error_code="deadline"))
            elif job["kind"] == "limits":
                try:
                    result = self.adapter.read_limits()
                    if self.cli_version is not None and isinstance(result, dict):
                        result = {**result, "cli_version": self.cli_version}
                    response = _response(job, ok=True, result=result, error_code=None)
                except Exception as exc:
                    response = _response(job, ok=False, result=None, error_code=getattr(exc, "code", "worker_error"))
                self._finish(job, response)
            else:
                self._run_review(job)
            return True
        return False


class WorkerClient:
    def __init__(
        self,
        reviewer: str,
        inbox: Path,
        outbox: Path,
        *,
        timeout_seconds: float = 10.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.reviewer = reviewer
        self.inbox = Path(inbox)
        self.outbox = Path(outbox)
        self.timeout_seconds = timeout_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._submitted: dict[str, dict[str, Any]] = {}

    def submit(self, job: dict[str, Any]) -> str:
        validate_job(job)
        if job["reviewer"] != self.reviewer:
            raise MailboxError("job reviewer does not match client")
        publish(self.inbox, job)
        self._submitted[job["job_id"]] = job
        return job["job_id"]

    def poll(self, job_id: str) -> dict[str, Any] | None:
        expected = self._submitted.get(job_id)
        for response in receive(self.outbox):
            if response.get("job_id") != job_id:
                continue
            if expected is None:
                return None
            fields = ("session_id", "round_no", "attempt_id", "reviewer", "kind", "input_state_hash")
            if any(response.get(field) != expected.get(field) for field in fields):
                return None
            if not isinstance(response.get("ok"), bool) or "error_code" not in response:
                raise MailboxError("malformed worker response")
            return response
        return None

    def _wait(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if response := self.poll(job_id):
                return response
            time.sleep(0.01)
        raise WorkerError(f"worker did not acknowledge {job_id!r} before timeout")

    def limits(self) -> dict[str, Any]:
        job_id = f"limits-{self.reviewer}-{uuid.uuid4().hex}"
        job = {
            "job_id": job_id,
            "session_id": "metadata",
            "round_no": None,
            "attempt_id": None,
            "reviewer": self.reviewer,
            "kind": "limits",
            "input_state_hash": None,
            "payload": {},
            "deadline": (self.clock() + timedelta(seconds=self.timeout_seconds)).isoformat(),
        }
        self.submit(job)
        response = self._wait(job_id)
        if not response["ok"]:
            raise WorkerError(f"limit read failed: {response['error_code']}")
        return response["result"]

    def cancel(self, job_id: str) -> None:
        target = self._submitted.get(job_id)
        session_id = target["session_id"] if target is not None else job_id.rsplit("-r", 1)[0]
        if not session_id or session_id == job_id:
            raise WorkerError(f"cannot derive session for job {job_id!r}")
        cancel_id = f"cancel-{job_id}-{uuid.uuid4().hex}"
        job = {
            "job_id": cancel_id,
            "session_id": session_id,
            "round_no": None,
            "attempt_id": None,
            "reviewer": self.reviewer,
            "kind": "cancel",
            "input_state_hash": None,
            "payload": {"target_job_id": job_id},
            "deadline": (self.clock() + timedelta(seconds=self.timeout_seconds)).isoformat(),
        }
        self.submit(job)
        response = self._wait(cancel_id)
        if not response["ok"]:
            raise WorkerError(f"cancel failed: {response['error_code']}")


class WorkerPool:
    def __init__(self, clients: dict[str, WorkerClient]) -> None:
        if set(clients) != {"A", "B"}:
            raise ValueError("worker pool requires A and B clients")
        self.clients = dict(clients)
        self.jobs: dict[str, str] = {}

    def limits(self) -> dict[str, Any]:
        return {reviewer: self.clients[reviewer].limits() for reviewer in ("A", "B")}

    def submit(self, job: dict[str, Any]) -> str:
        reviewer = job.get("reviewer")
        if reviewer not in self.clients:
            raise WorkerError("job has unknown reviewer")
        job_id = self.clients[reviewer].submit(job)
        self.jobs[job_id] = reviewer
        return job_id

    def poll(self, job_id: str) -> dict[str, Any] | None:
        reviewer = self.jobs.get(job_id) or (job_id[-1] if job_id.endswith(("-A", "-B")) else None)
        if reviewer is None:
            return None
        return self.clients[reviewer].poll(job_id)

    def cancel(self, job_id: str) -> None:
        reviewer = self.jobs.get(job_id) or (job_id[-1] if job_id.endswith(("-A", "-B")) else None)
        if reviewer is None:
            raise WorkerError(f"unknown job {job_id!r}")
        self.clients[reviewer].cancel(job_id)


PASSTHROUGH_ENV = ("PATH", "TZ", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "DISABLE_AUTOUPDATER")
DEFAULT_MODEL_BUCKETS = {"claude": "subscription", "codex": "codex"}


def provider_env(environ: dict[str, str]) -> dict[str, str]:
    """Minimal environment for reviewer CLIs: no API keys, only auth/config locations."""
    env = {"HOME": "/work/home"}
    env.update({name: environ[name] for name in PASSTHROUGH_ENV if environ.get(name)})
    return env


def cli_version(executable: Path, env: dict[str, str]) -> str | None:
    """Version reported by the reviewer CLI, e.g. "2.1.280" from "2.1.280 (Claude Code)"."""
    try:
        completed = subprocess.run(
            [str(executable), "--version"], capture_output=True, text=True, env=env, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return next((token for token in completed.stdout.split() if token[:1].isdigit()), None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("claude", "codex"), required=True)
    parser.add_argument(
        "--probe",
        action="store_true",
        help="print one limit reading (with the observed account fingerprint) and exit",
    )
    args = parser.parse_args(argv)
    from peer_reviewer.adapters.claude import ClaudeAdapter
    from peer_reviewer.adapters.codex import CodexAdapter

    reviewer = os.environ.get("PEER_REVIEWER_REVIEWER", "A" if args.provider == "claude" else "B")
    options = {
        "cwd": Path("/work"),
        "env": provider_env(dict(os.environ)),
        "timeout_seconds": 900,
        "max_output_bytes": 1024 * 1024,
        "prompt_max_bytes": 512 * 1024,
        "source_max_bytes": 64 * 1024,
        "account_fingerprint": os.environ.get("PEER_REVIEWER_ACCOUNT_FINGERPRINT", "unconfigured"),
        "model_bucket": os.environ.get(
            "PEER_REVIEWER_MODEL_BUCKET", DEFAULT_MODEL_BUCKETS[args.provider]
        ),
    }
    adapter_type = ClaudeAdapter if args.provider == "claude" else CodexAdapter
    executable = Path(os.environ["PEER_REVIEWER_EXECUTABLE"])
    adapter = adapter_type(executable, **options)
    version = cli_version(executable, options["env"])
    if args.probe:
        print(json.dumps(adapter.read_limits() | {"cli_version": version}, indent=2, sort_keys=True))
        return 0
    worker = Worker(
        reviewer,
        adapter,
        Path("/mailbox/inbox"),
        Path("/mailbox/outbox"),
        Path("/work/state"),
        cli_version=version,
    )
    while True:
        if not worker.run_once():
            time.sleep(0.1)


if __name__ == "__main__":
    raise SystemExit(main())
