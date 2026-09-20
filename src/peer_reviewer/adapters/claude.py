from __future__ import annotations

import json
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

from peer_reviewer.adapters.process import run_cli
from peer_reviewer.adapters.shared import AdapterError, check_process_result, unknown_limit_sample
from peer_reviewer.protocol import ProtocolError, parse_turn, reject_duplicate_keys
from peer_reviewer.prompts import build_context, build_prompt


def build_argv(executable: Path, schema: str, model: str | None = None) -> list[str]:
    argv = [
        str(executable),
        "-p",
    ]
    if model:
        argv.extend(["--model", model])
    argv.extend([
        "--output-format",
        "json",
        "--json-schema",
        schema,
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
    ])
    return argv


class ClaudeAdapter:
    def __init__(
        self,
        executable: Path,
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
        max_output_bytes: int,
        prompt_max_bytes: int,
        source_max_bytes: int,
        account_fingerprint: str,
        model_bucket: str,
        limit_reader: Callable[[], dict[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.executable = Path(executable)
        self.cwd = Path(cwd)
        self.env = dict(env)
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.prompt_max_bytes = prompt_max_bytes
        self.source_max_bytes = source_max_bytes
        self.account_fingerprint = account_fingerprint
        self.model_bucket = model_bucket
        self.limit_reader = limit_reader
        self.clock = clock

    def read_limits(self) -> dict[str, Any]:
        if self.limit_reader is not None:
            return self.limit_reader()
        return unknown_limit_sample(
            "claude", self.account_fingerprint, self.model_bucket, self.clock
        )

    def review(self, job: dict[str, Any]) -> dict[str, Any]:
        source = job["source"]
        process = job.get("process", {})
        source_max_bytes = int(process.get("source_max_bytes", self.source_max_bytes))
        prompt_max_bytes = int(process.get("prompt_max_bytes", self.prompt_max_bytes))
        output_max_bytes = int(process.get("output_max_bytes", self.max_output_bytes))
        timeout_seconds = float(process.get("timeout_seconds", self.timeout_seconds))
        if len(source.encode("utf-8")) > source_max_bytes:
            raise AdapterError("source_too_large", "Source exceeds configured byte limit")
        prompt = job.get("prompt")
        if prompt is None:
            context = build_context(source, job["state"], job["criteria"])
            prompt = build_prompt(context, job["reviewer"], max_bytes=prompt_max_bytes)
        if len(prompt.encode("utf-8")) > prompt_max_bytes:
            raise AdapterError("prompt_too_large", "Prompt exceeds configured byte limit")
        schema = files("peer_reviewer.schemas").joinpath("turn.json").read_text(encoding="utf-8")
        result = run_cli(
            build_argv(self.executable, schema, job.get("model")),
            prompt.encode("utf-8"),
            self.cwd,
            self.env,
            timeout_seconds,
            output_max_bytes,
            cancel_event=job.get("_cancel_event"),
        )
        check_process_result(result)
        try:
            envelope = json.loads(result["stdout"], object_pairs_hook=reject_duplicate_keys)
            structured = envelope["structured_output"]
            if not isinstance(structured, dict):
                raise TypeError("structured_output must be an object")
            raw_turn = json.dumps(structured, ensure_ascii=False, allow_nan=False).encode("utf-8")
            return parse_turn(raw_turn, job["expected"], job["state"], source)
        except (KeyError, TypeError, ValueError, ProtocolError, json.JSONDecodeError) as exc:
            if isinstance(exc, AdapterError):
                raise
            raise AdapterError("invalid_output", "Claude returned invalid structured output") from exc
