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
import re
from pathlib import Path

import pytest

from personal_agent.api.app import (
    _CALENDAR_QUERY_RESULT_TOOLS,
    _QUERY_RESULT_TOOLS,
    _RECORD_ID_RESULT_TOOLS,
    _operation_event_content,
    _operation_projection,
)
from personal_agent.api.finance_dispatcher import (
    _CALENDAR_QUERY_RESULT_TOOLS as DISPATCHER_CALENDAR_QUERY_RESULT_TOOLS,
)
from personal_agent.api.finance_dispatcher import (
    _QUERY_RESULT_TOOLS as DISPATCHER_QUERY_RESULT_TOOLS,
)
from personal_agent.api.finance_record_projection import (
    decode_finance_expense_record,
    seal_expense_record,
)
from personal_agent.api.calendar_issue import CALENDAR_DEVICE_TOOL, may_override
from personal_agent.api.operation_state import is_terminal
from personal_agent.storage.models import OPERATION_STATES, Operation
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.tool_ir import (
    ALLOWED_EXPENSE_CATEGORIES,
    DEFAULT_CLIENT_WIRE_VERSION,
    DEVICE_EXECUTED_TOOL_NAMES,
    TOOL_CONTRACTS,
)

#: One ring for the whole module. These vectors never leave the process, and
#: the projection has to open what the write path sealed -- the point of the
#: file is that both halves are the server's own code.
RING = KeyRing([generate_key("receipt-vectors")], service="personal-agent-api")

def _swift_source() -> str:
    """The iOS half of this contract, read as text.

    Read rather than restated for the same reason the vectors themselves are
    read: two copies of a rule drift, and the drift here draws a button whose
    press writes a second copy of an event the user already has.
    """
    path = Path(__file__).parents[2] / "ios/PersonalAgentKit/Sources/PersonalAgentKit/ChatWire.swift"
    assert path.is_file(), f"{path} is gone; the iOS half moved and this test cannot see it"
    return path.read_text(encoding="utf-8")


#: Every vector in this file is a receipt for a *settled* operation, so the
#: client capability gate cannot be what decides any of them -- a settled
#: operation carries no device action to withhold. Version 1 is passed to
#: say exactly that: these bodies are the shape every client sees.
_WIRE = DEFAULT_CLIENT_WIRE_VERSION

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
    operation_id = receipt["operation_id"]
    record = receipt.get("record")
    return Operation(
        operation_id=operation_id,
        # The action id a device action is reported and overridden by *is* the
        # operation's idempotency key (`_owned_action_operation`), so a case that
        # declares one declares this column. The projection is what decides
        # whether it may be emitted at all -- see
        # `test_a_connector_write_never_exposes_its_idempotency_key`, which sets
        # the key on a tool the device does not execute and requires null.
        idempotency_key=receipt.get("device_action_id") or f"key-{operation_id}",
        state=receipt["state"],
        cancel_requested=receipt["cancel_requested"],
        client_detached=receipt["client_detached"],
        tool=receipt["tool"],
        failure_reason=receipt["failure_reason"],
        duplicate_check_id=receipt["duplicate_check_id"],
        device_result=receipt.get("device_result"),
        safe_result=safe_result,
        # `G1`'s business fields are their own sealed column, not more content
        # in `safe_result`. Sealing them here with the same helper the write
        # path uses is what makes these vectors the server's real output rather
        # than a hand-written approximation of it.
        encrypted_result_record=(
            None
            if record is None
            else seal_expense_record(
                RING,
                operation_id=operation_id,
                record=decode_finance_expense_record(record),
            )
        ),
    )


def test_contract_version_is_pinned() -> None:
    assert V["contract"] == "chat_receipt_projection_v9"
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


def test_calendar_query_evidence_tools_match_the_server() -> None:
    """`step 6`'s second pinned set, on the same terms as the first.

    The client holds its own `calendarQueryEvidenceTools` against this, and that
    set is what decides which of the two projections a `query_result` body is
    even decoded as. Two hand-maintained lists agreeing proves only that they
    agree; the derivation is asserted separately below.
    """
    assert V["calendar_query_evidence_tools"] == sorted(_CALENDAR_QUERY_RESULT_TOOLS)


def test_the_calendar_query_projection_tool_set_is_derived_from_the_ir() -> None:
    """Every enabled governed read of the mirror, and only those.

    `finance_query_projection` and `calendar_query_projection` are separate
    modules precisely so neither can decode the other's result, and the sets
    that select them are what keep that true: the fork keys on the output
    schema's own const, so a renamed field or a newly enabled mirror read fails
    here rather than being rendered through the wrong card.
    """
    ir_derived = {
        contract.name
        for contract in TOOL_CONTRACTS
        if contract.effect == "read"
        and contract.enabled
        and contract.output_schema.get("properties", {})
        .get("source_system", {})
        .get("const")
        == "apple_calendar_mirror"
    }
    assert _CALENDAR_QUERY_RESULT_TOOLS == ir_derived
    assert DISPATCHER_CALENDAR_QUERY_RESULT_TOOLS == ir_derived
    assert "calendar.query_events" in ir_derived
    # Disjoint from the ledger's set, or one result could be decoded as the
    # other domain's card.
    assert not (ir_derived & _QUERY_RESULT_TOOLS)


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


def test_the_calendar_domain_is_the_literal_the_client_forks_on() -> None:
    """The 人工核对 card is chosen by domain, and the client compares a literal.

    `OperationReceipt.calendarDomain` is a hard-coded `"calendar"` in Swift —
    there is no way for the client to ask the server what the string is at the
    moment it draws a card. So the string itself is a contract, and this is the
    end of the chain that holds it: the IR declares it, the vector records it,
    and the Swift literal must equal it. A rename that moved only the IR would
    make `ManualReviewCopy.forDomain` fall through to the ledger copy for every
    calendar write — silently, and in the direction that sends Henson to the
    wrong place to check a write.
    """
    calendar_domains = {
        contract.domain
        for contract in TOOL_CONTRACTS
        if contract.name.startswith("calendar.")
    }
    assert calendar_domains == {"calendar"}

    for name in ("calendar_manual_review_keeps_record", "calendar_query_list_card"):
        case = next(case for case in V["cases"] if case["name"] == name)
        assert case["receipt"]["domain"] == "calendar", name

    ledger = next(
        case for case in V["cases"] if case["name"] == "manual_review_keeps_record"
    )
    assert ledger["receipt"]["domain"] != "calendar"


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
    assert _operation_projection(RING, _operation_for(case), client_wire_version=_WIRE)["record_id"] == "rec123"


def test_device_executed_tools_match_the_server() -> None:
    """The client's card choice is held to the IR, not to a name it recognises.

    The client draws its own card for a device-executed tool and a ledger receipt
    for everything else, so a second device tool shipping without this list being
    updated means its receipt renders as a ledger row -- the 2026-09-10 defect,
    reachable again by a different route.
    """
    assert V["device_executed_tools"] == sorted(DEVICE_EXECUTED_TOOL_NAMES)


@pytest.mark.parametrize("case", V["cases"], ids=lambda case: case["name"])
def test_a_connector_write_never_exposes_its_idempotency_key(case: dict) -> None:
    """The fail-closed half of `device_action_id`.

    Every case's reconstructed row carries an idempotency key -- real where the
    case declares an action id, synthetic otherwise -- so "it is null" can only
    mean the projection withheld it. A Finance write's key is an internal
    handle, and a card that named it would be naming the thing a replay is
    addressed by.
    """
    receipt = case["receipt"]
    is_device = receipt["tool"] in DEVICE_EXECUTED_TOOL_NAMES
    assert (receipt["device_action_id"] is not None) is is_device, case["name"]


def test_only_a_device_write_can_report_what_the_phone_decided() -> None:
    """`device_result` is a fact about a write *this phone* performed.

    A connector write is executed by the backend, so a body claiming `created`
    for one is describing an executor that does not exist. Pinning it here keeps
    the field from becoming a general-purpose "how did it go" that a card might
    learn to read for the wrong domain.
    """
    for case in V["cases"]:
        receipt = case["receipt"]
        if receipt["device_result"] is not None:
            assert receipt["tool"] in DEVICE_EXECUTED_TOOL_NAMES, case["name"]
            assert receipt["device_result"] in {"created", "duplicate"}, case["name"]


@pytest.mark.parametrize("case", V["cases"], ids=lambda case: case["name"])
def test_each_receipt_is_what_the_projection_emits(case: dict) -> None:
    assert _operation_projection(RING, _operation_for(case), client_wire_version=_WIRE) == case["receipt"]


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
        # Which domain the tool belongs to (v7, design §10). The client picks
        # the 人工核对 card by this: a calendar write is checked in the
        # calendar, not in the ledger.
        "domain",
        "record_id",
        "failure_reason",
        "duplicate_check_id",
        # What the phone decided about a device-executed write (v8): `created`
        # or `duplicate`, both of which settle as `succeeded` with the same
        # event id. The client draws a different card for each, and only
        # `duplicate` may offer 「仍要创建」.
        "device_result",
        # The action id that override names. Emitted only for a tool the IR
        # marks device-executed; null for every connector write, so an internal
        # idempotency key never reaches a card.
        "device_action_id",
        "clarification",
        "duplicate_existing",
        "answer",
        "query_result",
        "record",
        # The issued device action, delivered by the projection while the
        # operation is parked (review R6). The vector file carries no such
        # case -- these vectors are receipt *projections* of settled shapes,
        # and a parked one that still owes an action is exercised in
        # test_calendar_device_action.py -- but the field is part of the same
        # closed contract and the client knows its shape.
        "device_action",
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
def test_business_fields_never_travel_without_the_write_they_describe(
    case: dict,
) -> None:
    """`G1`'s one safety rule.

    A card showing 名称 / 金额 / 分类 reads as "this is in your ledger". If a
    body could carry those fields without a `record_id`, the card would make
    that claim for a write nothing proved -- which is the exact failure the
    whole receipt projection exists to prevent, arrived at from the other side.
    """
    receipt = case["receipt"]
    if receipt.get("record") is not None:
        assert receipt["record_id"], "business fields with no proven write"
        assert receipt["state"] == "succeeded"
        assert receipt["tool"] in _RECORD_ID_RESULT_TOOLS


@pytest.mark.parametrize("case", V["cases"], ids=lambda case: case["name"])
def test_a_receipt_record_carries_only_ledger_fields(case: dict) -> None:
    record = case["receipt"].get("record")
    if record is None:
        return
    assert set(record) <= {
        "name",
        "amount_cny",
        "occurred_on",
        "is_family_expense",
        "category",
        "personal_spend_cny",
        "category_updated_at",
    }
    # Money stays a decimal string on the wire in both languages. A float here
    # would be a float in Swift too, and ¥0.10 would stop being ¥0.10.
    for field in ("amount_cny", "personal_spend_cny"):
        if field in record:
            assert isinstance(record[field], str)
    assert isinstance(record["is_family_expense"], bool)
    if record.get("category") is not None:
        assert record["category"] in ALLOWED_EXPENSE_CATEGORIES


def test_the_calendar_list_card_case_carries_what_the_card_draws() -> None:
    """The row fields of §9.2, pinned in the one file both languages read.

    The iOS list card itself is design step 6; what lands here is the shape it
    will read, so the server cannot quietly drop the calendar's name, the
    all-day dates, or the null that means「这个日历没有名字」.
    """
    events = _case("calendar_query_list_card")["receipt"]["query_result"]["events"]

    timed = next(event for event in events if event["timezone"] == "Asia/Tokyo")
    assert timed["calendar_title"] == "演出&活动"
    assert timed["all_day"] is False

    all_day = next(event for event in events if event["event_identifier"] == "evt-trip")
    assert all_day["calendar_title"] == "出游计划"
    # The exclusive end date, straight from EventKit's own convention.
    assert (all_day["start_date"], all_day["end_date"]) == ("2026-10-01", "2026-10-04")
    assert all_day["timezone"] is None, "an all-day event has no anchor zone"

    unconfirmed = next(
        event for event in events if event["calendar_identifier"] == "uuid-flight"
    )
    assert unconfirmed["date_anchor_unknown"] is True

    unnamed = next(
        event for event in events if event["calendar_identifier"] == "uuid-gone"
    )
    assert "calendar_title" in unnamed
    assert unnamed["calendar_title"] is None, (
        "a calendar the device never listed has no name, and the identifier "
        "is not a substitute for one"
    )


def test_the_editable_category_set_is_the_ledgers_own_options() -> None:
    """What the client may offer in the 分类 picker.

    The connector never creates a select option, so an option the client
    invents is a refused write at best. Pinning the set here means the picker
    and the ledger's single-select cannot drift apart silently.
    """
    assert V["expense_categories"] == list(ALLOWED_EXPENSE_CATEGORIES)


def test_an_edited_category_drops_the_stale_formula_value() -> None:
    """个人支出 may depend on 分类, and this side does not know whether it does.

    Carrying the pre-edit number forward would put a figure on the card that
    the ledger may no longer agree with; recomputing it here would rebuild the
    formula the config freezes read-only. Absence is the honest third option.
    """
    record = _case("expense_category_edited")["receipt"]["record"]
    assert record["category_updated_at"]
    assert "personal_spend_cny" not in record


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
    """The immediate projection and the Timeline event must never disagree.

    Checked for both governed queries — Finance's and the calendar mirror's —
    because a second query is exactly when a hand-listed comparison would
    quietly stop covering the new one.
    """
    for name in (
        "query_total",
        "query_by_category",
        "query_records",
        "calendar_query_list_card",
    ):
        operation = _operation_for(_case(name))
        receipt = _operation_projection(RING, operation, client_wire_version=_WIRE)
        event = _operation_event_content(RING, operation)
        # The tool is compared to itself rather than to a literal: which tool
        # a case is about is the case's own business, and hard-coding one is
        # how this assertion would have stopped covering the calendar read.
        assert event["tool"] == receipt["tool"]
        assert event["query_result"] == receipt["query_result"]
        assert event["answer"] == receipt["answer"]
        assert event["domain"] == receipt["domain"]


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
    receipt = _operation_projection(RING, operation, client_wire_version=_WIRE)
    # A result that cannot be projected is never shown as an answer and never
    # becomes a card; it stays silently unknown until recovery.
    assert "query_result" not in receipt
    assert "answer" not in receipt
    event = _operation_event_content(RING, operation)
    assert "query_result" not in event
    assert "answer" not in event


def test_query_result_is_only_projected_for_a_governed_query() -> None:
    """A write card must never regress into a query card.

    The set of tools allowed a `query_result` is the IR-derived pair of
    projections, not one tool's name: the literal `finance.query_expenses`
    this assertion used to compare against would have called the calendar
    mirror's own governed read a regression.
    """
    for case in V["cases"]:
        operation = _operation_for(case)
        projection = _operation_projection(RING, operation, client_wire_version=_WIRE)
        if case["receipt"]["tool"] not in (
            _QUERY_RESULT_TOOLS | _CALENDAR_QUERY_RESULT_TOOLS
        ):
            assert "query_result" not in projection
        else:
            assert "query_result" in projection


def test_every_receipt_states_the_domain_of_its_tool() -> None:
    """The client picks the 人工核对 card by domain, so the domain is a fact
    the server states — and it is the tool contract's own domain, not a second
    mapping beside the IR. Null means no tool was recorded: an operation whose
    tool is unknown has no domain to report, and the card says that rather
    than defaulting to the ledger.
    """
    domains = {contract.name: contract.domain for contract in TOOL_CONTRACTS}
    for case in V["cases"]:
        receipt = case["receipt"]
        assert "domain" in receipt, "the field is always present, like `tool`"
        tool = receipt["tool"]
        assert receipt["domain"] == (domains.get(tool) if tool else None)
    # The two domains the card splits on are both exercised by a case, or the
    # split would be untested on the side that matters.
    assert {
        case["receipt"]["domain"]
        for case in V["cases"]
        if case["receipt"]["state"] == "needs_manual_review"
    } == {"calendar", "finance"}


def test_the_override_rule_is_one_rule_stated_once() -> None:
    """`may_override` and the client's `overrideDecision`, held together (v9).

    Whether 「仍要创建」 may be answered is decided by the server
    (`calendar_issue.may_override`) and drawn by the client
    (`OperationOutcome.overrideDecision`). Two statements of one predicate drift,
    and this pair drifts in the direction that writes a second copy of an event
    the user already has -- so the vector carries the answer per case, the Swift
    suite asserts the client's decision against it, and this asserts the server's.

    The tool name is read back out of the Swift source rather than restated, so
    a rename on either side fails here instead of leaving two names that agree
    only by having been typed the same day.
    """
    swift = _swift_source()
    match = re.search(
        r'public static let calendarDeviceTool = "([^"]+)"', swift
    )
    assert match is not None, (
        "the client no longer names the override tool as a single constant; "
        "the rule cannot be held equal to the server's without one"
    )
    assert match.group(1) == CALENDAR_DEVICE_TOOL
    assert V["override_tool"] == CALENDAR_DEVICE_TOOL

    for case in V["cases"]:
        receipt = case["receipt"]
        assert may_override(
            tool=receipt["tool"],
            state=receipt["state"],
            device_result=receipt.get("device_result"),
        ) is (case["expected_override_action_id"] is not None), case["name"]

    # And the criterion is narrow in both directions: a `created` report settles
    # with the same state and the same event id, so only the phone having *found*
    # an event may be overridden.
    assert may_override(
        tool=CALENDAR_DEVICE_TOOL, state="succeeded", device_result="created"
    ) is False
    assert may_override(
        tool=CALENDAR_DEVICE_TOOL, state="succeeded", device_result=None
    ) is False
    # A second device tool does not inherit this by being device-executed.
    assert may_override(
        tool="finance.log_expense", state="succeeded", device_result="duplicate"
    ) is False


def test_only_a_duplicate_names_an_override_action() -> None:
    """The action id is the override's target, and nothing else's.

    `record_id` and `device_action_id` travel in the same body and mean
    different things -- the EventKit identifier the phone reported, and the key
    the server will accept an override under. A card that sent the first would
    be refused by the endpoint's UUID pin rather than misapplied, but only if
    the two are never conflated on the way there.
    """
    for case in V["cases"]:
        action_id = case["expected_override_action_id"]
        if action_id is None:
            continue
        receipt = case["receipt"]
        assert receipt["device_result"] == "duplicate", case["name"]
        assert action_id == receipt["device_action_id"], case["name"]
        assert action_id != receipt["record_id"], case["name"]


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
    event = _operation_event_content(RING, operation)
    assert "tool" in event
    assert event["tool"] is None
