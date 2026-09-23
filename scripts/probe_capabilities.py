#!/usr/bin/env python3
"""Build the live capability contract from worker limit probes.

Each worker prints one limit reading with `docker compose run --rm <worker> --probe`.
Both readings come from non-generative sources (`claude -p /usage` and Codex App
Server `account/rateLimits/read`), so probing never spends subscription quota.
Missing or unverified readings are blocking reasons, never a PASS.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

PROVIDERS = {"A": "claude", "B": "codex"}
NON_GENERATIVE_SOURCES = {"claude": "usage-command", "codex": "app-server"}
# Verified on Claude Code 2.1.280: the status line command never runs under `-p`.
STATUSLINE_IN_PRINT_MODE = False


def _provider_result(provider: str, reading: dict[str, Any] | None, version: str | None) -> dict[str, Any]:
    if not isinstance(reading, dict):
        return {"version": version, "verified": False, "blocking_reasons": [f"{provider} probe has not run"]}
    reasons: list[str] = []
    if reading.get("provider") != provider:
        reasons.append(f"{provider} probe reported provider {reading.get('provider')!r}")
    if reading.get("source") != NON_GENERATIVE_SOURCES[provider]:
        reasons.append(f"{provider} limit source {reading.get('source')!r} is not the non-generative reader")
    if reading.get("confidence") != "verified" or not isinstance(reading.get("five_hour"), dict):
        details = [str(item) for item in reading.get("other_blockers", [])] or ["unverified reading"]
        reasons.append(f"{provider}: " + "; ".join(details))
    if version is None:
        reasons.append(f"{provider} CLI version unknown")
    return {"version": version, "verified": not reasons, "blocking_reasons": reasons}


def probe_capabilities(
    readings: dict[str, dict[str, Any] | None],
    versions: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Combine per-provider probe readings without interpreting missing data as success."""
    versions = versions or {}
    results = {
        reviewer: _provider_result(provider, readings.get(provider), versions.get(provider))
        for reviewer, provider in PROVIDERS.items()
    }
    # The readers query account state on the server, independent of any prior turn in
    # the worker, so the same check covers the first round and the round after a reset.
    preflight = {reviewer: result["verified"] for reviewer, result in results.items()}
    return {
        "versions": {provider: results[reviewer]["version"] for reviewer, provider in PROVIDERS.items()},
        "subscription_auth": all(preflight.values()),
        "statusline_in_print_mode": STATUSLINE_IN_PRINT_MODE,
        "preflight_before_first_turn": preflight,
        "preflight_after_reset": dict(preflight),
        "headless": all(preflight.values()),
        "blocking_reasons": [reason for result in results.values() for reason in result["blocking_reasons"]],
    }


def _compose(arguments: list[str]) -> str:
    completed = subprocess.run(
        ["docker", "compose", "run", "--rm", "--no-deps", "-T", *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return completed.stdout


def run_live_probes() -> tuple[dict[str, dict[str, Any] | None], dict[str, str | None]]:
    readings: dict[str, dict[str, Any] | None] = {}
    versions: dict[str, str | None] = {}
    for provider in PROVIDERS.values():
        try:
            readings[provider] = json.loads(_compose([provider, "--probe"]))
        except (subprocess.SubprocessError, OSError, ValueError):
            readings[provider] = None
        try:
            output = _compose(["--entrypoint", provider, provider, "--version"]).split()
            versions[provider] = next((token for token in output if token[:1].isdigit()), None)
        except (subprocess.SubprocessError, OSError):
            versions[provider] = None
    return readings, versions


def _read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("probe reading must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="run both worker probes via docker compose")
    parser.add_argument("--claude-reading", type=Path)
    parser.add_argument("--codex-reading", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    if args.live:
        readings, versions = run_live_probes()
    else:
        readings = {"claude": _read_json(args.claude_reading), "codex": _read_json(args.codex_reading)}
        versions = {}
    result = probe_capabilities(readings, versions)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.write_text(rendered, encoding="utf-8")
    return 0 if not result["blocking_reasons"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
