# Peer Reviewer — operations

## Deployment status

The offline implementation is usable with fixture workers. Production remains **NO-GO** until the live capability matrix in `docs/feasibility.md` confirms a fresh, non-generative five-hour limit reading for both subscriptions. In particular, the current Claude limit reader is a conservative stub and always yields `unknown`.

Pin both CLI versions and explicit model names in the configuration after that preflight. The example values in `config/example.toml` are deliberately invalid deployment placeholders.

## OAuth and billing controls

Provision each CLI's OAuth login in its own named authentication volume. The Claude worker receives only `claude-auth`; the Codex worker receives only `codex-auth`. Do the interactive login as a separate provisioning action, then return both services to `tty: false` and `stdin_open: false`. Never mount a host home directory, SSH directory, credential vault, or Docker socket.

Disable additional paid usage, credits, and API fallback in each provider account before deployment. Removing an API key from the container is useful isolation, but it does not prove that account-level extra usage is disabled. Verify the account fingerprint and subscription bucket reported by the limit probe. The Compose file forwards no API-key environment variables.

When rotating a token, stop the affected worker, refresh only its authentication volume, run `doctor`, and restart the worker. Do not copy one provider's authentication files into the other worker.

## Configuration and startup

Copy `config/example.toml` to `config/reviewer.toml`, replace every preflight placeholder, and keep the selected models explicit. Validate the deployment before starting a session:

```bash
peer-reviewer doctor --config config/reviewer.toml --sessions sessions
docker compose config --quiet
```

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

## Offline acceptance result

On 2026-09-20 the offline acceptance completed with these results:

- unit and integration: 142 passed, 1 live test deselected;
- Docker-marked E2E fixtures: 7 passed, 1 live test deselected;
- full offline suite: 149 passed, 2 live tests deselected;
- `docker compose config --quiet`: passed;
- source distribution and wheel build: passed.

This is an offline GO for the deterministic engine and fixture workers. Production
remains **NO-GO** because the real Claude limit read, OAuth/account controls,
provider CLI versions, container images, and subscription model names have not
passed the live capability matrix.
