from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from peer_reviewer.adapters.app_server import read_account_limits
from peer_reviewer.adapters.process import run_cli
from peer_reviewer.adapters.shared import AdapterError, check_process_result, unknown_limit_sample
from peer_reviewer.limits import account_fingerprint, normalize_limit
from peer_reviewer.protocol import ProtocolError, parse_turn
from peer_reviewer.schemas import provider_turn_schema
from peer_reviewer.prompts import build_context, build_prompt

LIMIT_SOURCE = "app-server"
LIMIT_TIMEOUT_SECONDS = 30.0


def build_argv(
    executable: Path,
    schema_path: Path,
    output_path: Path,
    model: str | None = None,
    effort: str | None = None,
) -> list[str]:
    # `--ask-for-approval` is a top-level option; `codex exec` rejects it.
    argv = [
        str(executable),
        "--ask-for-approval",
        "never",
        "exec",
    ]
    if model:
        argv.extend(["--model", model])
    if effort:
        # `codex exec` has no effort flag; the config override is the supported route.
        argv.extend(["-c", f'model_reasoning_effort="{effort}"'])
    argv.extend([
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
        "-",
    ])
    return argv


class CodexAdapter:
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
        """Read the five-hour window via App Server `account/rateLimits/read` (no turn)."""
        if self.limit_reader is not None:
            return self.limit_reader()
        try:
            account, limits = read_account_limits(
                self.executable,
                self.cwd,
                self.env,
                min(LIMIT_TIMEOUT_SECONDS, self.timeout_seconds),
            )
            details = account.get("account")
            if not isinstance(details, dict) or details.get("type") != "chatgpt":
                raise AdapterError("authentication", "Codex is not logged in with a ChatGPT subscription")
            account_id = limits.get("accountId")
            if not isinstance(account_id, str) or not account_id:
                raise ValueError("rate limits lack an account identifier")
        except (AdapterError, OSError, ValueError) as exc:
            sample = unknown_limit_sample("codex", self.account_fingerprint, self.model_bucket, self.clock)
            sample["source"] = LIMIT_SOURCE
            sample["other_blockers"] = [f"codex limit read failed: {getattr(exc, 'code', type(exc).__name__)}"]
            return sample
        observed_at = (self.clock or (lambda: datetime.now(timezone.utc)))()
        raw = {"account_fingerprint": account_fingerprint("codex", account_id), "rate_limits": limits}
        return normalize_limit(
            "codex", raw, observed_at, self.account_fingerprint, self.model_bucket, source=LIMIT_SOURCE
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

        descriptor, schema_name = tempfile.mkstemp(prefix="codex-schema-", suffix=".json", dir=self.cwd)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(provider_turn_schema(), handle)
        schema_path = Path(schema_name)
        descriptor, output_name = tempfile.mkstemp(prefix="codex-last-", suffix=".json", dir=self.cwd)
        os.close(descriptor)
        output_path = Path(output_name)
        try:
            result = run_cli(
                build_argv(
                    self.executable, schema_path, output_path, job.get("model"), job.get("effort")
                ),
                prompt.encode("utf-8"),
                self.cwd,
                self.env,
                timeout_seconds,
                output_max_bytes,
                cancel_event=job.get("_cancel_event"),
            )
            check_process_result(result)
            raw_turn = output_path.read_bytes()
            if len(raw_turn) > output_max_bytes:
                raise AdapterError("output_too_large", "Codex final output exceeds byte limit")
            return parse_turn(raw_turn, job["expected"], job["state"], source)
        except AdapterError:
            raise
        except (OSError, ProtocolError) as exc:
            raise AdapterError("invalid_output", "Codex returned invalid final output") from exc
        finally:
            output_path.unlink(missing_ok=True)
            schema_path.unlink(missing_ok=True)
