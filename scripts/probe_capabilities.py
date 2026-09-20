#!/usr/bin/env python3
"""Capability probe with an explicit safe stub for unavailable Claude Code."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


class UnavailableClaudeProbe:
    """Temporary probe used until Claude Code can be exercised with real OAuth."""

    def __init__(self, reason: str = "Claude capability probe has not run") -> None:
        self.reason = reason

    def probe(self) -> dict[str, Any]:
        return {
            "version": None,
            "subscription_auth": False,
            "statusline_in_print_mode": False,
            "preflight_before_first_turn": False,
            "preflight_after_reset": False,
            "headless": False,
            "blocking_reasons": [self.reason],
        }


def probe_capabilities(
    *,
    claude_probe: UnavailableClaudeProbe | None = None,
    codex_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Combine provider results without interpreting missing data as success."""
    claude = (claude_probe or UnavailableClaudeProbe()).probe()
    if codex_result is None:
        codex = {
            "version": None,
            "subscription_auth": False,
            "preflight_before_first_turn": False,
            "preflight_after_reset": False,
            "headless": False,
            "blocking_reasons": ["Codex capability probe has not run"],
        }
    else:
        codex = {**codex_result, "blocking_reasons": codex_result.get("blocking_reasons", [])}

    blocking_reasons = [
        str(reason)
        for result in (claude, codex)
        for reason in result.get("blocking_reasons", [])
    ]
    return {
        "versions": {"claude": claude.get("version"), "codex": codex.get("version")},
        "subscription_auth": bool(
            claude.get("subscription_auth") and codex.get("subscription_auth")
        ),
        "statusline_in_print_mode": bool(claude.get("statusline_in_print_mode")),
        "preflight_before_first_turn": {
            "A": bool(claude.get("preflight_before_first_turn")),
            "B": bool(codex.get("preflight_before_first_turn")),
        },
        "preflight_after_reset": {
            "A": bool(claude.get("preflight_after_reset")),
            "B": bool(codex.get("preflight_after_reset")),
        },
        "headless": bool(claude.get("headless") and codex.get("headless")),
        "blocking_reasons": blocking_reasons,
    }


def _read_result(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Capability result must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-result", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    result = probe_capabilities(codex_result=_read_result(args.codex_result))
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
