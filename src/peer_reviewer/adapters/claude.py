from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from peer_reviewer.adapters.process import run_cli
from peer_reviewer.adapters.shared import (
    AdapterError,
    RateLimitError,
    check_process_result,
    unknown_limit_sample,
)
from peer_reviewer.limits import account_fingerprint, normalize_limit, parse_claude_usage
from peer_reviewer.schemas import provider_turn_schema
from peer_reviewer.protocol import ProtocolError, parse_turn, reject_duplicate_keys
from peer_reviewer.prompts import build_context, build_prompt

LIMIT_SOURCE = "usage-command"
LIMIT_TIMEOUT_SECONDS = 60.0
LIMIT_OUTPUT_BYTES = 64 * 1024


def build_argv(
    executable: Path, schema: str, model: str | None = None, effort: str | None = None
) -> list[str]:
    argv = [
        str(executable),
        "-p",
    ]
    if model:
        argv.extend(["--model", model])
    if effort:
        argv.extend(["--effort", effort])
    argv.extend([
        "--output-format",
        "stream-json",
        "--verbose",
        "--json-schema",
        schema,
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
    ])
    return argv


def _stream_events(stdout: bytes) -> list[dict[str, Any]]:
    events = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        event = json.loads(line, object_pairs_hook=reject_duplicate_keys)
        if not isinstance(event, dict):
            raise TypeError("stream event must be an object")
        events.append(event)
    return events


def _raise_on_rate_limit(stdout: bytes) -> None:
    """Map in-stream rate-limit signals to RateLimitError, ignoring unparsable lines."""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict):
            continue
        info = event.get("rate_limit_info")
        rejected = event.get("type") == "rate_limit_event" and isinstance(info, dict) and info.get("status") == "rejected"
        throttled = event.get("type") == "result" and event.get("api_error_status") == 429
        if rejected or throttled:
            raise RateLimitError("rate_limit", "Reviewer CLI reported a subscription rate limit")


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

    def _now(self) -> datetime:
        return (self.clock or (lambda: datetime.now(timezone.utc)))()

    def _run_metadata(self, arguments: list[str], *, allow_exit_code: bool = False) -> dict[str, Any]:
        result = run_cli(
            [str(self.executable), *arguments],
            b"",
            self.cwd,
            self.env,
            LIMIT_TIMEOUT_SECONDS,
            LIMIT_OUTPUT_BYTES,
        )
        if not (allow_exit_code and result["returncode"] != 0 and not result["timed_out"]):
            check_process_result(result)
        value = json.loads(result["stdout"], object_pairs_hook=reject_duplicate_keys)
        if not isinstance(value, dict):
            raise TypeError("metadata output must be an object")
        return value

    def read_limits(self) -> dict[str, Any]:
        """Read the five-hour window via `claude -p /usage`, which runs no model turn."""
        if self.limit_reader is not None:
            return self.limit_reader()
        try:
            # `auth status` exits non-zero when logged out but still prints its JSON.
            status = self._run_metadata(["auth", "status", "--json"], allow_exit_code=True)
            if status.get("loggedIn") is not True or status.get("authMethod") != "claude.ai":
                raise AdapterError("authentication", "Claude is not logged in with a subscription")
            organization = status["orgId"]
            if not isinstance(organization, str) or not organization:
                raise ValueError("auth status lacks an organization")
            usage = self._run_metadata(
                ["-p", "/usage", "--output-format", "json", "--no-session-persistence"]
            )
            if usage.get("is_error") or usage.get("num_turns") != 0 or usage.get("total_cost_usd") != 0:
                raise ValueError("usage command did not run as a free local command")
            observed_at = self._now()
            windows = parse_claude_usage(str(usage.get("result", "")), observed_at)
        except (AdapterError, OSError, KeyError, TypeError, ValueError) as exc:
            sample = unknown_limit_sample("claude", self.account_fingerprint, self.model_bucket, self.clock)
            sample["source"] = LIMIT_SOURCE
            sample["other_blockers"] = [f"claude usage read failed: {getattr(exc, 'code', type(exc).__name__)}"]
            return sample
        raw = {
            "account_fingerprint": account_fingerprint("claude", organization),
            "rate_limits": windows,
        }
        return normalize_limit(
            "claude", raw, observed_at, self.account_fingerprint, self.model_bucket, source=LIMIT_SOURCE
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
        schema = json.dumps(provider_turn_schema(), separators=(",", ":"))
        result = run_cli(
            build_argv(self.executable, schema, job.get("model"), job.get("effort")),
            prompt.encode("utf-8"),
            self.cwd,
            self.env,
            timeout_seconds,
            output_max_bytes,
            cancel_event=job.get("_cancel_event"),
        )
        if not (result["timed_out"] or result.get("cancelled") or result.get("output_too_large")):
            _raise_on_rate_limit(result["stdout"])
        check_process_result(result)
        try:
            results = [event for event in _stream_events(result["stdout"]) if event.get("type") == "result"]
            if len(results) != 1 or results[0].get("is_error") is not False:
                raise ValueError("stream lacks exactly one successful result")
            structured = results[0]["structured_output"]
            if not isinstance(structured, dict):
                raise TypeError("structured_output must be an object")
            raw_turn = json.dumps(structured, ensure_ascii=False, allow_nan=False).encode("utf-8")
            return parse_turn(raw_turn, job["expected"], job["state"], source)
        except (KeyError, TypeError, ValueError, ProtocolError, json.JSONDecodeError) as exc:
            if isinstance(exc, AdapterError):
                raise
            raise AdapterError("invalid_output", "Claude returned invalid structured output") from exc
