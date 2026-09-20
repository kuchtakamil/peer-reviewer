from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


def run_cli(
    argv: list[str],
    stdin: bytes,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float,
    max_output_bytes: int,
    *,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    if not argv or any(not isinstance(item, str) for item in argv):
        raise ValueError("argv must be a non-empty list of strings")
    if max_output_bytes < 1:
        raise ValueError("max_output_bytes must be positive")
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        shell=False,
        start_new_session=True,
    )
    output = {"stdout": bytearray(), "stderr": bytearray()}
    output_lock = threading.Lock()
    output_too_large = threading.Event()

    def read_stream(name: str, stream: Any) -> None:
        while chunk := stream.read(64 * 1024):
            with output_lock:
                captured = len(output["stdout"]) + len(output["stderr"])
                remaining = max(0, max_output_bytes - captured)
                output[name].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    output_too_large.set()

    def write_stdin() -> None:
        try:
            assert process.stdin is not None
            process.stdin.write(stdin)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            if process.stdin is not None:
                process.stdin.close()

    assert process.stdout is not None and process.stderr is not None
    threads = [
        threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
        threading.Thread(target=write_stdin, daemon=True),
    ]
    for thread in threads:
        thread.start()

    def kill_group() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    cancelled = False
    while process.poll() is None:
        if output_too_large.is_set():
            kill_group()
            break
        if time.monotonic() >= deadline:
            timed_out = True
            kill_group()
            break
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            kill_group()
            break
        time.sleep(0.01)
    process.wait()
    for thread in threads:
        thread.join(timeout=1)
    return {
        "returncode": process.returncode,
        "stdout": bytes(output["stdout"]),
        "stderr": bytes(output["stderr"]),
        "timed_out": timed_out,
        "output_too_large": output_too_large.is_set(),
        "cancelled": cancelled,
    }
