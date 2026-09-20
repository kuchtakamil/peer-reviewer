# Feasibility of subscription limit preflight

Status as of 2026-09-20: **NO-GO for production use**.

The live matrix required by Task 0 has not been run in the target Linux containers. Claude Code is unavailable in the current environment, so its capability probe is an explicit conservative stub. It reports unknown limit data and blocks a review round. It does not infer remaining quota, issue a prompt, inspect OAuth metadata, or claim that status-line telemetry works in print mode.

The following checks remain unverified: subscription authentication and refresh after restart, preflight before the first turn, preflight after a reset, use from another device, expired authentication, rate-limit responses, and headless operation for both provider accounts. No CLI, image, account type, or model version is pinned here because none has completed that matrix.

`scripts/probe_capabilities.py` emits the public capability contract. A concrete live run can be supplied to the opt-in integration test through `PEER_REVIEWER_CAPABILITY_RESULT`. Missing live output yields `SKIP`, never `PASS`.

Core development may continue with fixtures. A review round must remain blocked whenever either provider has missing, stale, or unverified five-hour telemetry.
