"""The §5.1 guard on the isolated acceptance build's synthetic facts.

The acceptance build (`AcceptanceScenario.swift` and the `#if ACCEPTANCE`
composition beside it) has one job: draw the *shipping* cards over facts that
came from nowhere. Its whole value rests on the second half of that -- that the
facts it seeds are the shape the server really emits. A harness whose receipts
were written from the same mental model as the card would render a card nobody
could ever see in production, and it would look exactly like a passing
acceptance run.

That is the failure §5.1 names, and the countermeasure it prescribes is the one
taken here: the harness's seeded shapes are held against the server's own
emitters, read as source, so a field the server does not emit fails here before
it can be drawn on a phone and approved.

What each check defends:

- **every seeded event-content field is one the server writes.** The allowed set
  is extracted from `_operation_event_content` itself (its literal keys plus its
  `for name in (...)` tuple), not restated. A field added to the harness and not
  to the server -- the realistic drift, since only the harness is easy to edit --
  fails here.
- **every seeded projection field is one the server writes**, including the
  keys `_operation_projection` assigns conditionally.
- **the calendar query seed would survive the server's strict decoder.** This
  one is stronger than a key comparison: the decoder whitelists fields and
  requires several, so the check is *the seed has no key the decoder rejects and
  no required key missing*. The required set is derived by elimination against
  the real decoder rather than copied from it, so a future change that makes a
  field optional moves the assertion with it instead of turning this test into a
  false alarm.
- **the constants the harness names are the server's constants.** A seeded
  receipt under a renamed tool would stop offering 「仍要创建」, and the checklist
  would go red for a reason that has nothing to do with the button.

Nothing here reads a real calendar, a real model or a network. The harness this
pins is itself inert: `ios/scripts/check_acceptance_isolation.sh` asserts from
the built products that no production binary can select it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from personal_agent.api.calendar_issue import CALENDAR_DEVICE_TOOL
from personal_agent.api.calendar_query_projection import (
    _EVENT_FIELDS,
    _SOURCE_SYSTEM,
    _TOP_LEVEL_FIELDS,
    CalendarQueryProjectionError,
    decode_calendar_query_projection,
)
from personal_agent.api.manual_review import MANUAL_REVIEW_DOMAIN_FIELD
from personal_agent_core.tool_ir import domain_of_tool

ROOT = Path(__file__).parents[2]
KIT = ROOT / "ios/PersonalAgentKit/Sources/PersonalAgentKit"
SCENARIO_SWIFT = KIT / "AcceptanceScenario.swift"
CHATWIRE_SWIFT = KIT / "ChatWire.swift"
CHATWIRE_CARD_SWIFT = KIT / "CalendarQueryCard.swift"

# ---------------------------------------------------------------- python side


def _function_node(name: str) -> ast.FunctionDef:
    """One function of `app.py`, parsed rather than text-matched.

    Parsed because the thing being extracted is a *key set*, and text matching
    cannot tell a key from a comment that names one -- `_operation_projection`
    explains `device_result` in prose two lines above assigning it.
    """
    source = (ROOT / "src/personal_agent/api/app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is gone from app.py; this test cannot see the contract")


def _dict_keys(value: ast.expr) -> set[str]:
    assert isinstance(value, ast.Dict), "expected a dict literal"
    keys = set()
    for key in value.keys:
        assert isinstance(key, ast.Constant) and isinstance(key.value, str)
        keys.add(key.value)
    return keys


def _event_content_keys() -> set[str]:
    """The key set `_operation_event_content` can produce."""
    node = _function_node("_operation_event_content")
    keys: set[str] = set()
    for child in ast.walk(node):
        # `content: dict[str, Any] = {"state": ..., "tool": ...}`
        if (
            isinstance(child, ast.AnnAssign)
            and isinstance(child.target, ast.Name)
            and child.target.id == "content"
        ):
            keys |= _dict_keys(child.value)
        # `for name in ("domain", "record_id", ...)`
        if (
            isinstance(child, ast.For)
            and isinstance(child.target, ast.Name)
            and child.target.id == "name"
            and isinstance(child.iter, ast.Tuple)
        ):
            for element in child.iter.elts:
                assert isinstance(element, ast.Constant)
                keys.add(element.value)
    return keys


def _subscript_assigned_keys(node: ast.FunctionDef, name: str) -> set[str]:
    keys = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Assign):
            continue
        for target in child.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == name
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
            ):
                keys.add(target.slice.value)
    return keys


def _projection_keys() -> set[str]:
    """The key set `_operation_projection` can produce.

    Both halves: the literal body it always builds, and the fields it assigns
    behind a condition. A field that only ever appears on the conditional path
    is still a field the server writes, and the harness may seed it.
    """
    node = _function_node("_operation_projection")
    keys = _subscript_assigned_keys(node, "projection")
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Assign)
            and isinstance(child.targets[0], ast.Name)
            and child.targets[0].id == "projection"
        ):
            keys |= _dict_keys(child.value)
    return keys


# ------------------------------------------------------------------ swift side

#: A Swift `JSONValue`/dictionary literal key: `"state": .string(...)`. The
#: colon has to follow the closing quote, which is what keeps this off the
#: string *values* -- `"2026-10-01"` is followed by `)`, not `:`.
_SWIFT_OBJECT_KEY = re.compile(r'"([a-z_]+)"\s*:')

#: A tuple entry in the optional-field list: `("domain", operation.domain...)`.
_SWIFT_TUPLE_KEY = re.compile(r'\(\s*"([a-z_]+)"\s*,')


def _swift_file(path: Path) -> str:
    assert path.is_file(), f"{path} is gone; the iOS half moved and this test cannot see it"
    return path.read_text(encoding="utf-8")


def _swift_body(source: str, signature: str) -> str:
    """The `{...}` body of a Swift declaration, brace-matched.

    Sliced by signature rather than by line numbers so an edit above it does not
    silently move the window onto a different function -- which would leave this
    test reading *something*, and passing.
    """
    start = source.index(signature)
    opening = source.index("{", start + len(signature))
    depth = 0
    for index in range(opening, len(source)):
        character = source[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : index]
    raise AssertionError(f"{signature} has no closing brace; the slice is not a function")


def _scenario_source() -> str:
    return _swift_file(SCENARIO_SWIFT)


def _swift_event_content_keys() -> set[str]:
    body = _swift_body(_scenario_source(), "static func eventContent(")
    return set(_SWIFT_OBJECT_KEY.findall(body)) | set(_SWIFT_TUPLE_KEY.findall(body))


def _swift_projection_keys() -> set[str]:
    """The projection the harness seeds, both halves of it.

    `projection(_:)` writes five keys literally and copies the rest from
    `eventContent(_:)` in a loop, so the literal keys alone are not the seeded
    set. The copied keys are the event-content keys minus the two it already
    wrote, and they are part of the claim this file makes.
    """
    body = _swift_body(_scenario_source(), "static func projection(")
    literal = set(_SWIFT_OBJECT_KEY.findall(body))
    copied = _swift_event_content_keys() - {"state", "tool"}
    return literal | copied


def _swift_constant_body(source: str, name: str) -> str:
    """The text of a `let` whose literal is bracketed, not braced.

    `_swift_body` brace-matches, which works for the two functions above and
    cannot work here: `calendarQueryProjection` is one `JSONValue.object([...])`
    expression with no brace of its own, so brace matching would run past the
    end of the declaration and slice whatever came next -- text that still
    contains keys, so the test would pass while reading the wrong literal. It is
    sliced by the closing `])` at the declaration's own indentation instead.
    """
    lines = source.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if f"let {name} " in line), None
    )
    assert start is not None, f"{name} is gone from AcceptanceScenario.swift"
    for index in range(start + 1, len(lines)):
        if lines[index] == "    ])":
            return "\n".join(lines[start : index + 1])
    raise AssertionError(f"{name}'s literal never closes; the slice is not a declaration")


def _swift_calendar_query_keys() -> tuple[set[str], set[str]]:
    """`(top-level keys, per-event keys)` of the seeded calendar query result.

    Split on `"events"`, which is the last top-level key in the literal and the
    only place a nested object begins. Both halves are asserted non-vacuous
    below, so a slice that landed on the wrong text fails rather than checks
    nothing.
    """
    body = _swift_constant_body(_scenario_source(), "calendarQueryProjection")
    head, separator, tail = body.partition('"events"')
    assert separator, "the seeded calendar projection no longer has an events member"
    # The separator is itself the top-level key the split consumed.
    top = set(_SWIFT_OBJECT_KEY.findall(head)) | {"events"}
    return top, set(_SWIFT_OBJECT_KEY.findall(tail))


def _swift_constant(file: Path, name: str) -> str:
    source = _swift_file(file)
    match = re.search(rf'let {name} = "([^"]+)"', source)
    assert match, f"{name} is gone from {file.name}; the two languages disagree unnoticed"
    return match.group(1)


#: The harness's own seeded marker keys. Written as patterns rather than sliced
#: out of `seed(from:)` because the marker is built in two statements -- a
#: literal for the conclusion and a conditional subscript for the domain -- and
#: a slice would also pick up the operation-result content built in the case
#: directly above.
_SWIFT_MARKER_LITERAL = re.compile(r'"([a-z_]+)"\s*:\s*\.string\(resolution\)')
_SWIFT_MARKER_SUBSCRIPT = re.compile(r'content\["([a-z_]+)"\]')


def _swift_marker_keys() -> set[str]:
    body = _swift_body(_scenario_source(), "public static func seed(")
    return set(_SWIFT_MARKER_LITERAL.findall(body)) | set(
        _SWIFT_MARKER_SUBSCRIPT.findall(body)
    )


# ---------------------------------------------------------------------- tests


class TestSeededShapesAreTheServersShapes:
    """The harness may only seed fields the server can emit."""

    def test_the_extractors_found_the_contract(self) -> None:
        """Positive control. Every check below is a subset test, and a subset
        test over an empty or mis-sliced set passes for the wrong reason."""
        event_keys = _event_content_keys()
        projection_keys = _projection_keys()
        assert {"state", "tool", "domain", "record_id"} <= event_keys
        assert {"operation_id", "state", "cancel_requested", "client_detached"} <= (
            projection_keys
        )
        # `device_actions` is assigned only on the delivery path; if the
        # extraction ever stopped seeing conditional assignments this would be
        # the assertion that noticed.
        assert "device_actions" in projection_keys

    def test_seeded_event_content_fields_are_the_servers_own(self) -> None:
        seeded = _swift_event_content_keys()
        # The two the server always writes, and four the harness must exercise.
        # Without this the assertion below is satisfied by a harness that seeds
        # nothing.
        assert {"state", "tool"} <= seeded
        assert {"domain", "record_id", "device_result", "device_action_id"} <= seeded
        assert seeded <= _event_content_keys(), (
            "AcceptanceScenario seeds an operation-result field the server never "
            "writes; a card drawn from it is a card production cannot show"
        )

    def test_seeded_projection_fields_are_the_servers_own(self) -> None:
        seeded = _swift_projection_keys()
        assert {"operation_id", "state", "cancel_requested", "client_detached", "tool"} <= (
            seeded
        )
        assert seeded <= _projection_keys()

    def test_the_seeded_receipt_carries_no_calendar_read(self) -> None:
        """The harness's one `query_result` is a calendar list, and its shape is
        the server's strict decoder's, not a shape written to suit the card."""
        top, event = _swift_calendar_query_keys()
        assert len(top) >= 5, f"the top-level slice found {sorted(top)}"
        assert len(event) >= 10, f"the per-event slice found {sorted(event)}"
        assert top <= _TOP_LEVEL_FIELDS
        assert event <= _EVENT_FIELDS

    def test_the_seeded_marker_carries_the_domains_field_name(self) -> None:
        """A marker seeded under a key the server does not write would render
        neutrally in the acceptance build and by domain in production."""
        assert _swift_marker_keys() == {"resolution", MANUAL_REVIEW_DOMAIN_FIELD}


class TestSeededCalendarQueryWouldSurviveTheServer:
    """Passing the decoder is the claim; the required set is derived, not read.

    "The seed's keys are a subset of the decoder's fields" is half a check. The
    other half is that the decoder would not *refuse* the seed for a missing
    field, and which fields it refuses for is a property of the decoder that
    changes underneath a copied list. So it is measured, on a sample that
    decodes.
    """

    #: A calendar query result in the server's own shape. Not the harness's --
    #: this sample exists to interrogate the decoder, and using the harness's
    #: seed for that would make the decoder's behaviour depend on the thing
    #: under test.
    _SAMPLE = {
        "status": "ok",
        "source_system": _SOURCE_SYSTEM,
        "record_count": 1,
        "next_cursor": None,
        "data_as_of": "2026-09-10T09:00:00+08:00",
        "mirror_stale": False,
        "events": [
            {
                "event_identifier": "EKA-PROBE-0001",
                "calendar_identifier": "CAL-PROBE",
                "calendar_title": "工作",
                "title": "探针",
                "start": "2026-10-01T00:00:00+09:00",
                "end": "2026-10-02T00:00:00+09:00",
                "all_day": True,
                "timezone": None,
                "start_date": "2026-10-01",
                "end_date": "2026-10-02",
                "date_anchor_unknown": False,
                "title_over_limit": False,
                "location_over_limit": False,
                "notes_over_limit": False,
                "location": None,
                "notes": None,
                "created_by_agent": True,
            }
        ],
    }

    @staticmethod
    def _required(sample: dict, fields: frozenset[str], event_level: bool) -> set[str]:
        def decodes(candidate: dict) -> bool:
            try:
                decode_calendar_query_projection(candidate)
            except CalendarQueryProjectionError:
                return False
            return True

        assert decodes(sample), "the probe sample itself does not decode"
        required = set()
        for field in sorted(fields):
            probe = {k: v for k, v in sample.items() if k != field}
            if event_level:
                probe["events"] = [
                    {k: v for k, v in sample["events"][0].items() if k != field}
                ]
            if not decodes(probe):
                required.add(field)
        return required

    def test_the_seed_satisfies_every_field_the_decoder_requires(self) -> None:
        required_top = self._required(self._SAMPLE, _TOP_LEVEL_FIELDS, event_level=False)
        required_event = self._required(self._SAMPLE, _EVENT_FIELDS, event_level=True)
        # Both derivations have to find something, or "the seed satisfies them"
        # is satisfied by the empty set.
        assert {"status", "events", "data_as_of", "mirror_stale"} <= required_top
        assert {"event_identifier", "calendar_identifier", "all_day"} <= required_event

        top, event = _swift_calendar_query_keys()
        assert required_top <= top, (
            "the acceptance build's calendar list would be rejected by the server's "
            f"own decoder for missing {sorted(required_top - top)}"
        )
        assert required_event <= event

    def test_the_source_system_the_harness_names_is_the_servers(self) -> None:
        assert _swift_constant(CHATWIRE_CARD_SWIFT, "mirrorSourceSystem") == _SOURCE_SYSTEM


class TestTheHarnessNamesTheServersConstants:
    """The button and the card are selected by values, and a renamed value
    selects nothing -- silently, since a missing button looks like a card that
    simply has no button."""

    def test_the_device_tool_is_read_from_the_shipping_constant(self) -> None:
        match = re.search(
            r"public static let deviceTool = (\S+)", _scenario_source()
        )
        assert match, "AcceptanceScenario.deviceTool is gone; this test cannot see it"
        assert match.group(1).rstrip(";") == "OperationReceipt.calendarDeviceTool", (
            "deviceTool must be the shipping constant, not a second copy of the "
            "string: a copy would keep the harness green after the tool is renamed"
        )

    def test_that_constant_is_the_one_the_server_holds_out_the_override_for(
        self,
    ) -> None:
        assert _swift_constant(CHATWIRE_SWIFT, "calendarDeviceTool") == CALENDAR_DEVICE_TOOL

    def test_the_seeded_domain_is_the_one_the_server_derives(self) -> None:
        """`domain` is written server-side by the IR, so a seeded value the IR
        does not derive would put the harness's calendar card on an operation
        the server words as a ledger write."""
        assert _swift_constant(CHATWIRE_SWIFT, "calendarDomain") == domain_of_tool(
            CALENDAR_DEVICE_TOOL
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
