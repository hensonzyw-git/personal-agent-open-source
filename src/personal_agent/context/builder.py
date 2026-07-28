"""The Context Builder: the one place a model turn's input is assembled.

Cross-cutting design §9. Every model turn receives exactly one immutable
`ContextEnvelope`, built in the fixed order:

```text
system/policy instruction
-> current capability summary
-> deterministic relevant preferences
-> bounded lineage of latest valid Checkpoints
-> recent raw events after the checkpoint
-> exact pending state
-> retrieved memories
-> current user input
-> candidate tool declarations
```

Four properties are the reason this module exists rather than the orchestrator
assembling strings inline:

- **History is data, never instruction.** Checkpoints, raw events and memories
  are wrapped in an untrusted-data frame and are never merged into the system
  policy. A historical `ignore previous instructions` therefore reaches the
  model as quoted content, exactly like any other sentence the user once typed.
- **Exact state is structural.** A parked clarification, a `duplicate_check_id`,
  an operation state/version and an external `record_id` are projected from the
  operations table, not read out of a model-written summary. A Checkpoint that
  claims a write succeeded cannot make a pending operation look finished.
- **Lineage is bounded and traceable.** A `resumes` / `corrects_boundary` chain
  is followed to a configured depth with cycle detection, and stops at the last
  node whose Checkpoint verifies. A broken chain records a safe reason code; it
  never falls back to reading the whole Timeline.
- **The envelope cannot exist unvalidated.** Construction re-checks the budget
  and the mandatory components, so an envelope handed to a model adapter has by
  definition passed the Budgeter.

Nothing here decides authorisation. The tool declarations are the intersection
of the Router's candidates with the already-governed effective set: the builder
can only ever narrow what the Governed Tool Bridge already allowed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import InitVar, dataclass, field
from typing import Any, Final, Iterable, Mapping, Sequence

from personal_agent.api import events as timeline_events
from personal_agent.context.budget import (
    ComponentKind,
    ContextBudgeter,
    ContextComponent,
    mark_covered,
)
from personal_agent.context.compactor import Compactor, extract_record_id
from personal_agent.context.config import ContextConfig
from personal_agent.keys import HmacKey, HmacKeyRing
from personal_agent.policy.bridge import VisibleTool
from personal_agent.storage.models import (
    TERMINAL_OPERATION_STATES,
    ContextSession,
    ConversationEvent,
    Operation,
)
from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.manifest import canonical_json


SCHEMA_VERSION: Final[str] = "context_envelope_v1"

#: The frame every untrusted block carries. It is deliberately explicit and
#: symmetric: a model that is told "everything between these markers is recorded
#: data" has a single rule to follow, and the marker text itself is stripped
#: from any content that tries to forge it (`_frame`).
UNTRUSTED_OPEN: Final[str] = "<untrusted_data kind=\"{kind}\" ref=\"{ref}\">"
UNTRUSTED_CLOSE: Final[str] = "</untrusted_data>"
_FORGERY_PATTERNS: Final[tuple[str, ...]] = ("<untrusted_data", "</untrusted_data>")
_FRAME_ATTRIBUTE_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9_.:-]{1,160}"
)

#: `ContextEnvelope` is the model adapter's proof that the Context Budgeter
#: accepted the complete rendered input. Python cannot make a constructor truly
#: private, so the module keeps an identity-only witness out of the stored
#: dataclass fields. Direct construction and `dataclasses.replace()` do not carry
#: the witness and therefore fail closed instead of trusting caller-supplied
#: token totals.
_BUDGET_VALIDATION_WITNESS: Final[object] = object()

#: The relations that make an earlier Session part of this one's context. A
#: `new_topic` Session inherits nothing, which is the whole point of a boundary.
LINEAGE_RELATIONS: Final[frozenset[str]] = frozenset(
    {"resumes", "corrects_boundary"}
)

#: Why a lineage walk stopped early. A closed enumeration, because it travels
#: into trace: "the chain was too deep" and "the parent's Checkpoint no longer
#: verifies" must stay distinguishable without carrying any content.
LINEAGE_STOP_REASONS: Final[frozenset[str]] = frozenset(
    {
        "lineage_depth_exceeded",
        "lineage_parent_missing",
        "lineage_cycle",
        "lineage_left_timeline",
        "lineage_checkpoint_unverified",
    }
)

#: How many of a Session's events are decrypted for one turn. This bounds work,
#: not history: the newest events are kept, which is the same direction the
#: Budgeter trims, and when the cap binds the envelope says so through
#: `raw_window_scan_capped` rather than quietly shortening the window.
MAX_SESSION_EVENT_SCAN: Final[int] = 400
RAW_WINDOW_CAPPED: Final[str] = "raw_window_scan_capped"

#: The Checkpoint fields that may be shown to the model. `superseded_items` is
#: absent by design: it records decisions the user has already withdrawn, and
#: replaying them as context is exactly the failure `F-G8` describes.
_CHECKPOINT_VISIBLE_KEYS: Final[tuple[str, ...]] = (
    "goal",
    "constraints",
    "decisions",
    "entities",
    "completed_steps",
    "open_items",
    "evidence_refs",
    "exact_refs",
    "covered_from_sequence",
    "covered_through_sequence",
)


# -- structured projections -------------------------------------------------


@dataclass(frozen=True)
class PendingOperationProjection:
    """One non-terminal operation, exactly as the store holds it.

    Every field here is copied from a column, never from prose. `question` and
    `duplicate_existing` are the server's own text for a parked turn, so
    answering a clarification or a duplicate decision cannot lose the facts it
    was parked with. `record_id` stays `None` while an operation is still
    running -- a verified external id exists only once the write succeeded, and
    a pending turn must not read as though one already did.
    """

    operation_id: str
    state: str
    state_version: int
    tool: str | None
    idempotency_key: str
    duplicate_check_id: str | None
    record_id: str | None
    question: str | None
    duplicate_existing: str | None
    failure_reason: str | None
    cancel_requested: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "state": self.state,
            "state_version": self.state_version,
            "tool": self.tool,
            "idempotency_key": self.idempotency_key,
            "duplicate_check_id": self.duplicate_check_id,
            "record_id": self.record_id,
            "question": self.question,
            "duplicate_existing": self.duplicate_existing,
            "failure_reason": self.failure_reason,
            "cancel_requested": self.cancel_requested,
        }


@dataclass(frozen=True)
class MemoryCandidate:
    """A retrieved memory, with the source and time §9 requires beside it.

    `CAP-004` owns retrieval; the builder only knows how to place a candidate in
    the input as untrusted data with its provenance attached, and how to drop it
    first when the budget is tight.
    """

    memory_id: str
    kind: str
    source_ref: str
    recorded_at: str
    text: str
    weight: int = 0


@dataclass(frozen=True)
class ContextEnvelope:
    """The immutable, budget-validated input for exactly one model turn."""

    schema_version: str
    timeline_id: str
    session_id: str
    checkpoint_id: str | None
    lineage_checkpoint_ids: tuple[str, ...]
    lineage_stop_reason: str | None
    components: tuple[ContextComponent, ...]
    estimated_input_tokens: int
    soft_limit: int
    hard_limit: int
    config_version: str
    estimator_version: str
    source_fingerprint: str
    compaction_requested: bool
    trimmed: tuple[str, ...] = ()
    dropped_counts: Mapping[str, int] = field(default_factory=dict)
    _budget_validation_witness: InitVar[object | None] = None

    def __post_init__(self, _budget_validation_witness: object | None) -> None:
        """Re-check what makes an envelope safe to send.

        The Budgeter enforces all of this on the way in. Repeating it here is
        what makes "an envelope is validated" a property of the type rather than
        a property of one call site: no other construction path can hand a model
        adapter an over-limit or headless input.
        """
        if _budget_validation_witness is not _BUDGET_VALIDATION_WITNESS:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail=(
                    "a context envelope may only be created from a validated "
                    "Budgeter outcome"
                ),
            )
        if self.schema_version != SCHEMA_VERSION:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail="context envelope schema version mismatch",
            )
        if self.estimated_input_tokens > self.hard_limit:
            raise AppError(
                ErrorCode.CONTEXT_BUDGET_EXCEEDED,
                internal_detail=(
                    "an envelope may not exceed the hard limit it was built "
                    "against"
                ),
            )
        for kind in (ComponentKind.SYSTEM_POLICY, ComponentKind.USER_INPUT):
            if sum(1 for item in self.components if item.kind is kind) != 1:
                raise AppError(
                    ErrorCode.INTERNAL_ERROR,
                    internal_detail=(
                        f"a context envelope needs exactly one {kind.value}"
                    ),
                )
        if self.lineage_stop_reason is not None and (
            self.lineage_stop_reason not in LINEAGE_STOP_REASONS
        ):
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail="unknown lineage stop reason",
            )

    # -- accessors ------------------------------------------------------

    def texts_of(self, kind: ComponentKind) -> tuple[str, ...]:
        return tuple(item.text for item in self.components if item.kind is kind)

    @property
    def system_instruction(self) -> str:
        return self.texts_of(ComponentKind.SYSTEM_POLICY)[0]

    @property
    def user_text(self) -> str:
        return self.texts_of(ComponentKind.USER_INPUT)[0]

    @property
    def tool_aliases(self) -> tuple[str, ...]:
        return tuple(
            item.label
            for item in self.components
            if item.kind is ComponentKind.TOOL_DECLARATION
        )

    def trace(self) -> dict[str, Any]:
        """Enumerations, counts, versions and fingerprints -- never content.

        The one rule this has to keep is that reading a trace record must not
        reveal what the user said. Component labels are already either
        enumerations or fingerprints, and no `text` is copied here.
        """
        per_kind: dict[str, int] = {}
        for item in self.components:
            per_kind[item.kind.value] = per_kind.get(item.kind.value, 0) + 1
        return {
            "schema_version": self.schema_version,
            "session_fingerprint": self.source_fingerprint,
            "checkpoint_present": self.checkpoint_id is not None,
            "lineage_depth": len(self.lineage_checkpoint_ids),
            "lineage_stop_reason": self.lineage_stop_reason,
            "component_counts": per_kind,
            "estimated_input_tokens": self.estimated_input_tokens,
            "soft_limit": self.soft_limit,
            "hard_limit": self.hard_limit,
            "config_version": self.config_version,
            "estimator_version": self.estimator_version,
            "compaction_requested": self.compaction_requested,
            "trimmed": list(self.trimmed),
            "dropped_counts": dict(self.dropped_counts),
        }


# -- framing ----------------------------------------------------------------


def _frame(kind: str, ref: str, body: str) -> str:
    """Wrap recorded content as untrusted data the model may read, not obey.

    Any attempt to close the frame from inside is neutralised before wrapping.
    The marker metadata is restricted to a fixed safe grammar too: escaping the
    body alone would still let a forged Memory id close the opening tag before
    the body begins.
    """
    for label, value in (("kind", kind), ("ref", ref)):
        if (
            not isinstance(value, str)
            or _FRAME_ATTRIBUTE_RE.fullmatch(value) is None
        ):
            raise AppError(
                ErrorCode.CONTEXT_UNAVAILABLE,
                internal_detail=f"untrusted frame {label} is malformed",
            )
    safe = body
    for pattern in _FORGERY_PATTERNS:
        safe = safe.replace(pattern, pattern.replace("<", "﹤"))
    return "\n".join(
        (UNTRUSTED_OPEN.format(kind=kind, ref=ref), safe, UNTRUSTED_CLOSE)
    )


def _visible_checkpoint(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key] for key in _CHECKPOINT_VISIBLE_KEYS if key in payload
    }


# -- the builder ------------------------------------------------------------


class ContextBuilder:
    """Assembles one `ContextEnvelope` from trusted server-side state."""

    def __init__(
        self,
        config: ContextConfig,
        *,
        compactor: Compactor,
        budgeter: ContextBudgeter | None = None,
        max_session_event_scan: int = MAX_SESSION_EVENT_SCAN,
    ) -> None:
        if max_session_event_scan < 1:
            raise ValueError("max_session_event_scan must be positive")
        self._config = config
        self._compactor = compactor
        self._budgeter = budgeter or ContextBudgeter(config)
        self._max_scan = max_session_event_scan

    @property
    def budgeter(self) -> ContextBudgeter:
        return self._budgeter

    def build(
        self,
        db,
        keyring: KeyRing,
        identifier_key: HmacKey | HmacKeyRing,
        *,
        conversation_id: str,
        session_id: str,
        current_event_id: str,
        system_instruction: str,
        user_text: str,
        effective_tools: Sequence[VisibleTool],
        candidate_tools: Iterable[str] | None = None,
        essential_tools: Iterable[str] = (),
        preferences: Sequence[str] = (),
        memories: Sequence[MemoryCandidate] = (),
    ) -> ContextEnvelope:
        session = db.get(ContextSession, session_id)
        if session is None or session.conversation_id != conversation_id:
            raise AppError(
                ErrorCode.CONTEXT_UNAVAILABLE,
                internal_detail="session does not belong to this Timeline",
            )
        current_event = self._current_user_event(
            db,
            keyring,
            conversation_id=conversation_id,
            session_id=session_id,
            event_id=current_event_id,
            expected_text=user_text,
        )

        checkpoint = self._compactor.active_checkpoint(
            db, keyring, session_id=session_id
        )
        checkpoint_id = checkpoint[1].checkpoint_id if checkpoint else None
        covered_through = (
            checkpoint[1].covered_through_sequence if checkpoint else None
        )
        lineage, stop_reason = self._lineage_checkpoints(
            db, keyring, session=session
        )

        components: list[ContextComponent] = [
            self._budgeter.component(
                ComponentKind.SYSTEM_POLICY,
                system_instruction,
                label="system_policy",
            )
        ]

        capability = self._capability_summary(effective_tools)
        if capability is not None:
            components.append(capability)
        components.extend(self._preferences(preferences))
        components.extend(self._checkpoint_components(lineage, checkpoint))

        raw, capped = self._raw_events(
            db,
            keyring,
            session_id=session_id,
            exclude_event_id=current_event.event_id,
        )
        components.extend(raw)

        pending = self._pending_state(db, session_id=session_id)
        if pending:
            components.append(
                self._budgeter.component(
                    ComponentKind.PENDING_STATE,
                    {"pending_operations": [item.as_dict() for item in pending]},
                    label="pending_state",
                )
            )

        components.extend(self._memories(memories))
        components.append(
            self._budgeter.component(
                ComponentKind.USER_INPUT, user_text, label="user_input"
            )
        )
        declarations = self._tool_declarations(
            effective_tools,
            candidate_tools=candidate_tools,
            essential_tools=essential_tools,
        )
        components.extend(declarations)

        if covered_through is not None:
            components = list(
                mark_covered(components, through_sequence=covered_through)
            )

        outcome = self._budgeter.fit(components)
        trimmed = tuple(outcome.trimmed) + ((RAW_WINDOW_CAPPED,) if capped else ())

        return ContextEnvelope(
            schema_version=SCHEMA_VERSION,
            timeline_id=conversation_id,
            session_id=session_id,
            checkpoint_id=checkpoint_id,
            lineage_checkpoint_ids=tuple(
                row.checkpoint_id for _, row in lineage
            ),
            lineage_stop_reason=stop_reason,
            components=outcome.components,
            estimated_input_tokens=outcome.estimated_input_tokens,
            soft_limit=outcome.soft_limit,
            hard_limit=outcome.hard_limit,
            config_version=outcome.config_version,
            estimator_version=outcome.estimator_version,
            source_fingerprint=_fingerprint(
                identifier_key,
                {
                    "timeline_id": conversation_id,
                    "session_id": session_id,
                    "current_event_id": current_event.event_id,
                    "checkpoint_id": checkpoint_id,
                    "lineage": [row.checkpoint_id for _, row in lineage],
                    "components": [
                        [item.kind.value, item.label, item.ordinal]
                        for item in outcome.components
                    ],
                    "config_version": outcome.config_version,
                },
            ),
            compaction_requested=outcome.compaction_requested,
            trimmed=trimmed,
            dropped_counts=dict(outcome.dropped_counts),
            _budget_validation_witness=_BUDGET_VALIDATION_WITNESS,
        )

    # -- sections -------------------------------------------------------

    def _capability_summary(
        self, tools: Sequence[VisibleTool]
    ) -> ContextComponent | None:
        """What this device can currently do, as names and risk only.

        Deliberately not the schemas: those are the tool declarations, counted
        separately, and duplicating them here would spend the budget twice for
        the same information.
        """
        if not tools:
            return None
        return self._budgeter.component(
            ComponentKind.CAPABILITY_SUMMARY,
            {
                "available_tools": [
                    {"name": tool.alias, "risk_level": tool.risk_level}
                    for tool in tools
                ]
            },
            label="capability_summary",
        )

    def _preferences(
        self, preferences: Sequence[str]
    ) -> list[ContextComponent]:
        """Deterministic, server-owned preferences only.

        Phase 1 has no preference store, so this is normally empty. It exists as
        a parameter rather than a `CAP-004` import so the builder never learns
        how to retrieve anything by itself.
        """
        return [
            self._budgeter.component(
                ComponentKind.PREFERENCES,
                _frame("preference", f"pref-{index}", value),
                ordinal=index,
                label=f"preference:{index}",
            )
            for index, value in enumerate(preferences)
            if isinstance(value, str) and value.strip()
        ]

    def _checkpoint_components(
        self,
        lineage: list[tuple[dict[str, Any], Any]],
        checkpoint: tuple[dict[str, Any], Any] | None,
    ) -> list[ContextComponent]:
        """Lineage Checkpoints oldest-first, then this Session's own."""
        ordered = list(lineage)
        if checkpoint is not None:
            ordered.append(checkpoint)
        return [
            self._budgeter.component(
                ComponentKind.CHECKPOINT,
                _frame(
                    "checkpoint",
                    row.checkpoint_id,
                    canonical_json(_visible_checkpoint(payload)),
                ),
                ordinal=index,
                label=f"checkpoint:{row.checkpoint_id}",
            )
            for index, (payload, row) in enumerate(ordered)
        ]

    def _lineage_checkpoints(
        self, db, keyring: KeyRing, *, session: ContextSession
    ) -> tuple[list[tuple[dict[str, Any], Any]], str | None]:
        """Walk `resumes` / `corrects_boundary` to the last trusted node.

        Returns the chain oldest-first. A missing parent, a foreign Timeline, a
        cycle, an exhausted depth budget or a Checkpoint that no longer
        verifies all stop the walk and are reported as a reason code. None of
        them falls back to reading the whole Timeline: an unverifiable summary
        is missing context, and compensating with unbounded history would defeat
        both the budget and the Session boundary.
        """
        chain: list[tuple[dict[str, Any], Any]] = []
        stop_reason: str | None = None
        seen = {session.session_id}
        cursor = session
        depth = 0
        while cursor.relation_kind in LINEAGE_RELATIONS:
            if cursor.parent_session_id is None:
                stop_reason = "lineage_parent_missing"
                break
            if depth >= self._config.max_session_lineage_depth:
                stop_reason = "lineage_depth_exceeded"
                break
            parent_id = cursor.parent_session_id
            if parent_id in seen:
                stop_reason = "lineage_cycle"
                break
            parent = db.get(ContextSession, parent_id)
            if parent is None:
                stop_reason = "lineage_parent_missing"
                break
            if parent.conversation_id != session.conversation_id:
                stop_reason = "lineage_left_timeline"
                break
            seen.add(parent_id)
            depth += 1
            verified = self._compactor.active_checkpoint(
                db, keyring, session_id=parent_id
            )
            if verified is None:
                stop_reason = "lineage_checkpoint_unverified"
                break
            chain.append(verified)
            cursor = parent
        chain.reverse()
        return chain, stop_reason

    def _current_user_event(
        self,
        db,
        keyring: KeyRing,
        *,
        conversation_id: str,
        session_id: str,
        event_id: str,
        expected_text: str,
    ) -> ConversationEvent:
        """Return the persisted anchor for this turn, or refuse any mismatch.

        The API seals the current message before model work. Binding the separate
        `USER_INPUT` component to that exact immutable event prevents both a
        free-floating caller string and the same message appearing once as raw
        history and once as current input.
        """
        row = db.get(ConversationEvent, event_id)
        if (
            row is None
            or row.conversation_id != conversation_id
            or row.session_id != session_id
            or row.event_type != timeline_events.USER_MESSAGE
        ):
            raise AppError(
                ErrorCode.CONTEXT_UNAVAILABLE,
                internal_detail=(
                    "current user event does not belong to this Timeline and "
                    "Session"
                ),
            )
        plaintext = keyring.decrypt(
            row.encrypted_content,
            table="conversation_events",
            column="encrypted_content",
            row_id=row.event_id,
        )
        content = json.loads(plaintext.decode("utf-8"))
        persisted_text = content.get("text") if isinstance(content, dict) else None
        if (
            not isinstance(expected_text, str)
            or not expected_text.strip()
            or persisted_text != expected_text
        ):
            raise AppError(
                ErrorCode.CONTEXT_UNAVAILABLE,
                internal_detail=(
                    "current user input does not match its persisted event"
                ),
            )
        return row

    def _raw_events(
        self,
        db,
        keyring: KeyRing,
        *,
        session_id: str,
        exclude_event_id: str,
    ) -> tuple[list[ContextComponent], bool]:
        """This Session's prior model-visible events, newest-bounded, oldest-first.

        Dividers are excluded: they are presentation, and design §6.1 is
        explicit that neither divider event enters the model context. The
        persisted event supplying this turn's separate `USER_INPUT` is excluded
        too, so it cannot be sent twice.
        """
        rows = (
            db.query(ConversationEvent)
            .filter(
                ConversationEvent.session_id == session_id,
                ConversationEvent.event_id != exclude_event_id,
                ConversationEvent.event_type.in_(
                    timeline_events.MODEL_VISIBLE_EVENT_TYPES
                ),
            )
            .order_by(ConversationEvent.timeline_sequence.desc())
            .limit(self._max_scan + 1)
            .all()
        )
        capped = len(rows) > self._max_scan
        rows = list(reversed(rows[: self._max_scan]))
        components = []
        for row in rows:
            plaintext = keyring.decrypt(
                row.encrypted_content,
                table="conversation_events",
                column="encrypted_content",
                row_id=row.event_id,
            )
            content = json.loads(plaintext.decode("utf-8"))
            components.append(
                self._budgeter.component(
                    ComponentKind.RAW_EVENT,
                    _frame(
                        row.event_type,
                        row.event_id,
                        canonical_json(content),
                    ),
                    ordinal=row.timeline_sequence,
                    label=f"event:{row.event_type}",
                )
            )
        return components, capped

    def _pending_state(
        self, db, *, session_id: str
    ) -> list[PendingOperationProjection]:
        """Every non-terminal operation anchored in this Session.

        Read from `operations` through the events that anchored them, so the
        projection is the store's truth. A Checkpoint may *reference* these
        fields; it can never be what supplies them.
        """
        rows = (
            db.query(Operation)
            .join(
                ConversationEvent,
                ConversationEvent.operation_id == Operation.operation_id,
            )
            .filter(
                ConversationEvent.session_id == session_id,
                Operation.state.not_in(TERMINAL_OPERATION_STATES),
            )
            .order_by(Operation.created_at, Operation.operation_id)
            .distinct()
            .all()
        )
        return [
            PendingOperationProjection(
                operation_id=row.operation_id,
                state=row.state,
                state_version=row.state_version,
                tool=row.tool,
                idempotency_key=row.idempotency_key,
                duplicate_check_id=row.duplicate_check_id,
                record_id=extract_record_id(row.safe_result),
                question=(
                    row.safe_result
                    if row.state == "waiting_for_clarification"
                    else None
                ),
                duplicate_existing=(
                    row.safe_result
                    if row.state == "waiting_for_duplicate_decision"
                    else None
                ),
                failure_reason=row.failure_reason,
                cancel_requested=row.cancel_requested,
            )
            for row in rows
        ]

    def _memories(
        self, memories: Sequence[MemoryCandidate]
    ) -> list[ContextComponent]:
        return [
            self._budgeter.component(
                ComponentKind.MEMORY,
                _frame(
                    f"memory:{memory.kind}",
                    memory.memory_id,
                    canonical_json(
                        {
                            "text": memory.text,
                            "source_ref": memory.source_ref,
                            "recorded_at": memory.recorded_at,
                            "kind": memory.kind,
                        }
                    ),
                ),
                ordinal=index,
                weight=memory.weight,
                label=f"memory:{memory.memory_id}",
            )
            for index, memory in enumerate(memories)
        ]

    def _tool_declarations(
        self,
        effective_tools: Sequence[VisibleTool],
        *,
        candidate_tools: Iterable[str] | None,
        essential_tools: Iterable[str],
    ) -> list[ContextComponent]:
        """The intersection of Router candidates and the governed effective set.

        The direction is one-way on purpose: a candidate naming a tool that is
        not in the effective set is dropped silently, because the effective set
        is the authorisation decision and a Router suggestion is not. Only the
        Bridge can widen it, and it already did.
        """
        candidates = (
            None if candidate_tools is None else set(candidate_tools)
        )
        essential = set(essential_tools)
        selected = [
            tool
            for tool in effective_tools
            if candidates is None or tool.alias in candidates
        ]
        return [
            self._budgeter.component(
                ComponentKind.TOOL_DECLARATION,
                {
                    "type": "function",
                    "function": {
                        "name": tool.alias,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                },
                ordinal=index,
                essential=tool.alias in essential,
                label=tool.alias,
            )
            for index, tool in enumerate(selected)
        ]


def _fingerprint(key: HmacKey | HmacKeyRing, body: Mapping[str, Any]) -> str:
    secret = key.active.secret if isinstance(key, HmacKeyRing) else key.secret
    digest = hmac.new(
        secret,
        b"context-envelope\x1f" + canonical_json(body).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"hmac:{digest}"
