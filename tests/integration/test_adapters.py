import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from peer_reviewer.adapters.claude import ClaudeAdapter, build_argv as build_claude_argv
from peer_reviewer.adapters.codex import CodexAdapter, build_argv as build_codex_argv
from peer_reviewer.adapters.process import run_cli
from peer_reviewer.adapters.shared import AdapterError, RateLimitError
from peer_reviewer.limits import account_fingerprint


FIXTURES = Path(__file__).parents[1] / "fixtures" / "turns"
STATE_HASH = "sha256:" + "1" * 64
SOURCE = "Opening line.\nRisky guarantee.\n"


def test_document_is_stdin_not_shell_code(tmp_path):
    result = run_cli(
        [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
        b"$(touch injected) `touch injected2`",
        tmp_path,
        {},
        10,
        4096,
    )
    assert result["returncode"] == 0
    assert not (tmp_path / "injected").exists()
    assert not (tmp_path / "injected2").exists()


def test_process_drains_stdout_and_stderr_without_deadlock(tmp_path):
    code = "import os; os.write(1,b'o'*100000); os.write(2,b'e'*100000)"
    result = run_cli([sys.executable, "-c", code], b"", tmp_path, {}, 10, 300000)
    assert len(result["stdout"]) == 100000
    assert len(result["stderr"]) == 100000
    assert result["timed_out"] is False


def test_timeout_terminates_process_group(tmp_path):
    started = time.monotonic()
    result = run_cli(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        b"",
        tmp_path,
        {},
        0.1,
        4096,
    )
    assert result["timed_out"] is True
    assert result["returncode"] != 0
    assert time.monotonic() - started < 5


def test_cancel_event_terminates_process_group(tmp_path):
    cancel = threading.Event()
    timer = threading.Timer(0.05, cancel.set)
    timer.start()
    try:
        result = run_cli(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            b"",
            tmp_path,
            {},
            10,
            4096,
            cancel_event=cancel,
        )
    finally:
        timer.cancel()
    assert result["cancelled"] is True
    assert result["returncode"] != 0


def test_oversized_output_is_reported_without_returning_the_payload(tmp_path):
    result = run_cli(
        [sys.executable, "-c", "import sys; sys.stdout.write('x'*5000)"],
        b"",
        tmp_path,
        {},
        10,
        1024,
    )
    assert result["output_too_large"] is True
    assert len(result["stdout"]) <= 1024


def test_unbounded_output_is_killed_as_soon_as_byte_limit_is_crossed(tmp_path):
    code = "import os\nwhile True: os.write(1,b'x'*4096)"
    started = time.monotonic()
    result = run_cli([sys.executable, "-c", code], b"", tmp_path, {}, 5, 1024)
    assert result["output_too_large"] is True
    assert result["timed_out"] is False
    assert time.monotonic() - started < 2


def make_executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def adapter_job(reviewer: str) -> dict:
    return {
        "reviewer": reviewer,
        "prompt": "review this immutable document",
        "expected": {
            "session_id": "s-001",
            "round_no": 1,
            "reviewer": reviewer,
            "input_state_hash": STATE_HASH,
        },
        "state": {"issues": {}, "versions": {}, "arguments": {}, "positions": {}},
        "source": SOURCE,
    }


def adapter_options(tmp_path: Path) -> dict:
    return {
        "cwd": tmp_path,
        "env": {"PATH": os.environ["PATH"]},
        "timeout_seconds": 5,
        "max_output_bytes": 1024 * 1024,
        "prompt_max_bytes": 512 * 1024,
        "source_max_bytes": 64 * 1024,
        "account_fingerprint": "fixture:account",
        "model_bucket": "subscription",
    }


def test_claude_adapter_accepts_only_successful_structured_output(tmp_path):
    value = json.loads((FIXTURES / "round1-a.json").read_text())
    executable = make_executable(
        tmp_path / "claude-fake",
        f"import json\nprint(json.dumps({{'type': 'result', 'is_error': False, 'structured_output': {value!r}}}))\n",
    )
    adapter = ClaudeAdapter(executable, **adapter_options(tmp_path))
    assert adapter.review(adapter_job("A"))["reviewer"] == "A"


def test_job_process_limits_and_model_override_adapter_defaults(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, stdin, cwd, env, timeout_seconds, max_output_bytes, **kwargs):
        captured.update(argv=argv, timeout=timeout_seconds, output=max_output_bytes)
        value = json.loads((FIXTURES / "round1-a.json").read_text())
        return {
            "returncode": 0,
            "stdout": json.dumps({"type": "result", "is_error": False, "structured_output": value}).encode(),
            "stderr": b"",
            "timed_out": False,
            "output_too_large": False,
            "cancelled": False,
        }

    monkeypatch.setattr("peer_reviewer.adapters.claude.run_cli", fake_run)
    adapter = ClaudeAdapter(tmp_path / "claude", **adapter_options(tmp_path))
    request = adapter_job("A") | {
        "model": "pinned-model",
        "effort": "xhigh",
        "process": {
            "source_max_bytes": 1024,
            "prompt_max_bytes": 2048,
            "output_max_bytes": 4096,
            "timeout_seconds": 7,
        },
    }
    adapter.review(request)
    assert captured["timeout"] == 7
    assert captured["output"] == 4096
    assert captured["argv"][captured["argv"].index("--model") + 1] == "pinned-model"
    assert captured["argv"][captured["argv"].index("--effort") + 1] == "xhigh"


def test_nonzero_exit_rejects_even_valid_claude_json_without_echoing_secret(tmp_path):
    executable = make_executable(
        tmp_path / "claude-fail",
        "import json,sys\nprint(json.dumps({'type': 'result', 'structured_output': {}}))\nprint('sk-ant-secret', file=sys.stderr)\nsys.exit(3)\n",
    )
    adapter = ClaudeAdapter(executable, **adapter_options(tmp_path))
    with pytest.raises(AdapterError) as caught:
        adapter.review(adapter_job("A"))
    assert "sk-ant-secret" not in str(caught.value)
    assert caught.value.code == "process_exit"


@pytest.mark.parametrize(
    ("stderr", "expected_code"),
    [("HTTP 429 rate_limit", "rate_limit"), ("authentication login required", "authentication")],
)
def test_provider_failures_have_safe_machine_readable_codes(tmp_path, stderr, expected_code):
    executable = make_executable(
        tmp_path / f"claude-{expected_code}",
        f"import sys\nprint({stderr!r}, file=sys.stderr)\nsys.exit(1)\n",
    )
    adapter = ClaudeAdapter(executable, **adapter_options(tmp_path))
    with pytest.raises(AdapterError) as caught:
        adapter.review(adapter_job("A"))
    assert caught.value.code == expected_code
    assert stderr not in str(caught.value)


def test_truncated_claude_json_is_never_accepted(tmp_path):
    executable = make_executable(
        tmp_path / "claude-truncated",
        "print('{\"structured_output\":')\n",
    )
    adapter = ClaudeAdapter(executable, **adapter_options(tmp_path))
    with pytest.raises(AdapterError) as caught:
        adapter.review(adapter_job("A"))
    assert caught.value.code == "invalid_output"


def test_codex_reads_only_the_final_output_file(tmp_path):
    value = json.loads((FIXTURES / "round1-b.json").read_text())
    body = (
        "import json,sys\n"
        "path=sys.argv[sys.argv.index('--output-last-message')+1]\n"
        f"open(path,'w',encoding='utf-8').write(json.dumps({value!r}))\n"
        "print(json.dumps({'misleading':'jsonl event'}))\n"
    )
    executable = make_executable(tmp_path / "codex-fake", body)
    adapter = CodexAdapter(executable, **adapter_options(tmp_path))
    assert adapter.review(adapter_job("B"))["reviewer"] == "B"


def test_missing_claude_executable_yields_conservative_unknown_limit(tmp_path):
    adapter = ClaudeAdapter(tmp_path / "missing-claude", **adapter_options(tmp_path))
    reading = adapter.read_limits()
    assert reading["confidence"] == "unknown"
    assert reading["five_hour"] is None
    assert reading["source"] == "usage-command"


LIMIT_FIXTURES = Path(__file__).parents[1] / "fixtures" / "limits"
USAGE_NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
CLAUDE_ORG = json.loads((LIMIT_FIXTURES / "claude_auth_status.json").read_text())["orgId"]


def fake_claude_cli(tmp_path: Path, *, auth: dict | None = None, usage: dict | None = None) -> Path:
    auth = auth or json.loads((LIMIT_FIXTURES / "claude_auth_status.json").read_text())
    usage = usage or json.loads((LIMIT_FIXTURES / "claude_usage_print_mode.json").read_text())
    body = (
        "import json,sys\n"
        "args=sys.argv[1:]\n"
        f"if args[:2]==['auth','status']: print(json.dumps({auth!r}))\n"
        f"elif '-p' in args and '/usage' in args: print(json.dumps({usage!r}))\n"
        "else: sys.exit(9)\n"
    )
    return make_executable(tmp_path / "claude-limits", body)


def claude_limit_adapter(tmp_path: Path, executable: Path) -> ClaudeAdapter:
    options = adapter_options(tmp_path) | {
        "account_fingerprint": account_fingerprint("claude", CLAUDE_ORG),
        "clock": lambda: USAGE_NOW,
    }
    return ClaudeAdapter(executable, **options)


def test_claude_limits_come_from_non_generative_usage_command(tmp_path):
    reading = claude_limit_adapter(tmp_path, fake_claude_cli(tmp_path)).read_limits()
    assert reading["confidence"] == "verified"
    assert reading["source"] == "usage-command"
    assert reading["five_hour"] == {
        "used_percent": 4.0,
        "resets_at": "2026-09-23T14:20:00Z",
        "window_seconds": 18000,
    }


@pytest.mark.parametrize(
    ("auth", "usage"),
    [
        ({"loggedIn": False}, None),
        ({"loggedIn": True, "authMethod": "api_key", "orgId": CLAUDE_ORG}, None),
        (None, {"type": "result", "num_turns": 1, "total_cost_usd": 0.01, "result": "Current session: 4% used"}),
        (None, {"type": "result", "num_turns": 0, "total_cost_usd": 0, "result": "/usage isn't available"}),
    ],
)
def test_claude_limits_are_unknown_without_subscription_or_usage_report(tmp_path, auth, usage):
    reading = claude_limit_adapter(tmp_path, fake_claude_cli(tmp_path, auth=auth, usage=usage)).read_limits()
    assert reading["confidence"] == "unknown"
    assert reading["five_hour"] is None


def test_logged_out_claude_reports_authentication_not_process_failure(tmp_path):
    body = "import json,sys\nprint(json.dumps({'loggedIn': False, 'authMethod': 'none'}))\nsys.exit(1)\n"
    reading = claude_limit_adapter(tmp_path, make_executable(tmp_path / "claude-out", body)).read_limits()
    assert reading["confidence"] == "unknown"
    assert reading["other_blockers"] == ["claude usage read failed: authentication"]


def test_claude_limits_for_other_account_report_observed_fingerprint(tmp_path):
    options = adapter_options(tmp_path) | {"clock": lambda: USAGE_NOW}
    reading = ClaudeAdapter(fake_claude_cli(tmp_path), **options).read_limits()
    assert reading["confidence"] == "unknown"
    assert reading["observed_account_fingerprint"] == account_fingerprint("claude", CLAUDE_ORG)


def test_claude_stream_rate_limit_rejection_is_a_rate_limit_error(tmp_path):
    events = [
        {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"}},
        {"type": "result", "is_error": True, "api_error_status": 429, "result": "limit reached"},
    ]
    body = "import json,sys\n" + "".join(f"print(json.dumps({event!r}))\n" for event in events) + "sys.exit(1)\n"
    adapter = ClaudeAdapter(make_executable(tmp_path / "claude-limited", body), **adapter_options(tmp_path))
    with pytest.raises(RateLimitError):
        adapter.review(adapter_job("A"))


def test_claude_error_result_is_never_accepted(tmp_path):
    value = json.loads((FIXTURES / "round1-a.json").read_text())
    event = {"type": "result", "is_error": True, "structured_output": value}
    adapter = ClaudeAdapter(
        make_executable(tmp_path / "claude-error", f"import json\nprint(json.dumps({event!r}))\n"),
        **adapter_options(tmp_path),
    )
    with pytest.raises(AdapterError) as caught:
        adapter.review(adapter_job("A"))
    assert caught.value.code == "invalid_output"


CODEX_ACCOUNT_ID = json.loads((LIMIT_FIXTURES / "codex_app_server_rate_limits.json").read_text())["accountId"]


def fake_codex_app_server(tmp_path: Path, *, account_type: str | None = "chatgpt", hang: bool = False) -> Path:
    limits = json.loads((LIMIT_FIXTURES / "codex_app_server_rate_limits.json").read_text())
    account = {
        "account": {"type": account_type, "planType": "plus"} if account_type else None,
        "requiresOpenaiAuth": True,
    }
    body = (
        "import json,sys,time\n"
        "assert sys.argv[1:]==['app-server']\n"
        f"hang={hang!r}\n"
        f"results={{'initialize': {{'userAgent': 'fake'}}, 'account/read': {account!r}, 'account/rateLimits/read': {limits!r}}}\n"
        "for line in sys.stdin:\n"
        "    message=json.loads(line)\n"
        "    if 'id' not in message: continue\n"
        "    if hang: time.sleep(60)\n"
        "    print(json.dumps({'method':'account/updated','params':{}}), flush=True)\n"
        "    print(json.dumps({'id':message['id'],'result':results[message['method']]}), flush=True)\n"
    )
    return make_executable(tmp_path / "codex-app-server", body)


def codex_limit_adapter(tmp_path: Path, executable: Path, **overrides) -> CodexAdapter:
    options = adapter_options(tmp_path) | {
        "account_fingerprint": account_fingerprint("codex", CODEX_ACCOUNT_ID),
        "model_bucket": "codex",
        "clock": lambda: USAGE_NOW,
    } | overrides
    return CodexAdapter(executable, **options)


def test_codex_limits_come_from_app_server_without_a_turn(tmp_path):
    reading = codex_limit_adapter(tmp_path, fake_codex_app_server(tmp_path)).read_limits()
    assert reading["confidence"] == "verified"
    assert reading["source"] == "app-server"
    assert reading["five_hour"]["used_percent"] == 73.0


def test_logged_out_codex_reports_authentication(tmp_path):
    reading = codex_limit_adapter(tmp_path, fake_codex_app_server(tmp_path, account_type=None)).read_limits()
    assert reading["other_blockers"] == ["codex limit read failed: authentication"]


def test_codex_api_key_account_is_not_a_subscription_limit(tmp_path):
    reading = codex_limit_adapter(tmp_path, fake_codex_app_server(tmp_path, account_type="apiKey")).read_limits()
    assert reading["confidence"] == "unknown"


def test_hanging_codex_app_server_times_out_as_unknown(tmp_path):
    started = time.monotonic()
    adapter = codex_limit_adapter(tmp_path, fake_codex_app_server(tmp_path, hang=True), timeout_seconds=0.5)
    reading = adapter.read_limits()
    assert reading["confidence"] == "unknown"
    assert time.monotonic() - started < 5


def test_review_argv_disable_tools_persistence_and_write_access(tmp_path):
    claude = build_claude_argv(Path("claude"), "{}", "claude-pinned", "high")
    codex = build_codex_argv(Path("codex"), tmp_path / "schema.json", tmp_path / "last.json", "codex-pinned", "xhigh")
    assert claude == [
        "claude", "-p", "--model", "claude-pinned", "--effort", "high", "--output-format", "stream-json", "--verbose",
        "--json-schema", "{}",
        "--tools", "", "--permission-mode", "dontAsk", "--no-session-persistence",
    ]
    assert codex == [
        "codex", "--ask-for-approval", "never", "exec", "--model", "codex-pinned",
        "-c", 'model_reasoning_effort="xhigh"',
        "--skip-git-repo-check", "--sandbox", "read-only", "--ephemeral", "--output-schema",
        str(tmp_path / "schema.json"), "--output-last-message", str(tmp_path / "last.json"), "-",
    ]
