import json
from pathlib import Path

import pytest

from peer_reviewer.protocol import ProtocolError, parse_turn


FIXTURES = Path(__file__).parents[1] / "fixtures" / "turns"
STATE_HASH = "sha256:" + "1" * 64
SOURCE = "Opening line.\nRisky guarantee.\n"


def load_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def expected(reviewer: str, round_no: int = 1) -> dict:
    return {
        "session_id": "s-001",
        "round_no": round_no,
        "reviewer": reviewer,
        "input_state_hash": STATE_HASH,
    }


def empty_state() -> dict:
    return {"issues": {}, "versions": {}, "arguments": {}, "positions": {}}


def state_with_issue() -> dict:
    return {
        "issues": {"A-1": {"author": "A"}},
        "versions": {"sha256:" + "2" * 64: {"issue_id": "A-1"}},
        "arguments": {"A-r1-1": {"author": "A", "issue_id": "A-1", "round_no": 1}},
        "positions": {
            "A": {
                "A-1": {
                    "issue_id": "A-1",
                    "action": "oppose",
                    "version_ref": "sha256:" + "2" * 64,
                    "argument_id": "A-r1-1",
                }
            },
            "B": {},
        },
    }


def test_valid_independent_reviews_parse_against_empty_state():
    turn_a = parse_turn(load_fixture("round1-a.json"), expected("A"), empty_state(), SOURCE)
    turn_b = parse_turn(load_fixture("round1-b.json"), expected("B"), empty_state(), SOURCE)

    assert turn_a["new_issues"][0]["id"] == "A-1"
    assert turn_b["review_complete"] is True


def test_truncated_json_never_becomes_turn():
    with pytest.raises(ProtocolError, match="JSON"):
        parse_turn(b'{"schema_version":1,', {}, {}, "tekst")


def test_duplicate_json_key_is_rejected():
    raw = b'{"schema_version":1,"schema_version":1}'
    with pytest.raises(ProtocolError, match="Powtórzony klucz: schema_version"):
        parse_turn(raw, {}, {}, SOURCE)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(extra="forbidden"), "schema"),
        (lambda value: value["positions"].clear(), "positions"),
        (lambda value: value["new_issues"][0]["anchor"].update(quote="wrong"), "quote"),
        (lambda value: value["positions"][0].update(version_ref="sha256:" + "9" * 64), "version_ref"),
        (lambda value: value["positions"][0].update(reason=""), "reason"),
    ],
)
def test_malformed_or_incomplete_turn_is_rejected(mutate, message):
    value = json.loads(load_fixture("round1-a.json"))
    mutate(value)

    with pytest.raises(ProtocolError, match=message):
        parse_turn(json.dumps(value).encode(), expected("A"), empty_state(), SOURCE)


def test_foreign_issue_id_is_rejected():
    value = json.loads(load_fixture("round1-a.json"))
    value["new_issues"][0]["id"] = "B-1"
    value["proposals"][0]["issue_id"] = "B-1"
    value["positions"][0]["issue_id"] = "B-1"

    with pytest.raises(ProtocolError, match="author"):
        parse_turn(json.dumps(value).encode(), expected("A"), empty_state(), SOURCE)


def test_round_two_requires_position_for_every_input_issue():
    value = json.loads(load_fixture("round2-b.json"))
    assert parse_turn(
        json.dumps(value).encode(), expected("B", round_no=2), state_with_issue(), SOURCE
    )["positions"][0]["issue_id"] == "A-1"
    value["positions"] = []

    with pytest.raises(ProtocolError, match="positions"):
        parse_turn(json.dumps(value).encode(), expected("B", round_no=2), state_with_issue(), SOURCE)


def test_unknown_argument_reference_is_rejected():
    value = json.loads(load_fixture("round2-b.json"))
    value["positions"][0]["responds_to"] = ["A-r9-9"]

    with pytest.raises(ProtocolError, match="responds_to"):
        parse_turn(json.dumps(value).encode(), expected("B", round_no=2), state_with_issue(), SOURCE)


def test_changed_position_requires_argument_reference_or_new_source_evidence():
    value = json.loads(load_fixture("round2-b.json"))
    state = state_with_issue()
    state["positions"]["B"]["A-1"] = {
        "issue_id": "A-1",
        "action": "oppose",
        "version_ref": "sha256:" + "2" * 64,
        "argument_id": "B-r1-1",
    }
    value["positions"][0]["responds_to"] = []
    value["positions"][0]["evidence"] = []
    with pytest.raises(ProtocolError, match="position change"):
        parse_turn(json.dumps(value).encode(), expected("B", round_no=2), state, SOURCE)


def _keywords(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keywords(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keywords(item)


def test_provider_schema_drops_keywords_rejected_by_provider_structured_output():
    from peer_reviewer.schemas import provider_turn_schema

    schema = provider_turn_schema()
    assert not {"$schema", "uniqueItems", "minLength"} & set(_keywords(schema))
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["position"]["required"] == [
        "issue_id", "action", "version_ref", "argument_id", "reason", "responds_to", "evidence"
    ]


def test_every_provider_schema_node_declares_a_type():
    from peer_reviewer.schemas import provider_turn_schema

    def nodes(schema):
        for name, node in schema.get("properties", {}).items():
            yield name, node
            yield from nodes(node)
        if isinstance(schema.get("items"), dict):
            yield "items", schema["items"]
            yield from nodes(schema["items"])

    schema = provider_turn_schema()
    for definition in [schema, *schema["$defs"].values()]:
        for name, node in nodes(definition):
            assert "type" in node or "$ref" in node, name
