import copy

import pytest

from peer_reviewer.debate import DebateError, has_consensus, initial_state, reduce_round, version_id


STATE_HASH = "sha256:" + "1" * 64


def payload(*, rationale="R1", resolution="accepted", duplicate_of=None, closes=None, fix="F"):
    return {
        "problem": "P",
        "fix": fix,
        "resolution": resolution,
        "rationale": rationale,
        "duplicate_of": duplicate_of,
        "closes": [] if closes is None else closes,
    }


def issue(issue_id="A-1", author="A"):
    return {
        "id": issue_id,
        "author": author,
        "anchor": {"line_start": 1, "line_end": 1, "quote": "text"},
        "problem": "P",
        "significance": "S",
        "reasoning": "R",
        "suggested_fix": "F",
    }


def position(reviewer, round_no, issue_id, action, version_ref, suffix=1, responds_to=None):
    return {
        "issue_id": issue_id,
        "action": action,
        "version_ref": version_ref,
        "argument_id": f"{reviewer}-r{round_no}-{suffix}",
        "reason": "reason",
        "responds_to": [] if responds_to is None else responds_to,
        "evidence": [],
    }


def turn(reviewer, round_no, *, issues=None, proposals=None, positions=None):
    return {
        "schema_version": 1,
        "session_id": "s-001",
        "round_no": round_no,
        "reviewer": reviewer,
        "input_state_hash": STATE_HASH,
        "review_complete": True,
        "new_issues": [] if issues is None else issues,
        "proposals": [] if proposals is None else proposals,
        "positions": [] if positions is None else positions,
    }


def proposal(local_ref, issue_id, value):
    return {"local_ref": local_ref, "issue_id": issue_id, "payload": value}


def first_round():
    proposed = payload()
    return reduce_round(
        initial_state("s-001"),
        (
            turn(
                "A",
                1,
                issues=[issue()],
                proposals=[proposal("a-v1", "A-1", proposed)],
                positions=[position("A", 1, "A-1", "propose", "a-v1")],
            ),
            turn("B", 1),
        ),
    )


def test_changed_rationale_requires_new_acceptances():
    original = payload(rationale="R1")
    changed = payload(rationale="R2")
    assert version_id("A-1", original) != version_id("A-1", changed)


def test_agreement_on_same_version_after_two_rounds_is_consensus():
    state = first_round()
    version = next(iter(state["versions"]))

    state = reduce_round(
        state,
        (
            turn("A", 2, positions=[position("A", 2, "A-1", "accept", version)]),
            turn("B", 2, positions=[position("B", 2, "A-1", "accept", version)]),
        ),
    )

    assert state["resolved"] == {"A-1": version}
    assert has_consensus(state) is True


def test_turn_input_order_does_not_change_material_state():
    previous = initial_state("s-001")
    a = turn(
        "A",
        1,
        issues=[issue()],
        proposals=[proposal("a-v1", "A-1", payload())],
        positions=[position("A", 1, "A-1", "propose", "a-v1")],
    )
    b = turn("B", 1)

    assert reduce_round(previous, (a, b)) == reduce_round(previous, (b, a))


def test_simultaneous_modifications_keep_all_versions_open():
    state = first_round()
    old = next(iter(state["versions"]))
    a_payload = payload(rationale="A2", closes=[old])
    b_payload = payload(rationale="B2", closes=[old])

    state = reduce_round(
        state,
        (
            turn(
                "A", 2,
                proposals=[proposal("a-v2", "A-1", a_payload)],
                positions=[position("A", 2, "A-1", "propose", "a-v2")],
            ),
            turn(
                "B", 2,
                proposals=[proposal("b-v2", "A-1", b_payload)],
                positions=[position("B", 2, "A-1", "propose", "b-v2")],
            ),
        ),
    )

    assert len(state["open_proposals"]["A-1"]) == 3
    assert has_consensus(state) is False


def test_old_acceptance_does_not_accept_new_version():
    state = first_round()
    old = next(iter(state["versions"]))
    changed = payload(rationale="new", closes=[old])

    state = reduce_round(
        state,
        (
            turn("A", 2, positions=[position("A", 2, "A-1", "accept", old)]),
            turn(
                "B", 2,
                proposals=[proposal("b-v2", "A-1", changed)],
                positions=[position("B", 2, "A-1", "propose", "b-v2")],
            ),
        ),
    )

    assert state["resolved"] == {}
    assert has_consensus(state) is False


def test_unilateral_withdrawal_is_not_joint_rejection():
    state = first_round()
    old = next(iter(state["versions"]))
    rejected = payload(resolution="rejected", rationale="withdrawn", closes=[old], fix="")

    state = reduce_round(
        state,
        (
            turn(
                "A", 2,
                proposals=[proposal("reject", "A-1", rejected)],
                positions=[position("A", 2, "A-1", "propose", "reject")],
            ),
            turn("B", 2, positions=[position("B", 2, "A-1", "accept", old)]),
        ),
    )

    assert state["resolved"] == {}


def test_new_issue_in_later_round_prevents_consensus():
    state = first_round()
    original = next(iter(state["versions"]))
    state["round_no"] = 4
    state = reduce_round(
        state,
        (
            turn(
                "A", 5,
                issues=[issue("A-2")],
                proposals=[proposal("a2-v1", "A-2", payload())],
                positions=[
                    position("A", 5, "A-1", "accept", original),
                    position("A", 5, "A-2", "propose", "a2-v1", suffix=2),
                ],
            ),
            turn("B", 5, positions=[position("B", 5, "A-1", "accept", original)]),
        ),
    )

    assert "A-2" in state["open_proposals"]
    assert has_consensus(state) is False


def test_empty_reviews_require_two_complete_rounds():
    state = reduce_round(initial_state("s-001"), (turn("A", 1), turn("B", 1)))
    assert has_consensus(state) is False

    state = reduce_round(state, (turn("A", 2), turn("B", 2)))
    assert has_consensus(state) is True


def test_missing_position_never_becomes_agreement():
    state = first_round()
    version = next(iter(state["versions"]))

    with pytest.raises(DebateError, match="positions"):
        reduce_round(
            state,
            (
                turn("A", 2, positions=[position("A", 2, "A-1", "accept", version)]),
                turn("B", 2),
            ),
        )


def test_duplicate_cycle_is_rejected():
    state = initial_state("s-001")
    state["round_no"] = 1
    a_to_b = payload(resolution="duplicate", duplicate_of="B-1")
    b_to_a = payload(resolution="duplicate", duplicate_of="A-1")
    va = version_id("A-1", a_to_b)
    vb = version_id("B-1", b_to_a)
    state.update(
        issues={"A-1": issue(), "B-1": issue("B-1", "B")},
        versions={
            va: {"issue_id": "A-1", "payload": a_to_b},
            vb: {"issue_id": "B-1", "payload": b_to_a},
        },
        open_proposals={"A-1": [va], "B-1": [vb]},
    )
    positions_a = [
        position("A", 2, "A-1", "accept", va),
        position("A", 2, "B-1", "accept", vb, suffix=2),
    ]
    positions_b = [
        position("B", 2, "A-1", "accept", va),
        position("B", 2, "B-1", "accept", vb, suffix=2),
    ]

    with pytest.raises(DebateError, match="cycle"):
        reduce_round(state, (turn("A", 2, positions=positions_a), turn("B", 2, positions=positions_b)))
