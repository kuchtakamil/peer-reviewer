from pathlib import Path

import pytest

from peer_reviewer.config import ConfigError, load_config, validate_resume_config


VALID_CONFIG = """
[session]
max_rounds = 5
veto_seconds = 60
criteria = ["technical correctness", "missing assumptions"]

[limits]
minimum_remaining_percent = 20
max_age_seconds = 60

[process]
source_max_bytes = 65536
prompt_max_bytes = 524288
output_max_bytes = 1048576
timeout_seconds = 900

[reviewers.claude]
model = "claude-fixture"
cli_version = "unverified"
account_fingerprint = "fixture:claude"
model_bucket = "subscription"

[reviewers.codex]
model = "codex-fixture"
cli_version = "0.155.1"
account_fingerprint = "fixture:codex"
model_bucket = "fixture-bucket"
"""


def write_config(path: Path, text: str = VALID_CONFIG) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_config_applies_validated_process_defaults(tmp_path):
    config = load_config(write_config(tmp_path / "config.toml"))

    assert config["session"]["max_rounds"] == 5
    assert config["session"]["veto_seconds"] == 60
    assert config["limits"] == {
        "minimum_remaining_percent": 20.0,
        "max_age_seconds": 60,
    }
    assert config["reviewers"]["claude"]["model"] == "claude-fixture"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("max_rounds = 5", "max_rounds = 0", "max_rounds"),
        ("minimum_remaining_percent = 20", "minimum_remaining_percent = 101", "minimum_remaining_percent"),
        ('model = "claude-fixture"', 'model = ""', "model"),
    ],
)
def test_load_config_rejects_unsafe_values(tmp_path, old, new, message):
    path = write_config(tmp_path / "config.toml", VALID_CONFIG.replace(old, new, 1))

    with pytest.raises(ConfigError, match=message):
        load_config(path)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("max_rounds = 5", "max_rounds = 4", "max_rounds"),
        ('model = "codex-fixture"', 'model = "codex-other"', "reviewers"),
        ('criteria = ["technical correctness", "missing assumptions"]', 'criteria = ["style"]', "criteria"),
    ],
)
def test_resume_rejects_changes_to_immutable_session_fields(tmp_path, old, new, message):
    saved = load_config(write_config(tmp_path / "saved.toml"))
    current = load_config(write_config(tmp_path / "current.toml", VALID_CONFIG.replace(old, new, 1)))

    with pytest.raises(ConfigError, match=message):
        validate_resume_config(saved, current)
