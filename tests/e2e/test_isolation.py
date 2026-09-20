from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]


@pytest.mark.docker
def test_compose_workers_have_disjoint_mounts_and_hardened_runtime():
    completed = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    config = json.loads(completed.stdout)
    services = config["services"]
    engine_targets = {mount["target"] for mount in services["engine"]["volumes"]}
    assert {"/input", "/sessions"} <= engine_targets
    input_mount = next(mount for mount in services["engine"]["volumes"] if mount["target"] == "/input")
    assert input_mount["read_only"] is True
    for name in ("claude", "codex"):
        service = services[name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service.get("tty", False) is False
        assert service.get("stdin_open", False) is False
        assert service["restart"] == "unless-stopped"
        assert service["user"] != "0"
        assert service["pids_limit"] > 0
        assert int(service["mem_limit"]) >= 512 * 1024 * 1024

    claude_targets = {mount["target"] for mount in services["claude"]["volumes"]}
    codex_targets = {mount["target"] for mount in services["codex"]["volumes"]}
    assert claude_targets == {"/mailbox/inbox", "/mailbox/outbox", "/work", "/auth"}
    assert codex_targets == {"/mailbox/inbox", "/mailbox/outbox", "/work", "/auth"}
    assert services["claude"]["volumes"] != services["codex"]["volumes"]
    forbidden = {"/sessions", "/control", "/var/run/docker.sock", "/root", "/home"}
    assert not claude_targets & forbidden
    assert not codex_targets & forbidden


@pytest.mark.docker
def test_no_api_keys_are_forwarded_to_reviewers():
    completed = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    services = json.loads(completed.stdout)["services"]
    for name in ("claude", "codex"):
        environment = services[name].get("environment", {})
        assert all("API_KEY" not in key and "ANTHROPIC" not in key for key in environment)
