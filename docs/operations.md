# Peer Reviewer — operations

## Deployment status

The limit preflight works for both providers. Claude uses `claude -p /usage`,
Codex uses App Server `account/rateLimits/read`; neither reading runs a model turn
(see `docs/feasibility.md`). The worker images install the pinned CLIs: Claude Code
2.1.280 and Codex 0.156.1. Production GO still requires the container OAuth login
below, disabled extra usage on both accounts, pinned model names, and one real
review run.

## OAuth and billing controls

Each CLI logs in once into its own named volume. The Claude worker receives only
`claude-auth` (`CLAUDE_CONFIG_DIR=/auth/claude`); the Codex worker receives only
`codex-auth` (`CODEX_HOME=/auth/codex`). The login is a one-off interactive
`docker compose run`. The long-running services keep `tty: false` and
`stdin_open: false`.

```bash
docker compose build
# Claude: prints a URL; open it, approve, paste the code back.
docker compose run --rm --entrypoint claude claude auth login --claudeai
# Codex: device-code flow, works without a browser in the container.
docker compose run --rm --entrypoint codex codex login --device-auth
```

Then read the limits and account fingerprints:

```bash
docker compose run --rm claude --probe
docker compose run --rm codex --probe
```

Both readings must show `"confidence": "verified"` once the fingerprints are
configured. On the first run they show `account mismatch` together with
`observed_account_fingerprint`. Copy each value into
`reviewers.<provider>.account_fingerprint`. A fingerprint is a hash of the
provider account id, not a secret.

Never mount a host home directory, SSH directory, credential vault, or Docker
socket. Do not copy host credentials into the volumes: both providers rotate
refresh tokens, so two copies of one login invalidate each other.

Disable additional paid usage, credits, and API fallback in each provider account
before deployment. Removing an API key from the container is useful isolation, but
it does not prove that account-level extra usage is disabled. The Compose file
forwards no API-key environment variables, and the worker passes only `HOME`,
`PATH`, `TZ`, the auth directory and `DISABLE_AUTOUPDATER` to the CLIs.

When rotating a token, stop the affected worker, refresh only its authentication
volume, run `doctor`, and restart the worker. Do not copy one provider's
authentication files into the other worker.

## Configuration and startup

Copy `config/example.toml` to `config/reviewer.toml`, replace every preflight placeholder, and keep the selected models explicit. Validate the deployment before starting a session:

```bash
docker compose config --quiet
docker compose up -d claude codex engine
docker compose exec -T engine peer-reviewer doctor --config /config/reviewer.toml --sessions /sessions
```

`doctor` sends a `limits` job to each running worker. It reports a CLI version
that differs from `cli_version`, an unconfigured or different account, and any
unverified limit reading.

Start locally with:

```bash
PEER_REVIEWER_CONFIG=config/reviewer.toml \
  peer-reviewer start input/document.md --session sessions/s-001
docker compose up -d
```

Compose also mounts `${PEER_REVIEWER_INPUT:-./input}` read-only at `/input`. To run
the complete CLI flow inside the engine container, place the source in that host
directory and use:

```bash
mkdir -p input sessions
cp document.md input/document.md
docker compose up -d
docker compose exec -T engine peer-reviewer start /input/document.md --session /sessions/s-001
```

The same Compose project runs on a VPS. Set `PEER_REVIEWER_SESSIONS` and `PEER_REVIEWER_CONFIG` to absolute host paths when the defaults are unsuitable. The engine is the only service that mounts the session and control directories.

Only one active session is permitted per sessions root. `start` rejects an existing session directory and a second active session. The input must be a regular UTF-8 file within the configured byte limit; it need not be a Git repository.

## Observation and control

The host can read these files without entering a container:

```bash
cat sessions/s-001/runtime/status.md
cat sessions/s-001/rounds/0001/round.md
cat sessions/s-001/report.md
```

Equivalent CLI commands are:

```bash
peer-reviewer status --session sessions/s-001
peer-reviewer status --session sessions/s-001 --watch
peer-reviewer stop --session sessions/s-001
peer-reviewer resume --session sessions/s-001
peer-reviewer report --session sessions/s-001
peer-reviewer accept --session sessions/s-001
```

A stop response distinguishes a persisted request from an applied stop. A stale heartbeat warns that the engine may not be running; it never claims that the session stopped. Closing `status --watch`, its stdout, an SSH connection, or a terminal does not stop the engine.

After four transient failures the session enters `ERROR` with
`attempts_exhausted`. `resume` records an explicit human retry grant and starts a
new, uniquely identified attempt group. The active-session marker remains in place
until the session reaches a final outcome or a non-recoverable error.

`PAUSED_LIMIT_LOW` includes the known reset and next check. `PAUSED_LIMIT_UNKNOWN` says that the reset is unknown and retries only metadata reads with bounded backoff. It performs no inference while telemetry is unknown. A stop command remains available during either pause.

## Restart and recovery

Restart with `docker compose restart`. The engine replays committed round directories and ignores a stale replaceable `state.json`. If a round was renamed into place before a crash, recovery creates or reuses its checkpoint and does not submit that round again. An active worker job is cancelled and acknowledged before another attempt is allowed.

Back up the entire session directory, including `session.json`, `source.txt`, `rounds`, `attempts`, and `runtime`. Copying only `report.md` loses the replay and control history. Restore the directory at the same logical sessions root and run `resume`; a terminal session only rebuilds views and the report.

`state.json` and event JSON files are authoritative. Markdown status and event files are deterministic views and may be rebuilt. Each completed round is append-only and has checksums. Do not edit the source, round JSON, or checksums after session creation.

## Acceptance limits

The offline suite covers deterministic debates, crash boundaries, pauses, command idempotence, reports, and the rendered Compose isolation model. It does not establish real subscription behavior. Full acceptance requires both live OAuth CLIs, verified account settings, pinned versions/models, successful fresh limit readings, and inspection of an actual argument plus a later withdrawal.

The one-hour product criterion describes the normal user scenario. It is not a time guarantee when either subscription is paused or its limit telemetry is unavailable. A clock-controlled reset test does not replace the separate long-running real-reset trial.

## Acceptance results

On 2026-09-20 the offline acceptance passed: 149 tests, compose config, and the
package build.

On 2026-09-23:

- full offline suite: 193 passed, 2 live tests skipped;
- worker images build with the pinned CLIs and run under the hardened Compose
  settings (read-only root, `cap_drop: ALL`, tmpfs `/tmp`);
- host-side live limit reads: Claude `/usage` and Codex App Server both verified;
- live end-to-end session on the host with the real engine, workers and CLIs
  (Claude `claude-sonnet-5`, Codex `gpt-6-astra`, `max_rounds = 3`): `doctor` OK,
  3 rounds and 6 turns, each accepted on its first attempt, in 440 s. Outcome:
  `NO_CONSENSUS`, with one agreed issue, one duplicate link, and one reasoned
  dispute. Reviewer A changed its fix in response to B's counter-argument;
- the live runs exposed three defects that fixtures could not catch, all fixed:
  `--ask-for-approval` placed after `codex exec`, a turn schema rejected by both
  providers' structured-output validators, and a prompt that did not state the
  turn contract;
- container-side reads return `authentication` until the OAuth login above is done.
