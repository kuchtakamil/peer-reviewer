from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


class DebateError(ValueError):
    pass


def version_id(issue_id: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"issue_id": issue_id, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def initial_state(session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "round_no": 0,
        "issues": {},
        "versions": {},
        "arguments": {},
        "positions": {"A": {}, "B": {}},
        "open_proposals": {},
        "closed_versions": {},
        "resolved": {},
        "last_turns_complete": False,
        "consensus": False,
    }


def _ordered_turns(turns: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_reviewer = {turn.get("reviewer"): turn for turn in turns}
    if set(by_reviewer) != {"A", "B"} or len(turns) != 2:
        raise DebateError("A round requires exactly one turn from A and B")
    return {reviewer: by_reviewer[reviewer] for reviewer in ("A", "B")}


def _validate_turn_headers(
    previous: dict[str, Any], ordered: dict[str, dict[str, Any]], round_no: int
) -> None:
    hashes = set()
    for reviewer, turn in ordered.items():
        if turn.get("session_id") != previous["session_id"]:
            raise DebateError("Turn belongs to another session")
        if turn.get("round_no") != round_no:
            raise DebateError("Turn has an unexpected round number")
        if turn.get("review_complete") is not True:
            raise DebateError("Turn is not complete")
        hashes.add(turn.get("input_state_hash"))
        if turn.get("reviewer") != reviewer:
            raise DebateError("Reviewer identity mismatch")
    if len(hashes) != 1:
        raise DebateError("Reviewers did not receive the same input state")


def _candidate_versions(state: dict[str, Any]) -> dict[str, str]:
    candidates: dict[str, str] = {}
    closed = state["closed_versions"]
    for issue_id in state["issues"]:
        a = state["positions"]["A"].get(issue_id)
        b = state["positions"]["B"].get(issue_id)
        if not a or not b:
            continue
        if a["action"] == "oppose" or b["action"] == "oppose":
            continue
        if a["version_ref"] != b["version_ref"]:
            continue
        chosen = a["version_ref"]
        if chosen not in state["versions"]:
            raise DebateError("Position references an unknown version")
        active = {
            ref
            for ref, version in state["versions"].items()
            if version["issue_id"] == issue_id and ref not in closed
        }
        closes = set(state["versions"][chosen]["payload"]["closes"])
        if active - {chosen} <= closes:
            candidates[issue_id] = chosen
    return candidates


def _resolve_candidates(state: dict[str, Any], candidates: dict[str, str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    visiting: set[str] = set()

    def resolve(issue_id: str) -> bool:
        if issue_id in resolved:
            return True
        if issue_id not in candidates:
            return False
        if issue_id in visiting:
            raise DebateError("duplicate cycle detected")
        visiting.add(issue_id)
        version = candidates[issue_id]
        payload = state["versions"][version]["payload"]
        if payload["resolution"] == "duplicate":
            target = payload.get("duplicate_of")
            if target == issue_id or not isinstance(target, str) or not resolve(target):
                visiting.remove(issue_id)
                return False
        resolved[issue_id] = version
        visiting.remove(issue_id)
        return True

    for issue_id in sorted(candidates):
        resolve(issue_id)
    return resolved


def reduce_round(
    previous: dict[str, Any], turns: tuple[dict[str, Any], dict[str, Any]]
) -> dict[str, Any]:
    state = copy.deepcopy(previous)
    for key, default in (
        ("issues", {}),
        ("versions", {}),
        ("arguments", {}),
        ("positions", {"A": {}, "B": {}}),
        ("open_proposals", {}),
        ("closed_versions", {}),
        ("resolved", {}),
    ):
        state.setdefault(key, copy.deepcopy(default))
    state["positions"].setdefault("A", {})
    state["positions"].setdefault("B", {})

    round_no = int(previous.get("round_no", 0)) + 1
    ordered = _ordered_turns(turns)
    _validate_turn_headers(previous, ordered, round_no)
    prior_issue_ids = set(state["issues"])
    prior_version_ids = set(state["versions"])

    current_issue_ids: set[str] = set()
    for reviewer, turn in ordered.items():
        own_new = {item["id"] for item in turn.get("new_issues", [])}
        if current_issue_ids & own_new or prior_issue_ids & own_new:
            raise DebateError("Duplicate issue id")
        current_issue_ids.update(own_new)
        actual_positions = [item["issue_id"] for item in turn.get("positions", [])]
        required = prior_issue_ids | own_new
        if len(actual_positions) != len(set(actual_positions)) or set(actual_positions) != required:
            raise DebateError(f"{reviewer} positions do not cover every required issue")

    for reviewer, turn in ordered.items():
        for item in sorted(turn.get("new_issues", []), key=lambda value: value["id"]):
            if item.get("author") != reviewer or not item["id"].startswith(f"{reviewer}-"):
                raise DebateError("Issue author does not match reviewer")
            state["issues"][item["id"]] = copy.deepcopy(item)

    local_versions: dict[str, dict[str, str]] = {"A": {}, "B": {}}
    for reviewer, turn in ordered.items():
        seen_local: set[str] = set()
        for item in sorted(turn.get("proposals", []), key=lambda value: value["local_ref"]):
            local_ref = item["local_ref"]
            issue_id = item["issue_id"]
            if local_ref in seen_local:
                raise DebateError("Duplicate proposal local_ref")
            seen_local.add(local_ref)
            if issue_id not in state["issues"]:
                raise DebateError("Proposal references unknown issue")
            closes = item["payload"]["closes"]
            if any(ref not in prior_version_ids for ref in closes):
                raise DebateError("Proposal closes a version not visible on round input")
            if any(state["versions"][ref]["issue_id"] != issue_id for ref in closes):
                raise DebateError("Proposal closes a version from another issue")
            ref = version_id(issue_id, item["payload"])
            existing = state["versions"].get(ref)
            record = {
                "issue_id": issue_id,
                "payload": copy.deepcopy(item["payload"]),
                "proposed_by": reviewer,
                "round_no": round_no,
                "local_ref": local_ref,
            }
            if existing is not None and (
                existing["issue_id"] != issue_id or existing["payload"] != item["payload"]
            ):
                raise DebateError("Version hash collision")
            state["versions"].setdefault(ref, record)
            local_versions[reviewer][local_ref] = ref

    current_positions: dict[str, dict[str, dict[str, Any]]] = {"A": {}, "B": {}}
    for reviewer, turn in ordered.items():
        for item in sorted(turn.get("positions", []), key=lambda value: value["issue_id"]):
            normalized = copy.deepcopy(item)
            if normalized["action"] == "propose":
                try:
                    normalized["version_ref"] = local_versions[reviewer][normalized["version_ref"]]
                except KeyError as exc:
                    raise DebateError("propose position references unknown local proposal") from exc
            elif normalized["version_ref"] not in state["versions"]:
                raise DebateError("Position references unknown version")
            version = state["versions"][normalized["version_ref"]]
            if version["issue_id"] != normalized["issue_id"]:
                raise DebateError("Position version belongs to another issue")
            argument_id = normalized["argument_id"]
            if argument_id in state["arguments"]:
                raise DebateError("Duplicate argument id")
            state["arguments"][argument_id] = {
                "author": reviewer,
                "round_no": round_no,
                "issue_id": normalized["issue_id"],
                "reason": normalized["reason"],
                "responds_to": copy.deepcopy(normalized["responds_to"]),
                "evidence": copy.deepcopy(normalized["evidence"]),
            }
            current_positions[reviewer][normalized["issue_id"]] = normalized

    state["positions"] = current_positions
    candidates = _candidate_versions(state)
    resolved = _resolve_candidates(state, candidates)
    state["resolved"] = resolved
    for issue_id, chosen in resolved.items():
        for closed_ref in state["versions"][chosen]["payload"]["closes"]:
            state["closed_versions"][closed_ref] = chosen

    open_proposals: dict[str, list[str]] = {}
    for issue_id in sorted(state["issues"]):
        if issue_id in resolved:
            continue
        refs = sorted(
            ref
            for ref, item in state["versions"].items()
            if item["issue_id"] == issue_id and ref not in state["closed_versions"]
        )
        if refs:
            open_proposals[issue_id] = refs
    state["open_proposals"] = open_proposals
    state["round_no"] = round_no
    state["last_turns_complete"] = True
    state["consensus"] = bool(
        round_no >= 2
        and len(resolved) == len(state["issues"])
        and not open_proposals
    )
    return state


def has_consensus(state: dict[str, Any]) -> bool:
    return bool(state.get("consensus"))
