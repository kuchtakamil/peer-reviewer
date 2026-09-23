# Feasibility of subscription limit preflight

Status as of 2026-09-23: **limit preflight GO for both providers**. A live
end-to-end review session passed on the host. Production still requires the
container OAuth login (see the end of this document).

Tested versions: Claude Code 2.1.280, Codex CLI 0.156.1. Pin these in
`docker/Dockerfile.*` and `config/reviewer.toml`; re-run this matrix before
bumping either version.

## Claude Code

| Channel | Result |
|---|---|
| Status line under `claude -p` | **Does not run.** A probe script configured through `--settings` never executed during `claude -p "say hi"`. The path suggested in `claude-code-get-used-limit.md` is not available in print mode. |
| `--output-format stream-json` | Emits `{"type":"rate_limit_event","rate_limit_info":{"status":…,"unifiedWindows":{"five_hour":{"utilization":0.02,"resetsAt":…}}}}`. `utilization` is a 0–1 fraction. The event only appears **after** a model response, so it cannot be used as a preflight check. |
| `claude -p /usage --output-format json` | **Chosen preflight.** Runs as a local command with `num_turns: 0`, `total_cost_usd: 0`, `duration_api_ms: 0`. Its `result` text contains `Current session: N% used · resets Mon D, H:MMam (Zone)` and a weekly line. |
| `claude -p /status` | Not available in print mode. |
| `GET /api/oauth/usage` | Returns the same data as JSON; `/usage` uses this endpoint. It is undocumented and would require reading the OAuth token, so the CLI command is used instead. |

On 2026-09-23 the `/usage` values matched `/api/oauth/usage` and the
`rate_limit_event` data: 3–4 % used, and all three reported the same reset
(14:20 UTC).

Implementation (`adapters/claude.py`, `limits.parse_claude_usage`):

1. `claude auth status --json` must report `loggedIn: true` and
   `authMethod: "claude.ai"`. The account fingerprint is a hash of `orgId`.
2. `claude -p /usage` must report zero turns and zero cost. This guards against
   `/usage` ever becoming a model turn.
3. The text is parsed strictly. Any unrecognized layout yields `unknown`, which
   triggers `PAUSED_LIMIT_UNKNOWN`, never a guess. The displayed reset is
   truncated to the minute, so the parser rounds it up by one minute. The year
   is inferred as the nearest plausible date. Workers run with `TZ=UTC`.
4. `Current session: 0% used` without a reset time is treated as an idle window
   that resets within five hours. This layout has not been observed live yet. If
   the real output differs, the parser returns `unknown`, which is safe but
   blocks until a window exists.

During a review turn the adapter uses `stream-json`. A `rate_limit_event` with
`status: "rejected"`, or a result with `api_error_status: 429`, becomes a
`rate_limit` error, and the engine pauses instead of retrying.

## Codex

`codex app-server` over stdio: `initialize` → `initialized` → `account/read` →
`account/rateLimits/read`. No thread or turn is started.

- `account/read` must report `account.type == "chatgpt"`. The fingerprint is a
  hash of `accountId`.
- The bucket is `rateLimitsByLimitId["codex"]`, and the window is the one with
  `windowDurationMins == 300`. Codex does not guarantee that `primary` is the
  five-hour window.
- An exhausted non-five-hour window (e.g. weekly, 10080 min) is a blocker with
  its own reset.
- Observed live: with the five-hour window at 100 %, the server reports
  `ordinaryUsageAllowed: false` and `rateLimitReachedType: "rate_limit_reached"`,
  and silently switches interactive use to a cheaper model ("Luna"). The engine
  treats this as `PAUSED_LIMIT_LOW` until the known reset. `ordinaryUsageAllowed:
  false` without an exhausted window is `unknown`.

## Account billing observations (2026-09-23)

- The Claude account had **extra usage enabled** (monthly EUR limit, credits
  already used). Disable it before deployment. Otherwise reviews past the
  subscription limit may be billed instead of paused.
- The Codex account reported `credits.hasCredits: false`.

## Probing

```bash
docker compose run --rm claude --probe    # one limit reading + cli_version
docker compose run --rm codex --probe
python scripts/probe_capabilities.py --live --output capability.json
PEER_REVIEWER_CAPABILITY_RESULT=capability.json uv run pytest -m live tests/integration/test_capabilities.py
```

`--probe` prints `observed_account_fingerprint` when the configured fingerprint
does not match. Copy that value into `config/reviewer.toml`.

## Remaining before production GO

- Log in to OAuth inside each worker's volume (`docs/operations.md`) and obtain
  verified `--probe` readings from the containers.
- Disable extra usage / credits on both accounts.
- Pin the model names in `config/reviewer.toml`.
- Repeat the end-to-end run inside the containers. It passed on the host on
  2026-09-23; see `docs/operations.md`.
- Observe `/usage` output once in an idle window (no active five-hour window) to
  confirm the idle-window parser branch.
