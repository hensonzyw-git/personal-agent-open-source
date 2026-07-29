"""Session Manager: which semantic segment a new message belongs to.

Cross-cutting design §6. The user sees one continuous Timeline; the server
quietly groups it into Sessions so a long topic can be compacted and a new topic
does not inherit an unrelated context.

The decision order in §6.1 is fixed, and the ordering is the safety property:

1. resolve the canonical Timeline (the caller has already done this);
2. read the Timeline's current open Session;
3. look for a non-terminal operation belonging to this Timeline;
4. **a non-terminal operation pins its Session** -- an accepted/in-flight write
   or a parked clarification/duplicate decision must never cross a segment;
5. an explicit user reset / resume / correction outranks any classifier;
6. a previous Session that is already closed forces a new one;
7. only when still undecided, consider idle time and semantic continuity;
8. emit a structured decision;
9. **anything uncertain continues the current Session.** Low confidence, a
   malformed classifier answer, a timeout and an unavailable provider are one
   behaviour, because splitting a topic in half on a bad guess costs the user
   real context while merging two topics costs only some irrelevance.

A Session decision is not an authorisation decision (§6.1). Nothing here can
change a device scope, a tool allowlist or an operation state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal, Protocol

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from personal_agent.context.config import ContextConfig
from personal_agent.storage.models import (
    SESSION_BOUNDARY_REASONS,
    TERMINAL_OPERATION_STATES,
    ContextSession,
)
from personal_agent_core.errors import AppError, ErrorCode


CLASSIFIER_VERSION: Final[str] = "session-boundary-v1"

CONFIDENCE_BANDS: Final[frozenset[str]] = frozenset({"high", "medium", "low"})
TASK_STATES: Final[frozenset[str]] = frozenset(
    {"active", "completed", "blocked", "unknown"}
)
MAX_TOPIC_SUMMARY_CHARS: Final[int] = 512
MAX_DOMAIN_CHARS: Final[int] = 64
_TERMINAL_OPERATION_SQL: Final[str] = ", ".join(
    f"'{state}'" for state in sorted(TERMINAL_OPERATION_STATES)
)

#: The explicit signals a user can give in words. Deliberately a closed literal
#: set rather than a model judgement: design §6.1 makes a correction outrank the
#: classifier, and a signal that is itself classified could not outrank it.
#: Anything outside this set is not treated as explicit and falls through to the
#: ordinary decision -- under-detecting merely means the classifier decides,
#: while over-detecting would let an ordinary sentence silently move a topic.
RESET_MARKERS: Final[tuple[str, ...]] = (
    "换个话题",
    "新话题",
    "开个新话题",
    "重新开始",
    "说点别的",
)
RESUME_MARKERS: Final[tuple[str, ...]] = (
    "继续上次的话题",
    "接着上次的话题",
    "回到上一个话题",
    "继续刚才的话题",
)
CORRECTION_MARKERS: Final[tuple[str, ...]] = (
    "这是同一个话题",
    "还是同一个话题",
    "这跟上面是同一件事",
    "不是新话题",
)

ExplicitSignal = Literal["reset", "resume", "correction"] | None


def detect_explicit_signal(user_text: str) -> ExplicitSignal:
    """The user's own words about topic structure, or `None`.

    Correction is checked first: "这是同一个话题" is a statement about a
    boundary that already happened, and it must win over a phrase that merely
    looks like a resume.
    """
    if not isinstance(user_text, str):
        return None
    text_value = user_text.strip()
    if any(marker in text_value for marker in CORRECTION_MARKERS):
        return "correction"
    if any(marker in text_value for marker in RESUME_MARKERS):
        return "resume"
    if any(marker in text_value for marker in RESET_MARKERS):
        return "reset"
    return None


@dataclass(frozen=True)
class CompactSessionState:
    """A bounded, de-identified account of the open Session.

    The provider derives this from trusted Context Builder/Checkpoint state,
    never from a classifier's own output. Explicit fields make it impossible to
    mistake boundary metadata for the topic/domain/task evidence §6.1 requires.
    """

    topic_summary: str
    domain: str | None
    task_state: Literal["active", "completed", "blocked", "unknown"]

    def __post_init__(self) -> None:
        summary = self.topic_summary.strip()
        if not summary or len(summary) > MAX_TOPIC_SUMMARY_CHARS:
            raise ValueError("topic_summary must be non-empty and bounded")
        if self.domain is not None:
            domain = self.domain.strip()
            if not domain or len(domain) > MAX_DOMAIN_CHARS:
                raise ValueError("domain must be non-empty and bounded")
        if self.task_state not in TASK_STATES:
            raise ValueError("task_state is not recognised")


@dataclass(frozen=True)
class ClassifierInput:
    """What the semantic classifier is allowed to see (§6.1).

    Not in here, on purpose: tools, credentials, the full archive, and any
    write capability. The classifier answers one closed question about the new
    input and one bounded, de-identified Session description.
    """

    user_text: str
    open_session_state: CompactSessionState
    minutes_since_last_event: int | None


@dataclass(frozen=True)
class ClassifierOutcome:
    """The classifier's only legal answer shape."""

    decision: Literal["continue_session", "open_new_session"]
    reason: str
    confidence_band: str


@dataclass(frozen=True)
class PreparedClassification:
    """The database-bound half of one optional semantic boundary decision.

    The API prepares this value in a short transaction, ends that transaction,
    and only then calls the non-deterministic classifier.  The observed Session
    identity and event clock make a returned answer unusable if another request
    changed the open segment while the model was running.
    """

    expected_session_id: str | None
    expected_last_event_at: datetime | None
    expected_timeline_sequence: int
    request: ClassifierInput | None


@dataclass(frozen=True)
class ResolvedClassification:
    """A classifier answer obtained with no database transaction held."""

    expected_session_id: str | None
    expected_last_event_at: datetime | None
    expected_timeline_sequence: int
    outcome: ClassifierOutcome | None


class BoundaryClassifier(Protocol):
    """One bounded semantic judgement. May raise; may answer badly."""

    def classify(self, request: ClassifierInput) -> Any: ...


class CompactSessionStateProvider(Protocol):
    """Trusted semantic state assembled outside the model boundary."""

    def compact_state(
        self, db, *, session: ContextSession
    ) -> CompactSessionState: ...


@dataclass(frozen=True)
class SessionDecision:
    """The Boundary Record of design §6.2, written with the event."""

    decision: Literal["continue_session", "open_new_session"]
    session_id: str
    reason: str | None
    previous_session_id: str | None
    parent_session_id: str | None
    relation_kind: str
    classifier_version: str | None
    confidence_band: str | None
    #: True when a new Session was created.
    opened: bool

    @property
    def is_boundary(self) -> bool:
        """Whether a divider event belongs in the Timeline.

        A Timeline's *first* Session divides nothing: it has no predecessor and
        no boundary reason, and drawing a divider above the first message would
        show the user a split that never happened. Every other new Session is a
        real boundary between two segments.
        """
        return self.opened and (
            self.reason is not None or self.previous_session_id is not None
        )

    def audit_record(self) -> dict[str, Any]:
        """Enumerations, versions and nothing else (§6.2).

        Identifiers are left to the caller to fingerprint; no user text ever
        appears here.
        """
        return {
            "decision": self.decision,
            "reason": self.reason,
            "relation_kind": self.relation_kind,
            "classifier_version": self.classifier_version,
            "confidence_band": self.confidence_band,
        }


class SessionManager:
    """Chooses, opens and closes Sessions for one canonical Timeline."""

    def __init__(
        self,
        config: ContextConfig,
        *,
        classifier: BoundaryClassifier | None = None,
        state_provider: CompactSessionStateProvider | None = None,
    ) -> None:
        self._config = config
        self._classifier = classifier
        self._state_provider = state_provider

    # -- reads ---------------------------------------------------------

    def open_session(self, db, *, conversation_id: str) -> ContextSession | None:
        return (
            db.query(ContextSession)
            .filter(
                ContextSession.conversation_id == conversation_id,
                ContextSession.status == "open",
            )
            .one_or_none()
        )

    def non_terminal_session_id(self, db, *, conversation_id: str) -> str | None:
        """The Session holding an operation whose state may still change.

        Read from the events, not from the operations table alone: an operation
        is only part of this Timeline through the event that anchored it, and
        that is the row that knows which segment it belongs to.
        """
        return db.execute(
            text(
                "SELECT e.session_id FROM conversation_events AS e "
                "JOIN operations AS o ON o.operation_id = e.operation_id "
                "WHERE e.conversation_id = :cid "
                f"AND o.state NOT IN ({_TERMINAL_OPERATION_SQL}) "
                "ORDER BY e.timeline_sequence DESC LIMIT 1"
            ),
            {"cid": conversation_id},
        ).scalar_one_or_none()

    # -- the decision --------------------------------------------------

    def select_session(
        self,
        db,
        *,
        conversation_id: str,
        user_text: str,
        now: datetime,
        pinned_session_id: str | None = None,
        resolved_classification: ResolvedClassification | None = None,
    ) -> SessionDecision:
        """Run the fixed §6.1 order and return a structured decision."""
        current = self.open_session(db, conversation_id=conversation_id)

        # Steps 3-4. A non-terminal operation pins its Session outright. This
        # is checked before every heuristic, including the user's own words:
        # an in-flight result must land beside its request, and a parked
        # duplicate decision answered in a fresh Session would lose its
        # candidate set.
        pinned = pinned_session_id or self.non_terminal_session_id(
            db, conversation_id=conversation_id
        )
        if pinned is not None:
            return self._continue(pinned, reason=None)

        signal = detect_explicit_signal(user_text)

        # Step 5. Explicit user intent, ahead of any classifier.
        if signal == "correction":
            return self._correct_boundary(
                db, conversation_id=conversation_id, current=current, now=now
            )
        if signal == "resume":
            return self._resume(
                db, conversation_id=conversation_id, current=current, now=now
            )
        if signal == "reset":
            return self._open_new(
                db,
                conversation_id=conversation_id,
                previous=current,
                reason="explicit_reset",
                relation_kind="new_topic",
                parent=None,
                now=now,
                classifier_version=None,
                confidence_band=None,
            )

        # Step 6. No open Session at all: the previous one is closed, or this
        # is the Timeline's first message.
        if current is None:
            return self._open_new(
                db,
                conversation_id=conversation_id,
                previous=None,
                reason=self._first_or_previous_closed(db, conversation_id),
                relation_kind="new_topic",
                parent=None,
                now=now,
                classifier_version=None,
                confidence_band=None,
            )

        # Step 7. Idle time, domain, task completion and semantic continuity
        # are inputs to *one* judgement, not separate triggers. A long silence
        # alone is deliberately not a boundary: the reason code §6.2 defines is
        # `idle_and_unrelated`, and only the classifier can supply the second
        # half of that. Someone who steps away for two hours and comes back to
        # the same task keeps their context.
        idle_minutes = self._idle_minutes(current, now)
        if resolved_classification is None:
            # Compatibility path for standalone callers. The production API
            # always supplies a resolved value so no model call occurs while its
            # write transaction is open.
            outcome = self._classify(
                db, current, user_text=user_text, idle_minutes=idle_minutes
            )
        else:
            outcome = self._outcome_for_current(
                db,
                conversation_id,
                current,
                resolved_classification,
            )
        if outcome is None or outcome.decision == "continue_session":
            # Step 9 lives here too: `None` is every uncertain case.
            return self._continue(
                current.session_id,
                reason=None,
                classifier_version=(
                    CLASSIFIER_VERSION if outcome is not None else None
                ),
                confidence_band=outcome.confidence_band if outcome else None,
            )
        return self._open_new(
            db,
            conversation_id=conversation_id,
            previous=current,
            reason=outcome.reason,
            relation_kind="new_topic",
            parent=None,
            now=now,
            classifier_version=CLASSIFIER_VERSION,
            confidence_band=outcome.confidence_band,
        )

    def prepare_classification(
        self,
        db,
        *,
        conversation_id: str,
        user_text: str,
        now: datetime,
        pinned_session_id: str | None = None,
    ) -> PreparedClassification:
        """Read the bounded classifier input without calling the classifier.

        This deliberately mirrors the gates before step 7. Returning a prepared
        value even when no call is possible is important: the fresh write phase
        must fail closed rather than discover a newly available Checkpoint and
        make a remote call from inside that transaction.
        """

        current = self.open_session(db, conversation_id=conversation_id)
        expected_session_id = current.session_id if current is not None else None
        expected_last_event_at = (
            current.last_event_at if current is not None else None
        )
        expected_timeline_sequence = db.execute(
            text(
                "SELECT next_sequence FROM conversations "
                "WHERE conversation_id = :cid"
            ),
            {"cid": conversation_id},
        ).scalar_one()
        pinned = pinned_session_id or self.non_terminal_session_id(
            db, conversation_id=conversation_id
        )
        signal = detect_explicit_signal(user_text)
        if (
            pinned is not None
            or signal is not None
            or current is None
            or self._classifier is None
            or self._state_provider is None
        ):
            return PreparedClassification(
                expected_session_id,
                expected_last_event_at,
                expected_timeline_sequence,
                None,
            )
        try:
            state = self._state_provider.compact_state(db, session=current)
        except Exception:  # noqa: BLE001 - unavailable state means no split
            state = None
        request = (
            ClassifierInput(
                user_text=user_text,
                open_session_state=state,
                minutes_since_last_event=self._idle_minutes(current, now),
            )
            if isinstance(state, CompactSessionState)
            else None
        )
        return PreparedClassification(
            expected_session_id,
            expected_last_event_at,
            expected_timeline_sequence,
            request,
        )

    def resolve_classification(
        self, prepared: PreparedClassification
    ) -> ResolvedClassification:
        """Call the model half after the caller has ended its DB transaction."""

        outcome = None
        if prepared.request is not None and self._classifier is not None:
            try:
                outcome = parse_classifier_outcome(
                    self._classifier.classify(prepared.request)
                )
            except Exception:  # noqa: BLE001 - any provider failure continues
                outcome = None
        return ResolvedClassification(
            prepared.expected_session_id,
            prepared.expected_last_event_at,
            prepared.expected_timeline_sequence,
            outcome,
        )

    @staticmethod
    def _outcome_for_current(
        db,
        conversation_id: str,
        current: ContextSession,
        resolved: ResolvedClassification,
    ) -> ClassifierOutcome | None:
        """Use a model answer only for the exact Session snapshot it saw."""

        current_sequence = db.execute(
            text(
                "SELECT next_sequence FROM conversations "
                "WHERE conversation_id = :cid"
            ),
            {"cid": conversation_id},
        ).scalar_one()
        if (
            current.session_id != resolved.expected_session_id
            or current.last_event_at != resolved.expected_last_event_at
            or current_sequence != resolved.expected_timeline_sequence
        ):
            return None
        return resolved.outcome

    # -- branches ------------------------------------------------------

    def _continue(
        self,
        session_id: str,
        *,
        reason: str | None,
        classifier_version: str | None = None,
        confidence_band: str | None = None,
    ) -> SessionDecision:
        return SessionDecision(
            decision="continue_session",
            session_id=session_id,
            reason=reason,
            previous_session_id=None,
            parent_session_id=None,
            relation_kind="new_topic",
            classifier_version=classifier_version,
            confidence_band=confidence_band,
            opened=False,
        )

    def _first_or_previous_closed(self, db, conversation_id: str) -> str | None:
        existed = db.execute(
            text(
                "SELECT 1 FROM context_sessions WHERE conversation_id = :cid "
                "LIMIT 1"
            ),
            {"cid": conversation_id},
        ).scalar_one_or_none()
        # The Timeline's very first Session is not the outcome of a boundary
        # decision, so it records no reason rather than a plausible-looking one.
        return "previous_closed" if existed else None

    def _resume(
        self,
        db,
        *,
        conversation_id: str,
        current: ContextSession | None,
        now: datetime,
    ) -> SessionDecision:
        """Continue an earlier topic by opening a Session that points at it."""
        candidates = self._resume_candidates(db, conversation_id, current)
        if not candidates:
            # Nothing to resume: the honest outcome is to carry on rather than
            # invent a lineage.
            if current is not None:
                return self._continue(current.session_id, reason=None)
            return self._open_new(
                db,
                conversation_id=conversation_id,
                previous=None,
                reason=None,
                relation_kind="new_topic",
                parent=None,
                now=now,
                classifier_version=None,
                confidence_band=None,
            )
        return self._open_new(
            db,
            conversation_id=conversation_id,
            previous=current,
            reason="explicit_resume",
            relation_kind="resumes",
            parent=candidates[0],
            now=now,
            classifier_version=None,
            confidence_band=None,
        )

    def _resume_candidates(
        self, db, conversation_id: str, current: ContextSession | None
    ) -> list[str]:
        """The Session a bare resume marker names: the most recent closed one.

        Every phrase in `RESUME_MARKERS` names *the last* topic ("继续上次的
        话题"), so the target is singular by construction and asking about it
        would be asking a question the user already answered. The ambiguity
        §6.1 requires a clarification for is the other case -- a user naming a
        topic ("继续东京那个话题") -- which needs a resolver this version does
        not have, and which the marker set deliberately does not match. That
        wording therefore falls through to the ordinary decision rather than
        being guessed at.
        """
        row = db.execute(
            text(
                "SELECT session_id FROM context_sessions "
                "WHERE conversation_id = :cid AND status = 'closed' "
                "AND relation_kind <> 'legacy' "
                "ORDER BY closed_at DESC, session_id DESC LIMIT 1"
            ),
            {"cid": conversation_id},
        ).scalar_one_or_none()
        if row is None or (current is not None and row == current.session_id):
            return []
        return [row]

    def _correct_boundary(
        self,
        db,
        *,
        conversation_id: str,
        current: ContextSession | None,
        now: datetime,
    ) -> SessionDecision:
        """"This is the same topic": link forward without rewriting history.

        The earlier divider stays in the Timeline as a historical fact. Design
        §6.1 is explicit that a correction must not reorder or rewrite existing
        events; it adds a relation, and the Context Builder follows it.
        """
        parent = self._previous_closed_session(db, conversation_id, current)
        if parent is None:
            if current is not None:
                return self._continue(current.session_id, reason=None)
            return self._open_new(
                db,
                conversation_id=conversation_id,
                previous=None,
                reason=None,
                relation_kind="new_topic",
                parent=None,
                now=now,
                classifier_version=None,
                confidence_band=None,
            )
        return self._open_new(
            db,
            conversation_id=conversation_id,
            previous=current,
            reason="explicit_correction",
            relation_kind="corrects_boundary",
            parent=parent,
            now=now,
            classifier_version=None,
            confidence_band=None,
        )

    def _previous_closed_session(
        self, db, conversation_id: str, current: ContextSession | None
    ) -> str | None:
        row = db.execute(
            text(
                "SELECT session_id FROM context_sessions "
                "WHERE conversation_id = :cid AND status = 'closed' "
                "AND relation_kind <> 'legacy' "
                "ORDER BY closed_at DESC, session_id DESC LIMIT 1"
            ),
            {"cid": conversation_id},
        ).scalar_one_or_none()
        if row is not None and current is not None and row == current.session_id:
            return None
        return row

    def _open_new(
        self,
        db,
        *,
        conversation_id: str,
        previous: ContextSession | None,
        reason: str | None,
        relation_kind: str,
        parent: str | None,
        now: datetime,
        classifier_version: str | None,
        confidence_band: str | None,
    ) -> SessionDecision:
        if reason is not None and reason not in SESSION_BOUNDARY_REASONS:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail=f"unknown boundary reason {reason!r}",
            )
        if parent is not None:
            self._require_valid_parent(
                db, conversation_id=conversation_id, parent_id=parent
            )
        opened = ContextSession(
            session_id=f"ses_{uuid.uuid4().hex}",
            conversation_id=conversation_id,
            status="open",
            boundary_reason=reason,
            parent_session_id=parent,
            relation_kind=relation_kind,
            classifier_version=classifier_version,
            opened_at=now,
            last_event_at=None,
        )
        try:
            # Only the competing boundary mutation belongs in this savepoint.
            # The caller already has an operation and sealed request in the
            # outer transaction; losing this race must not roll those back.
            with db.begin_nested():
                if previous is not None:
                    self.close_session(db, previous, now=now)
                db.add(opened)
                db.flush()
        except IntegrityError as exc:
            # Two concurrent messages both decided to open a Session. The
            # partial unique index -- not the read above -- is what makes only
            # one of them win, and the loser continues in the winner's Session.
            if (
                "UNIQUE constraint failed: context_sessions.conversation_id"
                not in str(exc.orig)
            ):
                raise
            winner = self.open_session(db, conversation_id=conversation_id)
            if winner is None:  # pragma: no cover - only reachable on a race
                raise AppError(
                    ErrorCode.CONTEXT_UNAVAILABLE,
                    internal_detail="no open session after a boundary race",
                ) from exc
            return self._continue(winner.session_id, reason=None)
        return SessionDecision(
            decision="open_new_session",
            session_id=opened.session_id,
            reason=reason,
            previous_session_id=previous.session_id if previous else None,
            parent_session_id=parent,
            relation_kind=relation_kind,
            classifier_version=classifier_version,
            confidence_band=confidence_band,
            opened=True,
        )

    def _require_valid_parent(
        self, db, *, conversation_id: str, parent_id: str
    ) -> None:
        """A lineage target must exist, share this Timeline and be acyclic."""
        parent = db.get(ContextSession, parent_id)
        if parent is None or parent.conversation_id != conversation_id:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail=(
                    "a session may only be linked to one in the same Timeline"
                ),
            )
        # Walk up from the target. A new Session cannot yet be anyone's parent,
        # so a cycle can only exist if the stored chain already contains one --
        # which a bounded walk detects without trusting the depth limit.
        seen = {parent_id}
        cursor = parent.parent_session_id
        depth = 0
        while cursor is not None:
            depth += 1
            if cursor in seen or depth > self._config.max_session_lineage_depth * 4:
                raise AppError(
                    ErrorCode.INVALID_ARGUMENT,
                    internal_detail="session lineage contains a cycle",
                )
            seen.add(cursor)
            node = db.get(ContextSession, cursor)
            if node is None or node.conversation_id != conversation_id:
                raise AppError(
                    ErrorCode.INVALID_ARGUMENT,
                    internal_detail="session lineage leaves this Timeline",
                )
            cursor = node.parent_session_id

    def close_session(self, db, session: ContextSession, *, now: datetime) -> None:
        """Close a Session only after every operation in it is terminal."""
        pending = db.execute(
            text(
                "SELECT 1 FROM conversation_events AS e "
                "JOIN operations AS o ON o.operation_id = e.operation_id "
                "WHERE e.session_id = :sid "
                f"AND o.state NOT IN ({_TERMINAL_OPERATION_SQL}) "
                "LIMIT 1"
            ),
            {"sid": session.session_id},
        ).scalar_one_or_none()
        if pending:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail=(
                    "a session with a non-terminal operation must not be closed"
                ),
            )
        session.status = "closed"
        session.closed_at = now
        db.flush()

    # -- classifier ----------------------------------------------------

    def _idle_minutes(
        self, current: ContextSession, now: datetime
    ) -> int | None:
        last = current.last_event_at or current.opened_at
        if last is None:  # pragma: no cover - opened_at is never null
            return None
        return max(0, int((now - last) / timedelta(minutes=1)))

    def _classify(
        self,
        db,
        current: ContextSession,
        *,
        user_text: str,
        idle_minutes: int | None,
    ) -> ClassifierOutcome | None:
        """Ask the classifier, and treat every deviation as "keep going".

        A missing classifier, an exception, a wrong shape, an unknown reason, a
        low confidence band -- all one answer. That is step 9, and it is the
        reason a provider outage cannot fragment a conversation.
        """
        if self._classifier is None or self._state_provider is None:
            return None
        try:
            state = self._state_provider.compact_state(db, session=current)
            if not isinstance(state, CompactSessionState):
                return None
            raw = self._classifier.classify(
                ClassifierInput(
                    user_text=user_text,
                    open_session_state=state,
                    minutes_since_last_event=idle_minutes,
                )
            )
        except Exception:  # noqa: BLE001 - any provider failure continues
            return None
        return parse_classifier_outcome(raw)


def parse_classifier_outcome(raw: Any) -> ClassifierOutcome | None:
    """Accept only the closed schema; anything else means "continue".

    Free text, extra fields, several results, an unknown reason and a low
    confidence band are all rejected here rather than being repaired. A
    repaired answer would be the model deciding the boundary through a shape the
    contract does not allow.
    """
    if isinstance(raw, ClassifierOutcome):
        candidate: Any = {
            "decision": raw.decision,
            "reason": raw.reason,
            "confidence_band": raw.confidence_band,
        }
    else:
        candidate = raw
    if not isinstance(candidate, dict):
        return None
    if set(candidate) != {"decision", "reason", "confidence_band"}:
        return None
    decision = candidate["decision"]
    reason = candidate["reason"]
    band = candidate["confidence_band"]
    if decision not in {"continue_session", "open_new_session"}:
        return None
    if band not in CONFIDENCE_BANDS:
        return None
    if reason not in SESSION_BOUNDARY_REASONS:
        return None
    if decision == "open_new_session":
        # Only these two reasons are the classifier's to give: it is told the
        # elapsed gap and judges whether the new message is unrelated to it.
        # The explicit reasons belong to the user and `previous_closed` to the
        # store, so a classifier claiming either is answering a question it was
        # not asked.
        if reason not in {"task_boundary", "idle_and_unrelated"}:
            return None
        if band != "high":
            return None
    return ClassifierOutcome(
        decision=decision, reason=reason, confidence_band=band
    )
