"""The ingest barrier: the channel's ratchet and one device's rebuild state.

Design 14.2's rollback needs the mirror to stop accepting uploads, be emptied
and be rebuilt from the phone -- and the danger it is defending against is a
batch captured *before* the rebuild arriving *after* it. The device is the fact
source and its upload loop is `where lastError == nil`, so nothing the server
says can recall a request already in flight; the only defensible position is
that an old window has **no writable entry point at any time, under any
combination of states** (design 14.2, R7). Three facts carry that, and two of
them are deliberately one-way.

`min_ingest_protocol` -- the lowest client protocol version the channel still
accepts -- is raised to 2 by a rebuild and is never lowered. The reason is not
caution, it is that the premise for safely re-opening version 1 ("confirm no v1
request is still in flight") has no executable proof: a silent period proves
neither that the network holds nothing nor that a request already inside the
server has ended, and a retry backoff bounds the *interval* between retries,
not the *lifetime* of a request (design 14.2, R6-F21). So the entry point does
not exist -- not built, not built-and-unused -- and `begin_rebuild` is the only
writer of the column. `leave_maintenance` touches the switch and not the
ratchet; that asymmetry is the whole design of this module.

`rebuild_pending` is presentation only. It drives the 「正在重建 / 未同步」
line the query renders, and it is cleared by a completed window, because at
that point the mirror really has been rebuilt from the new client's own
snapshot. It refuses v1 while it is set, but that refusal is **redundant**: the
ratchet carries it afterwards and `complete_rebuild` does not move the ratchet.
Two independent states, per the R7 revision -- the earlier design that had a
successful v2 batch re-open v1 was refuted, because an epoch proves the batch
that carries it and cannot vouch for a request that does not.

`rebuild_instant` is not a predicate at all. It is written so a batch captured
at or before the rebuild can be logged as "an old window is still arriving",
which is how an operator notices a controlled recovery that did not take. It
must never decide anything: with zero clock skew -- the case a tolerance is
supposed to be safe for -- `12:00 > 12:01 - 5min` holds and the entire
pre-rebuild window is admitted (design 14.2, R6-F20). No tolerance both admits
the new window and refuses the old one; the choice is structural, not a
parameter.

The mirror is a cache, so none of this loses data: what a rebuild discards is
a copy of what the phone still holds, and the phone rebuilds it by completing
one window. What the ratchet costs is an old client's ability to upload ever
again -- the honest degradation the design names (`mirror_stale` stays true and
the query says to upgrade), not a silent divergence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import delete

from personal_agent_core.errors import AppError, ErrorCode
from personal_data_mcp.storage.models import CalendarDeviceSync, CalendarEvent, CalendarIngestPolicy, CalendarDirectory


#: The single policy row's primary key. The migration writes it and nothing
#: deletes it, so there is exactly one value this can be.
POLICY_ID: Final[int] = 1

PROTOCOL_BEFORE_REBUILD: Final[int] = 1
#: What a rebuild raises the ratchet to, and the highest version the wire
#: currently defines. A client that declares 2 or more is past the barrier.
PROTOCOL_AFTER_REBUILD: Final[int] = 3

MODE_NORMAL: Final[str] = "normal"
MODE_MAINTENANCE: Final[str] = "maintenance"

#: The watermark a rebuilt device carries, and the reason it is a sentinel
#: rather than a deleted row.
#:
#: The design says the rebuild empties `calendar_device_sync`, which reads as
#: "the watermark becomes None" -- but `watermark_ts` is NOT NULL, and the row
#: is where `rebuild_pending` lives, so deleting it would delete the state step
#: (c) of the same runbook just set, and the 「正在重建」 prompt would have
#: nothing to render from. Epoch 0 keeps both true at once: it is earlier than
#: any snapshot a device can honestly report, so the row is not a late-packet
#: watermark (`ingest.is_late_packet` compares strictly), a complete window
#: sweeps against it (`snapshot_ts > 0`), `device_watermark` reads 1970, and
#: freshness takes the newest across devices -- so a rebuilt device contributes
#: nothing and the mirror reads as honestly stale until it is rebuilt.
NO_WATERMARK_TS: Final[int] = 0


def read_policy(session) -> CalendarIngestPolicy:
    """The one policy row, or a refusal.

    The migration inserts this row, so it is part of the schema and "no row" is
    an implementation bug rather than a state. It raises instead of returning
    defaults because the defaulted shape is the one that fails open: a missing
    row would read as `min_ingest_protocol=1`, and an old client's late packets
    would write into a mirror that was rebuilt without them. Failing closed
    costs an outage; failing open costs correctness, silently.
    """
    row = session.get(CalendarIngestPolicy, POLICY_ID)
    if row is None:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail=(
                f"calendar_ingest_policy has no row {POLICY_ID}; the ingest "
                "channel's floor is unknown, so no calendar upload is accepted"
            ),
        )
    return row


def check_ingest_allowed(
    policy: CalendarIngestPolicy,
    *,
    rebuild_pending: bool,
    client_wire_version: int,
    sync_epoch: int | None,
    expected_sync_epoch: int,
) -> None:
    """Refuse this upload if the barrier says so, before anything is written.

    Ordered so the caller's reason is the most specific true one: maintenance
    is the operator's deliberate state and a client should hear that before it
    hears about its own version; the ratchet is what permanently closes the v1
    channel; `rebuild_pending` is the redundant line that only matters while
    the ratchet is somehow still low.

    Every refusal is `SOURCE_UNAVAILABLE`, per design 14.2. Two things about
    that code are worth stating rather than leaving for a reader to discover:
    its outward message is written for the ledger (`账本暂时不可用`), which is
    the wrong noun for a calendar upload; and it is the one code in
    `RETRYABLE_CODES`, which fits maintenance (the channel comes back) and does
    not fit the ratchet (no retry will ever help an old client). Neither is
    this module's to change, and the device acts on neither string nor flag
    today -- but the pair is recorded in the delivery notes as an open item
    rather than silently depended on.
    """
    if policy.ingest_mode == MODE_MAINTENANCE:
        raise AppError(
            ErrorCode.SOURCE_UNAVAILABLE,
            internal_detail="calendar ingest is in maintenance; nothing is written",
        )
    if client_wire_version < policy.min_ingest_protocol:
        # The permanent closure. `min_ingest_protocol` reached 2 because the
        # mirror was rebuilt, and no event lowers it again, so this is the
        # answer an old client gets for the rest of its life. The device is
        # supposed to read this as "upgrade the App"; that instruction is
        # carried by the App, not by a message the server may compose.
        raise AppError(
            ErrorCode.SOURCE_UNAVAILABLE,
            internal_detail=(
                f"calendar ingest refuses client protocol "
                f"{client_wire_version}; the mirror was rebuilt and the channel "
                f"now requires {policy.min_ingest_protocol}"
            ),
        )
    if rebuild_pending and client_wire_version < PROTOCOL_AFTER_REBUILD:
        # Redundant while the ratchet is up, and kept anyway: this is the line
        # that holds if the ratchet is ever found low (a restored snapshot, a
        # hand-edited row), and the cost of holding it is one boolean.
        raise AppError(
            ErrorCode.SOURCE_UNAVAILABLE,
            internal_detail=(
                "calendar ingest refuses a pre-rebuild client shape while this "
                "device's mirror is mid-rebuild"
            ),
        )
    if client_wire_version >= PROTOCOL_AFTER_REBUILD and sync_epoch != expected_sync_epoch:
        raise AppError(
            ErrorCode.CALENDAR_SYNC_RESET_REQUIRED,
            internal_detail="calendar upload epoch is stale or absent; capture a fresh window",
        )


def enter_maintenance(session, *, now: datetime) -> None:
    """Step (a) of the controlled recovery: stop accepting calendar uploads.

    Called on its own, before anything is deleted, because the runbook needs a
    window in which the mirror can be inspected and reset without a batch
    landing in the middle of it.
    """
    policy = read_policy(session)
    policy.ingest_mode = MODE_MAINTENANCE
    policy.updated_at = now


def leave_maintenance(session, *, now: datetime) -> None:
    """Step (d): re-open the channel.

    This is the only operation that re-opens anything, and it re-opens exactly
    one thing. It does **not** lower the ratchet -- that is not an oversight to
    be corrected later, it is the design (design 14.2, R7-F22): after a rebuild
    the channel is open to the new protocol and closed to the old one forever.
    A test asserts both halves together, because either one alone is the bug.
    """
    policy = read_policy(session)
    policy.ingest_mode = MODE_NORMAL
    policy.updated_at = now


def begin_rebuild(session, *, device_id: str, now: datetime) -> None:
    """Step (c): arm the barrier and empty this device's mirror state.

    All of it in one call, deliberately. The states it sets are the defense,
    and a half-armed barrier is the dangerous shape -- a raised ratchet with
    maintenance still off, or an emptied watermark with `rebuild_pending`
    unset -- so there is no way to perform part of it through this module.

    The `sync_epoch` column is untouched: v2's epoch comparison is the other
    half of the design's barrier and is **not implemented**, because nothing
    on the wire defines how a device learns or states an epoch (recorded as an
    open contract gap). Writing a random value here would look like the check
    exists.
    """
    policy = read_policy(session)
    policy.min_ingest_protocol = PROTOCOL_AFTER_REBUILD
    policy.ingest_mode = MODE_MAINTENANCE
    policy.updated_at = now

    row = session.get(CalendarDeviceSync, device_id)
    if row is None:
        # A device that has enrolled but never completed a snapshot has no row
        # yet, and it still has to read as mid-rebuild -- otherwise the first
        # window it completes after the rebuild looks like a first-ever sync
        # and nobody is told the mirror was rebuilt under it.
        row = CalendarDeviceSync(
            device_id=device_id,
            watermark_ts=NO_WATERMARK_TS,
            window_start_ts=None,
            window_end_ts=None,
            sync_epoch=2,
            updated_at=now,
        )
        session.add(row)
    else:
        row.watermark_ts = NO_WATERMARK_TS
        # Coverage goes with the watermark: a watermark's bounds say which
        # window it vouched for, and this one vouches for nothing. Leaving
        # them would let a query keep reading the pre-rebuild window as
        # covered, which is the one claim the rebuild made false.
        row.window_start_ts = None
        row.window_end_ts = None
        row.updated_at = now
        row.sync_epoch += 1
    # This project has one iPhone calendar fact source and one mirror. Query
    # reads event rows directly, so logical reset through watermarks would
    # leave stale appointments visible; rebuild physically empties the cache.
    session.execute(delete(CalendarEvent))
    session.execute(delete(CalendarDirectory))
    row.rebuild_pending = True
    row.rebuild_instant = int(now.timestamp())


def complete_rebuild(session, *, device_id: str) -> None:
    """Clear the presentation flag once the device has rebuilt its mirror.

    Called when a completed window writes a watermark -- at that point the
    mirror really does hold a whole snapshot again, so 「正在重建」 is no
    longer true. It clears **only** the flag: `min_ingest_protocol` is not
    this function's to move, and neither is `rebuild_instant`, which stays as
    the watermark for "was this device's recovery followed by an old window
    arriving".

    `updated_at` is left alone as well. It records when the device last
    completed a snapshot, which the caller has just written; stamping it again
    here would make one fact have two writers.
    """
    row = session.get(CalendarDeviceSync, device_id)
    if row is None:
        # Reached only if a completed window was recorded without creating the
        # row it records, which the ingest cannot do. Raising beats a silent
        # pass: the alternative reading -- "nothing to clear" -- is exactly the
        # state a real bug would produce.
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail=(
                f"cannot complete a rebuild for {device_id!r}: it has no "
                "calendar sync row"
            ),
        )
    row.rebuild_pending = False
