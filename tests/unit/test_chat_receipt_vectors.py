"""The Python half of `DEV-030`'s cross-language receipt contract.

The iOS client's only reason to tell Henson 已写入 is a `record_id` in one of these
bodies. That makes the exact shape of `_operation_projection` part of a contract
two languages have to agree on, so it is pinned in one file both sides read --
`src/personal_agent/api/vectors/chat_receipt_vectors.json`, never a copy.

What each test here defends:

- **a new server state must not become an unreadable receipt.** `OPERATION_STATES`
  is compared against the vector, so adding a state fails here first, while there
  is still a chance to teach the client about it. Without this the client would
  silently render 本客户端无法判定 for a state the server considers routine.
- **the evidence tool set may not drift.** The client refuses to display a write
  for a governed tool that came back without a `record_id`; if the server adds a
  write tool the client does not know, that refusal turns into a *false* refusal.
  Two checks, because comparing the server's set to the vector only proves two
  hand-maintained lists agree: the set is also asserted to be exactly the IR's
  `R2` tools, so a new governed write fails here before it can reach a client
  that would render it as a clean answer.
- **each case really is what the server emits.** The receipts are produced by the
  real projection here rather than typed out, so the Swift suite is asserting
  against the server's output and not against a fixture someone kept in step by
  hand.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_agent.api.app import (
    _QUERY_RESULT_TOOLS,
    _RECORD_ID_RESULT_TOOLS,
    _operation_event_content,
    _operation_projection,
)
from personal_agent.api.finance_dispatcher import (
    _QUERY_RESULT_TOOLS as DISPATCHER_QUERY_RESULT_TOOLS,
)
from personal_agent.api.operation_state import is_terminal
from personal_agent.storage.models import OPERATION_STATES, Operation
from personal_agent_core.tool_ir import TOOL_CONTRACTS

VECTORS_PATH = (
    Path(__file__).parents[2]
    / "src/personal_agent/api/vectors/chat_receipt_vectors.json"
)
V = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))

#: The receipt fields a case may set, and how they land on an `Operation` row.
_OPERATION_FIELDS = (
    "state",
    "cancel_requested",
    "client_detached",
    "tool",
    "failure_reason",
    "duplicate_check_id",
)


def _operation_for(case: dict) -> Operation:
    """Rebuild the row this case's receipt was projected from.

    `record_id`, `answer`, `clarification` and `duplicate_existing` are all the
    same column -- `safe_result` -- which is exactly why the projection has to
    decide between them from the state and the tool, and why this test exists.
    A `query_result` is also the same column: its durable carrier is canonical
    JSON of the whitelisted projection, and the projection re-decodes it into
    `query_result` plus a deterministic `answer` summary.
    """
    receipt = case["receipt"]
    if "query_result" in receipt:
        safe_result = json.dumps(
            receipt["query_result"], ensure_ascii=False, sort_keys=True
        )
    else:
        safe_result = next(
            (
                receipt[name]
                for name in (
                    "record_id",
                    "answer",
                    "clarification",
                    "duplicate_existing",
                )
                if receipt.get(name) is not None
            ),
            None,
        )
    return Operation(
        operation_id=receipt["operation_id"],
        state=receipt["state"],
        cancel_requested=receipt["cancel_requested"],
        client_detached=receipt["client_detached"],
        tool=receipt["tool"],
        failure_reason=receipt["failure_reason"],
        duplicate_check_id=receipt["duplicate_check_id"],
        safe_result=safe_result,
    )


def test_contract_version_is_pinned() -> None:
    assert V["contract"] == "chat_receipt_projection_v4"
    assert V["cases"], "an empty vector file would pass every check vacuously"


def test_every_operation_state_is_in_the_vector() -> None:
    # A state the client has never been told about is projected as
    # "本客户端无法判定结果". That is the right answer for an unknown state and the
    # wrong answer for a state this repo just added, so adding one has to fail here.
    assert V["operation_states"] == list(OPERATION_STATES)


def test_record_evidence_tools_match_the_server() -> None:
    assert V["record_evidence_tools"] == sorted(_RECORD_ID_RESULT_TOOLS)


def test_query_evidence_tools_match_the_server() -> None:
    """The client holds its hard-coded query check against this set."""
    assert V["query_evidence_tools"] == sorted(_QUERY_RESULT_TOOLS)


def test_the_evidence_set_is_derived_from_the_ir_not_hand_listed() -> None:
    """Every R2 tool the IR declares, enabled or not.

    The test above only proves two hand-maintained lists agree with each other,
    which is what let them agree while both were wrong: the set held the three
    *enabled* R2 tools while `finance.log_expense_batch` was already R2, so
    enabling it would have projected a succeeded batch write as an `answer`.
    This is the assertion that catches a future re-hand-listing, and it is
    deliberately independent of `enabled` -- a tool becoming enabled must not be
    the moment the client's refusal turns into a false receipt.
    """

    assert _RECORD_ID_RESULT_TOOLS == {
        contract.name for contract in TOOL_CONTRACTS if contract.risk_level == "R2"
    }
    assert "finance.log_expense_batch" in _RECORD_ID_RESULT_TOOLS


def test_the_query_projection_tool_set_is_derived_from_the_ir() -> None:
    """Every enabled governed *query* must project as a query, not raw JSON.

    A hand-listed query set would drift silently the day a second governed query
    ships: the projection would fall through to the `answer` branch and emit
    canonical JSON as the user-facing reply -- exactly the bug this change
    exists to fix. The API projection and the dispatcher's own set must be the
    same IR-derived set, so the two stages can never disagree about which tool
    is a structured query. `meta.capabilities` is a governed read but not a
    query, and must be excluded so it keeps the plain `answer` path.
    """
    ir_derived = {
        contract.name
        for contract in TOOL_CONTRACTS
        if contract.effect == "read"
        and contract.enabled
        and contract.output_schema.get("properties", {}).get("metric", {}).get(
            "const"
        )
        == "personal_spend_total_cny"
    }
    assert _QUERY_RESULT_TOOLS == ir_derived
    assert DISPATCHER_QUERY_RESULT_TOOLS == ir_derived
    assert "finance.query_expenses" in ir_derived
    assert "meta.capabilities" not in ir_derived


def test_manual_review_preserves_a_known_record_id() -> None:
    case = {
        "receipt": {
            "operation_id": "op_manual_review",
            "state": "needs_manual_review",
            "cancel_requested": False,
            "client_detached": False,
            "tool": "finance.log_expense",
            "record_id": "rec123",
            "failure_reason": "RECEIPT_MISMATCH",
            "duplicate_check_id": None,
        }
    }
    assert _operation_projection(_operation_for(case))["record_id"] == "rec123"


@pytest.mark.parametrize("case", V["cases"], ids=lambda case: case["name"])
def test_each_receipt_is_what_the_projection_emits(case: dict) -> None:
    assert _operation_projection(_operation_for(case)) == case["receipt"]


@pytest.mark.parametrize("case", V["cases"], ids=lambda case: case["name"])
def test_receipt_fields_are_closed(case: dict) -> None:
    # A field the client cannot name is a field it would ignore. Keeping the set
    # closed means a new one has to be added on both sides deliberately.
    allowed = {
        "operation_id",
        "state",
        "cancel_requested",
        "client_detached",
        "tool",
        "record_id",
        "failure_reason",
        "duplicate_check_id",
        "clarification",
        "duplicate_existing",
        "answer",
        "query_result",
    }
    assert set(case["receipt"]) <= allowed


@pytest.mark.parametrize("case", V["cases"], ids=lambda case: case["name"])
def test_only_a_governed_write_carries_record_evidence(case: dict) -> None:
    receipt = case["receipt"]
    if receipt.get("record_id") is not None:
        assert receipt["tool"] in _RECORD_ID_RESULT_TOOLS
        # `needs_manual_review` also keeps its record id: something may exist and
        # could not be verified. Only `succeeded` may be presented as a write.
        assert receipt["state"] in {"succeeded", "needs_manual_review"}
    if case["expected_proves_write"]:
        assert receipt["state"] == "succeeded"
        assert receipt["record_id"]


@pytest.mark.parametrize("case", V["cases"], ids=lambda case: case["name"])
def test_settled_never_contradicts_the_state_machine(case: dict) -> None:
    state = case["receipt"]["state"]
    parked = {"waiting_for_clarification", "waiting_for_duplicate_decision"}
    # The client stops polling on a terminal state *and* on a parked one, because a
    # parked operation's only exit is a new operation the user starts.
    assert case["expected_settled"] == (is_terminal(state) or state in parked)


@pytest.mark.parametrize("case", V["cases"], ids=lambda case: case["name"])
def test_pending_release_requires_a_safe_next_action(case: dict) -> None:
    outcome = case["expected_outcome"]
    unsafe_to_release = {
        "running",
        "needs_manual_review",
        "indeterminate",
    }
    assert case["expected_releases_pending"] == (
        outcome not in unsafe_to_release
    )


@pytest.mark.parametrize("case", V["cases"], ids=lambda case: case["name"])
def test_a_detached_client_is_never_projected_as_a_rollback(case: dict) -> None:
    receipt = case["receipt"]
    if receipt["state"] == "cancelled_pre_submit":
        assert case["expected_cancellation"] == "cancelled_before_submit"
    elif receipt["cancel_requested"] or receipt["client_detached"]:
        assert (
            case["expected_cancellation"] == "requested_outcome_still_authoritative"
        )
        assert case["expected_outcome"] != "cancelled_before_submit"
    else:
        assert case["expected_cancellation"] == "none"


def _case(name: str) -> dict:
    return next(case for case in V["cases"] if case["name"] == name)


def test_query_receipt_and_timeline_event_carry_the_same_facts() -> None:
    """The immediate projection and the Timeline event must never disagree."""
    for name in ("query_total", "query_by_category", "query_records"):
        operation = _operation_for(_case(name))
        receipt = _operation_projection(operation)
        event = _operation_event_content(operation)
        assert event["tool"] == receipt["tool"] == "finance.query_expenses"
        assert event["query_result"] == receipt["query_result"]
        assert event["answer"] == receipt["answer"]


def test_a_query_safe_result_that_does_not_decode_fails_closed() -> None:
    operation = _operation_for(
        {
            "receipt": {
                "operation_id": "op_bad_query",
                "state": "succeeded",
                "cancel_requested": False,
                "client_detached": False,
                "tool": "finance.query_expenses",
                "record_id": None,
                "failure_reason": None,
                "duplicate_check_id": None,
            }
        }
    )
    operation.safe_result = "not a query projection"
    receipt = _operation_projection(operation)
    # A result that cannot be projected is never shown as an answer and never
    # becomes a card; it stays silently unknown until recovery.
    assert "query_result" not in receipt
    assert "answer" not in receipt
    event = _operation_event_content(operation)
    assert "query_result" not in event
    assert "answer" not in event


def test_query_result_is_only_projected_for_the_query_tool() -> None:
    """A write card must never regress into a query card."""
    for case in V["cases"]:
        operation = _operation_for(case)
        if case["receipt"]["tool"] != "finance.query_expenses":
            assert "query_result" not in _operation_projection(operation)


def test_new_events_carry_tool_explicitly_even_when_null() -> None:
    """`tool` is a fact the server recorded; its absence is 'unknown', not 'no tool'."""
    operation = _operation_for(
        {
            "receipt": {
                "operation_id": "op_direct_answer",
                "state": "succeeded",
                "cancel_requested": False,
                "client_detached": False,
                "tool": None,
                "record_id": None,
                "failure_reason": None,
                "duplicate_check_id": None,
                "answer": "好的",
            }
        }
    )
    event = _operation_event_content(operation)
    assert "tool" in event
    assert event["tool"] is None
