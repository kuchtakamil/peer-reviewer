"""Minimal JSON-RPC client for `codex app-server` metadata requests over stdio.

Only account and rate-limit reads are issued; no thread or turn is ever started,
so reading limits does not consume the subscription.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from peer_reviewer.adapters.shared import AdapterError

MAX_LINE_BYTES = 1024 * 1024
CLIENT_INFO = {"name": "peer-reviewer", "version": "0"}


class AppServerSession:
    def __init__(self, executable: Path, cwd: Path, env: dict[str, str], timeout_seconds: float) -> None:
        self.deadline = time.monotonic() + timeout_seconds
        self.process = subprocess.Popen(
            [str(executable), "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=cwd,
            env=env,
            shell=False,
            start_new_session=True,
        )
        self.lines: queue.Queue[bytes | None] = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        while line := self.process.stdout.readline(MAX_LINE_BYTES):
            self.lines.put(line)
        self.lines.put(None)

    def _send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        try:
            self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AdapterError("app_server", "Codex app-server closed its input") from exc

    def notify(self, method: str) -> None:
        self._send({"method": method})

    def request(self, method: str, params: dict[str, Any]) -> Any:
        self.next_id += 1
        request_id = self.next_id
        self._send({"id": request_id, "method": method, "params": params})
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise AdapterError("timeout", "Codex app-server did not answer before the deadline")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise AdapterError("app_server", "Codex app-server exited before answering")
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise AdapterError("app_server", "Codex app-server wrote invalid JSON") from exc
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue  # notifications and unrelated messages
            if "error" in message:
                raise AdapterError("app_server", f"Codex app-server rejected {method}")
            return message.get("result")

    def close(self) -> None:
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait()


def read_account_limits(
    executable: Path, cwd: Path, env: dict[str, str], timeout_seconds: float
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (`account/read` result, `account/rateLimits/read` result)."""
    session = AppServerSession(executable, cwd, env, timeout_seconds)
    try:
        session.request("initialize", {"clientInfo": CLIENT_INFO})
        session.notify("initialized")
        account = session.request("account/read", {})
        if not isinstance(account, dict) or not account.get("account"):
            raise AdapterError("authentication", "Codex is not logged in")
        limits = session.request("account/rateLimits/read", {})
    finally:
        session.close()
    if not isinstance(account, dict) or not isinstance(limits, dict):
        raise AdapterError("app_server", "Codex app-server returned malformed account data")
    return account, limits
