from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from peer_reviewer.debate import DebateError, has_consensus, reduce_round
from peer_reviewer.limits import decide_round_start
from peer_reviewer.store import IntegrityError, SessionStore, StoreError, content_hash


class EngineError(RuntimeError):
    pass


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value).astimezone(
        timezone.utc
    )


def _material(state: dict[str, Any]) -> dict[str, Any]:
    positions: dict[str, dict[str, Any]] = {}
    for reviewer in ("A", "B"):
        positions[reviewer] = {
            issue_id: {
                key: value
                for key, value in position.items()
                if key not in {"argument_id", "round_no", "created_at", "updated_at"}
            }
            for issue_id, position in sorted(state.get("positions", {}).get(reviewer, {}).items())
        }
    versions = {
        reference: {
            "issue_id": value["issue_id"],
            "payload": value["payload"],
        }
        for reference, value in sorted(state.get("versions", {}).items())
    }
    return {
        "versions": versions,
        "positions": positions,
        "open_proposals": state.get("open_proposals", {}),
    }


def _issue_counts(state: dict[str, Any]) -> dict[str, int]:
    counts = {"agreed": 0, "rejected": 0, "disputed": 0}
    resolved = state.get("resolved", {})
    versions = state.get("versions", {})
    for issue_id in state.get("issues", {}):
        reference = resolved.get(issue_id)
        version = versions.get(reference, {}) if reference else {}
        resolution = version.get("payload", {}).get("resolution")
        if resolution == "rejected":
            counts["rejected"] += 1
        elif resolution in {"accepted", "duplicate"}:
            counts["agreed"] += 1
        else:
            counts["disputed"] += 1
    return counts


class Engine:
    TRANSIENT_ERRORS = {
        "timeout",
        "deadline",
        "cancelled",
        "process_exit",
        "interrupted",
        "worker_error",
        "transport",
    }

    def __init__(self, store: SessionStore, workers: Any, control: Any, clock: Any) -> None:
        self.store = store
        self.workers = workers
        self.control = control
        self.clock = clock
        self.recovered = self.store.recover()
        self.metadata = self.recovered["session"]
        self.config = self.metadata.get("config", {})
        self.source = (self.store.root / "source.txt").read_text(encoding="utf-8")
        self._needs_checkpoint = False
        self.state = self._load_runtime()

    def _session_config(self) -> dict[str, Any]:
        value = self.config.get("session", self.config)
        return value if isinstance(value, dict) else {}

    def _limit_config(self) -> dict[str, Any]:
        value = self.config.get("limits", {})
        return value if isinstance(value, dict) else {}

    def _load_runtime(self) -> dict[str, Any]:
        path = self.store.root / "runtime" / "state.json"
        stored: dict[str, Any] = {}
        if path.exists():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    stored = value
            except (OSError, UnicodeError, json.JSONDecodeError):
                stored = {}
        state = {
            "phase": "READY",
            "outcome": None,
            "last_completed_round": self.recovered["last_completed_round"],
            "debate_state": self.recovered["state"],
            "attempts": {},
            "attempt_grants": {},
            "active": None,
            "checkpoint": None,
            "retry_at": None,
            "error_code": None,
            "updated_at": _iso(self.clock.now()),
        }
        for key in (
            "phase",
            "outcome",
            "attempts",
            "active",
            "checkpoint",
            "retry_at",
            "error_code",
            "no_progress",
            "pause",
            "attempt_grants",
            "last_event_id",
        ):
            if key in stored:
                state[key] = stored[key]
        state["last_completed_round"] = self.recovered["last_completed_round"]
        state["debate_state"] = self.recovered["state"]
        if self.recovered["last_completed_round"] > int(
            stored.get("last_completed_round", 0)
        ) or (self.recovered["last_completed_round"] > 0 and "phase" not in stored):
            self._needs_checkpoint = True
            state["phase"] = "READY"
            state["active"] = None
            state["checkpoint"] = None
        return state

    def _save(self) -> dict[str, Any]:
        self.state["updated_at"] = _iso(self.clock.now())
        self.store.write_status(self.state)
        return copy.deepcopy(self.state)

    def _event(self, event_id: str, kind: str, **values: Any) -> None:
        event = {
            "event_id": event_id,
            "session_id": self.metadata["session_id"],
            "kind": kind,
            "created_at": _iso(self.clock.now()),
            **values,
        }
        path = self.store.root / "runtime" / "events" / f"{event_id}.json"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if {key: value for key, value in existing.items() if key != "created_at"} != {
                key: value for key, value in event.items() if key != "created_at"
            }:
                self.store.write_event(event)
        else:
            self.store.write_event(event)
        self.state["last_event_id"] = event_id

    def _terminal_error(self, code: str) -> dict[str, Any]:
        self.state.update(phase="ERROR", outcome="ERROR", error_code=code, active=None)
        active_round = self.recovered["last_completed_round"] + 1
        attempt = int(self.state.get("attempts", {}).get(str(active_round), 0))
        self._event(
            f"error-r{active_round:04d}-a{attempt}-{code}",
            "error",
            round_no=active_round,
            error_code=code,
            message=f"Session stopped with error {code}.",
        )
        return self._save()

    def _create_checkpoint(self) -> dict[str, Any]:
        round_no = self.recovered["last_completed_round"]
        seconds = int(self._session_config().get("veto_seconds", 60))
        event_id = f"checkpoint-{round_no:04d}"
        command = f"peer-reviewer stop --session {self.store.root}"
        event_path = self.store.root / "runtime" / "events" / f"{event_id}.json"
        if event_path.exists():
            try:
                existing = json.loads(event_path.read_text(encoding="utf-8"))
                if existing.get("kind") != "checkpoint" or existing.get("round_no") != round_no:
                    raise ValueError("checkpoint identity mismatch")
                deadline = _time(existing["deadline"])
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise IntegrityError("invalid durable checkpoint event") from exc
        else:
            deadline = self.clock.now() + timedelta(seconds=seconds)
            self._event(
                event_id,
                "checkpoint",
                round_no=round_no,
                deadline=_iso(deadline),
                message=(
                    f"Session {self.metadata['session_id']} committed round {round_no}; "
                    f"veto by {_iso(deadline)} with: {command}"
                ),
                veto_command=command,
                counts=_issue_counts(self.recovered["state"]),
            )
        self.state.update(
            phase="CHECKPOINT",
            checkpoint={"event_id": event_id, "round_no": round_no, "deadline": _iso(deadline)},
            active=None,
            last_completed_round=round_no,
            debate_state=self.recovered["state"],
        )
        self._needs_checkpoint = False
        return self._save()

    def _commands(self) -> list[dict[str, Any]]:
        commands = self.control.poll()
        return commands if isinstance(commands, list) else []

    def _stop(self, command: dict[str, Any] | None = None) -> dict[str, Any]:
        active = self.state.get("active")
        if isinstance(active, dict) and active.get("job_id"):
            try:
                self.workers.cancel(active["job_id"])
            except Exception:
                return self._terminal_error("cancel_unconfirmed")
        self.state.update(phase="STOPPED", outcome="STOPPED", active=None)
        command_id = None if command is None else command.get("command_id")
        suffix = content_hash(str(command_id).encode()).removeprefix("sha256:")[:12]
        self._event(
            f"stopped-{self.state['last_completed_round']:04d}-{suffix}",
            "stopped",
            round_no=self.state["last_completed_round"],
            command_id=command_id,
        )
        return self._save()

    def _apply_commands(self) -> dict[str, Any] | None:
        for command in self._commands():
            if command.get("kind") == "stop":
                result = self._stop(command)
                if hasattr(self.control, "acknowledge"):
                    self.control.acknowledge(command, result["phase"])
                return result
            if (
                command.get("kind") == "resume"
                and self.state.get("phase") == "ERROR"
                and self.state.get("error_code") == "attempts_exhausted"
            ):
                round_key = str(self.recovered["last_completed_round"] + 1)
                self.state["attempts"][round_key] = 0
                self.state["attempt_grants"][round_key] = int(
                    self.state["attempt_grants"].get(round_key, 0)
                ) + 1
                self.state.update(phase="READY", outcome=None, error_code=None, retry_at=None)
                self._event(
                    f"retry-grant-{round_key}-{command.get('command_id', 'manual')}",
                    "retry_grant",
                    round_no=int(round_key),
                    command_id=command.get("command_id"),
                )
                result = self._save()
                if hasattr(self.control, "acknowledge"):
                    self.control.acknowledge(command, "RESUMED")
                return result
        return None

    def _gate(self) -> dict[str, Any]:
        try:
            samples = self.workers.limits()
        except Exception:
            samples = {}
        expected = self.config.get("reviewers", {})
        identities = {
            "A": ("claude", expected.get("claude", {})),
            "B": ("codex", expected.get("codex", {})),
        }
        for reviewer, (provider, configured) in identities.items():
            sample = samples.get(reviewer)
            if not isinstance(sample, dict):
                continue
            if configured and (
                sample.get("provider") != provider
                or sample.get("account_fingerprint") != configured.get("account_fingerprint")
                or sample.get("model_bucket") != configured.get("model_bucket")
            ):
                sample = copy.deepcopy(sample)
                sample["confidence"] = "unknown"
                sample["five_hour"] = None
                sample["other_blockers"] = ["limit telemetry identity mismatch"]
                samples[reviewer] = sample
        return decide_round_start(
            samples,
            self.clock.now(),
            max_age_seconds=int(self._limit_config().get("max_age_seconds", 60)),
            minimum_remaining_percent=float(
                self._limit_config().get("minimum_remaining_percent", 20.0)
            ),
        )

    def _pause(self, decision: dict[str, Any]) -> dict[str, Any]:
        phase = decision["reason"]
        next_check = decision.get("next_check_at")
        self.state.update(
            phase=phase,
            active=None,
            retry_at=None,
            pause={
                "reason": decision["reason"],
                "details": decision.get("details", {}),
                "wake_at": _iso(decision["wake_at"]) if decision.get("wake_at") else None,
                "next_check_at": _iso(next_check) if next_check else None,
            },
        )
        sequence = len(list((self.store.root / "runtime" / "events").glob("pause-*.json"))) + 1
        self._event(
            f"pause-{sequence:06d}",
            "pause",
            round_no=self.recovered["last_completed_round"] + 1,
            **self.state["pause"],
        )
        return self._save()

    def _job(self, reviewer: str, round_no: int, attempt_no: int) -> dict[str, Any]:
        round_key = str(round_no)
        grant_no = int(self.state.get("attempt_grants", {}).get(round_key, 0))
        attempt_id = f"r{round_no}-g{grant_no}-a{attempt_no}"
        state = copy.deepcopy(self.recovered["state"])
        state_hash = content_hash(state)
        job_id = f"{self.metadata['session_id']}-r{round_no}-{attempt_id}-{reviewer}"
        expected = {
            "session_id": self.metadata["session_id"],
            "round_no": round_no,
            "reviewer": reviewer,
            "input_state_hash": state_hash,
        }
        provider = "claude" if reviewer == "A" else "codex"
        process = copy.deepcopy(self.config.get("process", {}))
        timeout_seconds = int(process.get("timeout_seconds", 15 * 60))
        return {
            "job_id": job_id,
            "session_id": self.metadata["session_id"],
            "round_no": round_no,
            "attempt_id": attempt_id,
            "reviewer": reviewer,
            "kind": "review",
            "input_state_hash": state_hash,
            "payload": {
                "source": self.source,
                "state": state,
                "criteria": self._session_config().get("criteria", []),
                "reviewer": reviewer,
                "expected": expected,
                "model": self.config.get("reviewers", {}).get(provider, {}).get("model"),
                "effort": self.config.get("reviewers", {}).get(provider, {}).get("effort"),
                "process": process,
            },
            "deadline": _iso(self.clock.now() + timedelta(seconds=timeout_seconds)),
        }

    def _start_attempt(self) -> dict[str, Any]:
        round_no = self.recovered["last_completed_round"] + 1
        key = str(round_no)
        attempt_no = int(self.state["attempts"].get(key, 0)) + 1
        if attempt_no > 4:
            return self._terminal_error("attempts_exhausted")
        self.state["attempts"][key] = attempt_no
        job = self._job("A", round_no, attempt_no)
        self.state.update(
            phase="REVIEWING_A",
            active={
                "job_id": job["job_id"],
                "round_no": round_no,
                "attempt_no": attempt_no,
                "attempt_id": job["attempt_id"],
                "input_state_hash": job["input_state_hash"],
                "deadline": job["deadline"],
                "turns": {},
            },
        )
        self._save()
        try:
            self.workers.submit(job)
        except Exception:
            return self._failure("transport")
        return copy.deepcopy(self.state)

    def _failure(self, code: str) -> dict[str, Any]:
        active = self.state["active"]
        attempt_no = int(active["attempt_no"])
        if code == "rate_limit":
            return self._pause({
                "reason": "PAUSED_LIMIT_UNKNOWN",
                "details": {"worker": "provider rate limit"},
                "wake_at": None,
                "next_check_at": self.clock.now() + timedelta(seconds=60),
            })
        if code not in self.TRANSIENT_ERRORS:
            return self._terminal_error(code)
        if attempt_no >= 4:
            return self._terminal_error("attempts_exhausted")
        delay = (10, 30, 90)[attempt_no - 1]
        self.state.update(
            phase="RETRY_WAIT",
            retry_at=_iso(self.clock.now() + timedelta(seconds=delay)),
            error_code=code,
            active=None,
        )
        return self._save()

    def _valid_response(self, response: dict[str, Any], active: dict[str, Any], reviewer: str) -> bool:
        return all(
            response.get(key) == value
            for key, value in {
                "job_id": active["job_id"],
                "session_id": self.metadata["session_id"],
                "round_no": active["round_no"],
                "attempt_id": active["attempt_id"],
                "reviewer": reviewer,
                "kind": "review",
                "input_state_hash": active["input_state_hash"],
            }.items()
        )

    def _poll_a(self) -> dict[str, Any]:
        active = self.state["active"]
        try:
            response = self.workers.poll(active["job_id"])
        except Exception:
            return self._failure("transport")
        if response is None:
            if self.clock.now() >= _time(active["deadline"]):
                try:
                    self.workers.cancel(active["job_id"])
                except Exception:
                    return self._terminal_error("cancel_unconfirmed")
                return self._failure("timeout")
            return copy.deepcopy(self.state)
        if not self._valid_response(response, active, "A"):
            return self._failure("stale_response")
        if not response.get("ok"):
            return self._failure(str(response.get("error_code") or "worker_error"))
        active["turns"]["A"] = response["result"]
        decision = self._gate()
        if not decision["allow"]:
            return self._pause(decision)
        job = self._job("B", active["round_no"], active["attempt_no"])
        active["job_id"] = job["job_id"]
        active["deadline"] = job["deadline"]
        self.state["phase"] = "REVIEWING_B"
        self._save()
        try:
            self.workers.submit(job)
        except Exception:
            return self._failure("transport")
        return copy.deepcopy(self.state)

    def _poll_b(self) -> dict[str, Any]:
        active = self.state["active"]
        try:
            response = self.workers.poll(active["job_id"])
        except Exception:
            return self._failure("transport")
        if response is None:
            if self.clock.now() >= _time(active["deadline"]):
                try:
                    self.workers.cancel(active["job_id"])
                except Exception:
                    return self._terminal_error("cancel_unconfirmed")
                return self._failure("timeout")
            return copy.deepcopy(self.state)
        if not self._valid_response(response, active, "B"):
            return self._failure("stale_response")
        if not response.get("ok"):
            return self._failure(str(response.get("error_code") or "worker_error"))
        active["turns"]["B"] = response["result"]
        previous = self.recovered["state"]
        try:
            result = reduce_round(previous, (active["turns"]["A"], active["turns"]["B"]))
        except (DebateError, KeyError, TypeError, ValueError) as exc:
            return self._terminal_error("integrity")
        bundle = {
            "round_no": active["round_no"],
            "attempt_id": active["attempt_id"],
            "source_hash": self.metadata["source_hash"],
            "input_state_hash": active["input_state_hash"],
            "turns": [active["turns"]["A"], active["turns"]["B"]],
            "state_result": result,
        }
        try:
            self.store.commit_round(bundle)
            self.recovered = self.store.recover()
        except StoreError:
            return self._terminal_error("storage")
        return self._create_checkpoint()

    def _finish_checkpoint(self) -> dict[str, Any]:
        if has_consensus(self.recovered["state"]):
            self.state.update(phase="CONSENSUS", outcome="CONSENSUS", checkpoint=None)
            return self._save()
        max_rounds = int(self._session_config().get("max_rounds", 5))
        if self.recovered["last_completed_round"] >= max_rounds:
            self.state.update(phase="NO_CONSENSUS", outcome="NO_CONSENSUS", checkpoint=None)
            return self._save()
        rounds = self.recovered["rounds"]
        if len(rounds) >= 2 and _material(rounds[-1]["state_result"]) == _material(
            rounds[-2]["state_result"]
        ):
            self.state.update(phase="NO_PROGRESS", no_progress=True, checkpoint=None)
            return self._save()
        self.state.update(phase="READY", checkpoint=None, error_code=None)
        return self._save()

    def tick(self) -> dict[str, Any]:
        command_result = self._apply_commands()
        if command_result is not None:
            return command_result
        if self._needs_checkpoint:
            return self._create_checkpoint()
        phase = self.state["phase"]
        if phase in {"CONSENSUS", "NO_CONSENSUS", "STOPPED", "ERROR"}:
            return copy.deepcopy(self.state)
        if self.recovered["last_completed_round"] and (
            not self.state.get("checkpoint")
            and phase not in {"READY", "NO_PROGRESS"}
            and self.state["last_completed_round"] < self.recovered["last_completed_round"]
        ):
            return self._create_checkpoint()
        if phase == "CHECKPOINT":
            if self.clock.now() < _time(self.state["checkpoint"]["deadline"]):
                return copy.deepcopy(self.state)
            return self._finish_checkpoint()
        if phase == "NO_PROGRESS":
            self.state["phase"] = "READY"
            return self._save()
        if phase == "REVIEWING_A":
            return self._poll_a()
        if phase == "REVIEWING_B":
            return self._poll_b()
        if phase == "RETRY_WAIT":
            if self.clock.now() < _time(self.state["retry_at"]):
                return copy.deepcopy(self.state)
            self.state.update(phase="READY", retry_at=None)
        if phase in {"PAUSED_LIMIT_LOW", "PAUSED_LIMIT_UNKNOWN"}:
            next_check = self.state.get("pause", {}).get("next_check_at")
            if next_check and self.clock.now() < _time(next_check):
                return copy.deepcopy(self.state)
            self.state["phase"] = "READY"
        decision = self._gate()
        if not decision["allow"]:
            return self._pause(decision)
        return self._start_attempt()

    def resume(self) -> dict[str, Any]:
        self.recovered = self.store.recover()
        if self._needs_checkpoint or self.recovered["last_completed_round"] > int(
            self.state.get("last_completed_round", 0)
        ):
            self.state.update(
                last_completed_round=self.recovered["last_completed_round"],
                debate_state=self.recovered["state"],
                active=None,
                checkpoint=None,
            )
            return self._create_checkpoint()
        active = self.state.get("active")
        if isinstance(active, dict) and active.get("job_id"):
            try:
                self.workers.cancel(active["job_id"])
            except Exception:
                return self._terminal_error("cancel_unconfirmed")
            attempt_no = int(active["attempt_no"])
            if attempt_no >= 4:
                return self._terminal_error("attempts_exhausted")
            else:
                delay = (10, 30, 90)[attempt_no - 1]
                self.state.update(
                    phase="RETRY_WAIT",
                    retry_at=_iso(self.clock.now() + timedelta(seconds=delay)),
                    error_code="interrupted",
                    active=None,
                )
            return self._save()
        if self.state["phase"] == "CHECKPOINT":
            return copy.deepcopy(self.state)
        return self.tick()
