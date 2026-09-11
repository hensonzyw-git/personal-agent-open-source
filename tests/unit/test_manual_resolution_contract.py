"""The cross-language half of `DEV-040`'s manual-resolution contract.

`needs_manual_review` is terminal and the server records a person's report beside
it, never instead of it. Two strings carry that whole affordance across the
language boundary: the resolution vocabulary (`MANUAL_RESOLUTIONS`, which a DB
CHECK constraint also enforces) and the Timeline marker's event type.

Three things cross this boundary: the resolution vocabulary (`MANUAL_RESOLUTIONS`,
which a DB CHECK constraint also enforces), the Timeline marker's event type, and
the marker's destination field plus the domain value written into it.

None is covered by `chat_receipt_vectors.json` -- deliberately, because the
resolution endpoint is not a chat receipt and widening that contract for it was
rejected. So the drift has to be caught here instead, and the failure it is
catching is specific: if the server's vocabulary changes, the iOS buttons keep
sending the old value, every tap becomes a `400`, and the one escape from a
parked operation stops working with no test anywhere going red. The client can
only ever *refuse* with these strings, never grant, so being behind the server is
safe; being wrong about the spelling is not.

The domain field is the opposite kind of risk: nothing refuses, nothing errors,
and the symptom is a calendar resolution's permanent history line saying 账本 --
or, once the markers carry the field but the two sides disagree on its spelling,
saying nothing at all. The entry is frozen when it is appended, so there is no
later chance to notice.

Reading the Swift source is the point rather than a shortcut: a second Python
copy of the same list would agree with itself forever.
"""

from __future__ import annotations

import re
from pathlib import Path

from personal_agent.api.manual_review import (
    MANUAL_REVIEW_DOMAIN_FIELD,
    MANUAL_REVIEW_RESOLVED,
)
from personal_agent.storage.models import MANUAL_RESOLUTIONS
from personal_agent_core.tool_ir import TOOL_CONTRACTS, domain_of_tool

_IOS = Path(__file__).parents[2] / "ios/PersonalAgentKit/Sources/PersonalAgentKit"
_CHAT_WIRE = _IOS / "ChatWire.swift"


def _swift_source() -> str:
    assert _CHAT_WIRE.is_file(), (
        f"{_CHAT_WIRE} is gone; the iOS half of this contract moved and this "
        "test can no longer see it"
    )
    return _CHAT_WIRE.read_text(encoding="utf-8")


def test_the_resolution_vocabulary_is_exactly_these_two() -> None:
    """A third value is a deliberate product decision, not a refactor.

    It would need a new iOS case, new wording, and a new answer to "what does the
    card show?" -- so it fails here first.
    """
    assert MANUAL_RESOLUTIONS == ("confirmed_written", "confirmed_not_written")


def test_ios_sends_only_values_the_server_accepts() -> None:
    source = _swift_source()
    body = re.search(
        r"public enum ManualResolution: String.*?\n\}", source, re.DOTALL
    )
    assert body is not None, "ManualResolution is no longer declared in ChatWire.swift"
    swift_values = set(re.findall(r'case \w+ = "([^"]+)"', body.group(0)))

    assert swift_values, "ManualResolution declares no raw values"
    # Subset, not equality: a client that has not learned a newly added value can
    # only fail to offer it, which is safe. A client that offers a value the
    # server refuses is a dead button on the only escape from a parked write.
    assert swift_values <= set(MANUAL_RESOLUTIONS), (
        f"iOS would send {sorted(swift_values - set(MANUAL_RESOLUTIONS))}, which "
        f"the server refuses; accepted values are {sorted(MANUAL_RESOLUTIONS)}"
    )


def test_ios_reads_the_marker_event_type_the_server_writes() -> None:
    """The marker is how a resolved card stays resolved across a restart.

    If the two spellings diverge, the client renders the marker as an
    unrecognised event and offers the buttons again -- and the second tap either
    does nothing or, on a change of mind, is refused with `409` by a prompt the
    screen itself invited.
    """
    assert MANUAL_REVIEW_RESOLVED == "manual_review_resolved"
    assert f'case "{MANUAL_REVIEW_RESOLVED}":' in _swift_source()


def test_the_marker_is_decoded_under_the_key_the_server_writes() -> None:
    """The domain field's name, held to one spelling on both sides.

    Textual on purpose. There is no behaviour to test here -- Python writes a dict
    key and Swift reads the same key out of JSON -- and the failure is a rename on
    one side that the other reads as an absent field. The marker then renders
    neutrally, which is a legal state this client already handles, so nothing
    complains: a calendar resolution recorded today would read as
    "核对目标未记录" for as long as the entry is kept.
    """
    source = _swift_source()
    assert f'content["{MANUAL_REVIEW_DOMAIN_FIELD}"]' in source, (
        f"the server writes the marker's destination under "
        f"{MANUAL_REVIEW_DOMAIN_FIELD!r} but ChatWire.swift does not read that "
        "key; every marker would decode as domain-less"
    )


def test_the_calendar_domain_the_marker_carries_is_the_ir_one() -> None:
    """The one domain value neither side may spell differently.

    The server freezes `domain_of_tool(operation.tool)` -- the IR's own string --
    into the marker, and the client compares what it reads against its own
    constant. For every domain but one a mismatch lands on the same branch
    anyway, so only the calendar fork can tell the two apart, and it tells them
    apart by silently falling back to the neutral words. A calendar resolution
    whose marker says 未记录 is that drift, and there is no second clue in the
    event to recover from.

    The client constant is read from the Swift source rather than mirrored in
    Python: a second Python copy would agree with itself forever.
    """
    match = re.search(
        r'public static let calendarDomain = "([^"]+)"', _swift_source()
    )
    assert match is not None, (
        "OperationReceipt.calendarDomain is no longer declared in ChatWire.swift"
    )
    client_domain = match.group(1)

    calendar_tools = [c.name for c in TOOL_CONTRACTS if c.domain == client_domain]
    assert calendar_tools, (
        f"no tool in the IR has domain {client_domain!r}, so the client's "
        "calendar fork can never be selected by a marker the server writes"
    )
    # Non-vacuous in both directions: the calendar tools really derive it, and a
    # tool of another domain does not.
    assert {domain_of_tool(name) for name in calendar_tools} == {client_domain}
    assert domain_of_tool("finance.expense.create") != client_domain
    # An unknown tool has no domain at all -- not the calendar one, and not a
    # borrowed default.
    assert domain_of_tool("retired.old_tool") is None
