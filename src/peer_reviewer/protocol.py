from __future__ import annotations

import json
import re
from importlib.resources import files
from typing import Any

import jsonschema


class ProtocolError(ValueError):
    pass


HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ISSUE_RE = re.compile(r"^([AB])-(\d+)$")
ARGUMENT_RE = re.compile(r"^([AB])-r(\d+)-(\d+)$")


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"Powtórzony klucz: {key}")
        result[key] = value
    return result


def _schema() -> dict[str, Any]:
    path = files("peer_reviewer.schemas").joinpath("turn.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_anchor(anchor: dict[str, Any], source: str, label: str) -> None:
    lines = source.splitlines()
    start = anchor["line_start"]
    end = anchor["line_end"]
    if start < 1 or end < start or end > len(lines):
        raise ProtocolError(f"{label} line range is outside source")
    actual = "\n".join(lines[start - 1 : end])
    if anchor["quote"] != actual:
        raise ProtocolError(f"{label} quote does not match immutable source")


def _unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ProtocolError(f"Duplicate {label}")


def parse_turn(
    raw: bytes,
    expected: dict[str, Any],
    state: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    try:
        turn = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except ProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"Invalid JSON: {exc}") from exc

    try:
        jsonschema.validate(turn, _schema())
    except jsonschema.ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path) or "<root>"
        raise ProtocolError(f"schema validation failed at {location}: {exc.message}") from exc

    for field in ("session_id", "round_no", "reviewer", "input_state_hash"):
        if field in expected and turn[field] != expected[field]:
            raise ProtocolError(f"Unexpected {field}")

    reviewer = turn["reviewer"]
    round_no = turn["round_no"]
    issues = state.get("issues", {})
    versions = state.get("versions", {})
    arguments = state.get("arguments", {})
    if not all(isinstance(value, dict) for value in (issues, versions, arguments)):
        raise ProtocolError("Invalid input state maps")

    issue_ids = [issue["id"] for issue in turn["new_issues"]]
    local_refs = [proposal["local_ref"] for proposal in turn["proposals"]]
    argument_ids = [position["argument_id"] for position in turn["positions"]]
    _unique(issue_ids, "new issue id")
    _unique(local_refs, "proposal local_ref")
    _unique(argument_ids, "argument_id")
    if set(issue_ids) & set(issues):
        raise ProtocolError("new_issues contains an existing id")
    if set(argument_ids) & set(arguments):
        raise ProtocolError("argument_id is already known")

    for issue in turn["new_issues"]:
        match = ISSUE_RE.fullmatch(issue["id"])
        if issue["author"] != reviewer or match is None or match.group(1) != reviewer:
            raise ProtocolError("new issue id/author does not belong to reviewer")
        _validate_anchor(issue["anchor"], source, f"issue {issue['id']}")

    visible_issue_ids = set(issues) | set(issue_ids)
    proposals_by_ref = {proposal["local_ref"]: proposal for proposal in turn["proposals"]}
    proposals_by_issue: dict[str, list[dict[str, Any]]] = {}
    for proposal in turn["proposals"]:
        issue_id = proposal["issue_id"]
        if issue_id not in visible_issue_ids:
            raise ProtocolError(f"proposal references unknown issue_id {issue_id}")
        proposals_by_issue.setdefault(issue_id, []).append(proposal)
        payload = proposal["payload"]
        closes = payload["closes"]
        if closes != sorted(closes) or any(ref not in versions for ref in closes):
            raise ProtocolError("proposal closes must be sorted known earlier versions")
        duplicate_of = payload["duplicate_of"]
        if payload["resolution"] == "duplicate":
            if duplicate_of not in visible_issue_ids or duplicate_of == issue_id:
                raise ProtocolError("duplicate_of must name another known issue")
        elif duplicate_of is not None:
            raise ProtocolError("duplicate_of is only valid for duplicate resolution")

    positions_by_issue: dict[str, dict[str, Any]] = {}
    for position in turn["positions"]:
        issue_id = position["issue_id"]
        if issue_id in positions_by_issue:
            raise ProtocolError(f"positions contains issue {issue_id} more than once")
        positions_by_issue[issue_id] = position
        if issue_id not in visible_issue_ids:
            raise ProtocolError(f"position references unknown issue_id {issue_id}")
        argument_match = ARGUMENT_RE.fullmatch(position["argument_id"])
        if (
            argument_match is None
            or argument_match.group(1) != reviewer
            or int(argument_match.group(2)) != round_no
        ):
            raise ProtocolError("argument_id must identify reviewer and current round")
        for reference in position["responds_to"]:
            if reference not in arguments:
                raise ProtocolError(f"responds_to references unknown argument {reference}")
        for index, anchor in enumerate(position["evidence"]):
            _validate_anchor(anchor, source, f"position evidence {index}")

        version_ref = position["version_ref"]
        if position["action"] == "propose":
            proposal = proposals_by_ref.get(version_ref)
            if proposal is None or proposal["issue_id"] != issue_id:
                raise ProtocolError("propose version_ref must name own proposal")
        elif version_ref not in versions:
            raise ProtocolError("accept/oppose version_ref must name a known version_ref")

        prior = state.get("positions", {}).get(reviewer, {}).get(issue_id)
        if isinstance(prior, dict):
            changed = (
                prior.get("action") != position["action"]
                or prior.get("version_ref") != position["version_ref"]
            )
            if changed and not position["responds_to"] and not position["evidence"]:
                raise ProtocolError(
                    "position change requires a prior argument reference or new source evidence"
                )

    required_positions = set(issues) | set(issue_ids)
    if set(positions_by_issue) != required_positions:
        missing = sorted(required_positions - set(positions_by_issue))
        extra = sorted(set(positions_by_issue) - required_positions)
        raise ProtocolError(f"positions must cover all input and own issues; missing={missing}, extra={extra}")

    for issue_id in issue_ids:
        if not proposals_by_issue.get(issue_id):
            raise ProtocolError(f"new issue {issue_id} requires a proposal")
        if positions_by_issue[issue_id]["action"] != "propose":
            raise ProtocolError(f"new issue {issue_id} requires a propose position")

    return turn
