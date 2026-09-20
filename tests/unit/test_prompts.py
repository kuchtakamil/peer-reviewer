import copy

import pytest

from peer_reviewer.prompts import PromptTooLarge, build_context, build_prompt


def test_context_numbers_source_lines_and_has_stable_state_hash():
    state = {"round_no": 1, "issues": {}, "versions": {}, "arguments": {}, "positions": {}}
    context_a = build_context("one\ntwo\n", state, ["correctness"])
    context_b = build_context("one\ntwo\n", copy.deepcopy(state), ["correctness"])
    assert context_a["numbered_source"] == "1: one\n2: two"
    assert context_a["input_state_hash"] == context_b["input_state_hash"]


def test_both_reviewers_receive_same_frozen_state_without_current_a_answer():
    state = {
        "round_no": 1,
        "issues": {},
        "versions": {},
        "arguments": {},
        "positions": {},
        "last_turns": {"A": "previous A", "B": "previous B"},
    }
    context = build_context("source", state, ["correctness"])
    prompt_a = build_prompt(context, "A")
    prompt_b = build_prompt(context, "B")
    assert context["input_state_hash"] in prompt_a
    assert context["input_state_hash"] in prompt_b
    assert "previous A" in prompt_b and "previous B" in prompt_a
    assert "current A" not in prompt_b


def test_first_round_contains_source_and_rules_but_no_partner_history():
    context = build_context("source", {"session_id": "custom-session", "round_no": 0}, ["correctness"])
    prompt = build_prompt(context, "A")
    assert "BEGIN IMMUTABLE REVIEW MATERIAL" in prompt
    assert "source" in prompt
    assert "previous" not in prompt
    assert '"session_id": "custom-session"' in prompt
    assert '"round_no": 1' in prompt
    assert '"reviewer": "A"' in prompt
    assert context["input_state_hash"] in prompt


def test_prompt_size_limit_fails_instead_of_truncating():
    context = build_context("x" * 100, {"round_no": 0}, ["correctness"])
    with pytest.raises(PromptTooLarge):
        build_prompt(context, "A", max_bytes=32)
