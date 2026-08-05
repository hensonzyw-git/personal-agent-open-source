"""The cross-language half of `DEV-040`'s manual-resolution contract.

`needs_manual_review` is terminal and the server records a person's report beside
it, never instead of it. Two strings carry that whole affordance across the
language boundary: the resolution vocabulary (`MANUAL_RESOLUTIONS`, which a DB
CHECK constraint also enforces) and the Timeline marker's event type.

Neither is covered by `chat_receipt_vectors.json` -- deliberately, because the
resolution endpoint is not a chat receipt and widening that contract for it was
rejected. So the drift has to be caught here instead, and the failure it is
catching is specific: if the server's vocabulary changes, the iOS buttons keep
sending the old value, every tap becomes a `400`, and the one escape from a
parked operation stops working with no test anywhere going red. The client can
only ever *refuse* with these strings, never grant, so being behind the server is
safe; being wrong about the spelling is not.

Reading the Swift source is the point rather than a shortcut: a second Python
copy of the same list would agree with itself forever.
"""

from __future__ import annotations

import re
from pathlib import Path

from personal_agent.api.manual_review import MANUAL_REVIEW_RESOLVED
from personal_agent.storage.models import MANUAL_RESOLUTIONS

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
