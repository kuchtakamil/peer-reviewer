from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


DEFAULT_SESSION = {"max_rounds": 5, "veto_seconds": 60}
DEFAULT_LIMITS = {"minimum_remaining_percent": 20.0, "max_age_seconds": 60}
DEFAULT_PROCESS = {
    "source_max_bytes": 64 * 1024,
    "prompt_max_bytes": 512 * 1024,
    "output_max_bytes": 1024 * 1024,
    "timeout_seconds": 15 * 60,
}


def _positive_int(value: Any, name: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{name} must be an integer >= {minimum}")
    return value


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value


def load_config(path: Path) -> dict[str, Any]:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot load config: {exc}") from exc

    session = {**DEFAULT_SESSION, **raw.get("session", {})}
    limits = {**DEFAULT_LIMITS, **raw.get("limits", {})}
    process = {**DEFAULT_PROCESS, **raw.get("process", {})}
    reviewers = raw.get("reviewers")

    session["max_rounds"] = _positive_int(session.get("max_rounds"), "max_rounds")
    session["veto_seconds"] = _positive_int(session.get("veto_seconds"), "veto_seconds", minimum=0)
    criteria = session.get("criteria")
    if not isinstance(criteria, list) or not criteria or any(
        not isinstance(item, str) or not item.strip() for item in criteria
    ):
        raise ConfigError("criteria must be a non-empty list of strings")

    threshold = limits.get("minimum_remaining_percent")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ConfigError("minimum_remaining_percent must be numeric")
    threshold = float(threshold)
    if not 0 <= threshold <= 100:
        raise ConfigError("minimum_remaining_percent must be between 0 and 100")
    limits["minimum_remaining_percent"] = threshold
    limits["max_age_seconds"] = _positive_int(limits.get("max_age_seconds"), "max_age_seconds")

    for name in DEFAULT_PROCESS:
        process[name] = _positive_int(process.get(name), name)

    if not isinstance(reviewers, dict) or set(reviewers) != {"claude", "codex"}:
        raise ConfigError("reviewers must define exactly claude and codex")
    normalized_reviewers: dict[str, dict[str, str]] = {}
    for provider in ("claude", "codex"):
        reviewer = reviewers[provider]
        if not isinstance(reviewer, dict):
            raise ConfigError(f"reviewers.{provider} must be a table")
        normalized_reviewers[provider] = {
            field: _nonempty_string(reviewer.get(field), f"reviewers.{provider}.{field}")
            for field in ("model", "cli_version", "account_fingerprint", "model_bucket")
        }

    return {
        "session": copy.deepcopy(session),
        "limits": copy.deepcopy(limits),
        "process": copy.deepcopy(process),
        "reviewers": normalized_reviewers,
    }


def validate_resume_config(saved: dict[str, Any], current: dict[str, Any]) -> None:
    checks = {
        "max_rounds": (saved["session"]["max_rounds"], current["session"]["max_rounds"]),
        "criteria": (saved["session"]["criteria"], current["session"]["criteria"]),
        "reviewers": (saved["reviewers"], current["reviewers"]),
    }
    changed = [name for name, (before, after) in checks.items() if before != after]
    if changed:
        raise ConfigError("Immutable session config changed: " + ", ".join(changed))
