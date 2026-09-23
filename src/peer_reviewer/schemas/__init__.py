"""JSON schemas shipped with peer-reviewer."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

# Keywords rejected by provider structured-output validators: Claude Code does not
# accept the draft 2020-12 `$schema` URI, and OpenAI strict mode rejects
# `uniqueItems` and `minLength`. `protocol.parse_turn` still enforces the full schema.
UNSUPPORTED_PROVIDER_KEYWORDS = frozenset({"$schema", "uniqueItems", "minLength"})


def turn_schema() -> dict[str, Any]:
    return json.loads(files(__name__).joinpath("turn.json").read_text(encoding="utf-8"))


def _strip(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip(item) for key, item in value.items() if key not in UNSUPPORTED_PROVIDER_KEYWORDS}
    if isinstance(value, list):
        return [_strip(item) for item in value]
    return value


def provider_turn_schema() -> dict[str, Any]:
    """Turn schema reduced to the subset reviewer CLIs accept for structured output."""
    return _strip(turn_schema())
