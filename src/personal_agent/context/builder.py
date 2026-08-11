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
-> exact clarification or safe Finance-retry continuation, when present
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
from dataclasses import InitVar, dataclass, field
from types import MappingProxyType
from typing import Any, Final, Iterable, Mapping, Sequence

from sqlalchemy import or_

from personal_agent.api import events as timeline_events
from personal_agent.context.budget import (
    ComponentKind,
    ContextBudgeter,
    ContextComponent,
    mark_covered,
)
from personal_agent.context.compactor import Compactor, extract_record_id
from personal_agent.context.config import ContextConfig
from personal_agent.context.continuation import (
    MAX_CLARIFICATION_QUESTION_CHARS,
    ClarificationContext,
    FinanceRetryContext,
)
from personal_agent.context.untrusted import frame_untrusted_data as _frame
from personal_agent.keys import HmacKey, HmacKeyRing
from personal_agent.policy.bridge import VisibleTool
from personal_agent.runtime.bookkeeping_intent import (
    is_finance_intent_candidate,
    is_finance_query_request,
    is_finance_retry_request,
)
from personal_agent.storage.models import (
    TERMINAL_OPERATION_STATES,
    ContextSession,
    ConversationEvent,
    Operation,
)
from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.finance_tools import FINANCE_QUERY_TOOL
from personal_agent_core.manifest import canonical_json


SCHEMA_VERSION: Final[str] = "context_envelope_v1"

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

#: Recorded when an *ancestor* Session's Checkpoint had to go to fit the budget.
#: This Session's own Checkpoint is never in that set: it is what replaces this
#: Session's raw history, so dropping it would enlarge the input it exists to
#: shrink. An ancestor's summary is additive -- its raw events are not in the
#: input at all -- so losing it costs recall, while refusing the turn costs the
#: user the whole conversation.
LINEAGE_CHECKPOINTS_TRIMMED: Final[str] = "dropped_lineage_checkpoints"

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
    #: A direct answer is forbidden for this turn. This is derived by the
    #: trusted builder, never by the provider, and includes sealed safe retries.
    finance_intent_required: bool = False
    #: The exact Finance tool required where the intent is unambiguous. Query
    #: turns use this to prevent a model from turning a read into a write.
    finance_required_tool: str | None = None
    #: An explicit retry phrase for which the server found no eligible sealed
    #: zero-write source. The orchestrator rejects it before calling the model.
    finance_retry_unbound: bool = False
    trimmed: tuple[str, ...] = ()
    dropped_counts: Mapping[str, int] = field(default_factory=dict)
    #: Estimated tokens per component kind, before the safety margin. §16.1
    #: requires the per-component numbers in trace, not only the total.
    component_tokens: Mapping[str, int] = field(default_factory=dict)
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
        if (
            sum(
                1
                for item in self.components
                if item.kind is ComponentKind.CLARIFICATION_CONTEXT
            )
            > 1
        ):
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail=(
                    "a context envelope may carry at most one exact continuation"
                ),
            )
        if self.lineage_stop_reason is not None and (
            self.lineage_stop_reason not in LINEAGE_STOP_REASONS
        ):
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail="unknown lineage stop reason",
            )
        if self.finance_required_tool is not None and (
            not self.finance_intent_required
            or not self.finance_required_tool.startswith("finance.")
        ):
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail="a required Finance tool needs a Finance turn",
            )
        if self.finance_retry_unbound and not self.finance_intent_required:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail="an unbound Finance retry needs a Finance turn",
            )
        # "Immutable" has to be true of the containers too, or a caller holding
        # the envelope could still edit what a model was told it may send.
        if not isinstance(self.components, tuple):
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail="envelope components must be a tuple",
            )
        for name in ("dropped_counts", "component_tokens"):
            object.__setattr__(
                self, name, MappingProxyType(dict(getattr(self, name)))
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
            "source_fingerprint": self.source_fingerprint,
            "component_tokens": dict(self.component_tokens),
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
            "finance_intent_required": self.finance_intent_required,
            "finance_required_tool": self.finance_required_tool,
            "finance_retry_unbound": self.finance_retry_unbound,
            "trimmed": list(self.trimmed),
            "dropped_counts": dict(self.dropped_counts),
        }


def _checkpoint_label(checkpoint_id: str) -> str:
    """The component label a Checkpoint is identified by inside one envelope."""
    return f"checkpoint:{checkpoint_id}"


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
        clarification_context: ClarificationContext | None = None,
        finance_retry_context: FinanceRetryContext | None = None,
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

        if clarification_context is not None and finance_retry_context is not None:
            raise AppError(
                ErrorCode.CONTEXT_UNAVAILABLE,
                internal_detail="a turn cannot carry two continuation contexts",
            )
        source_operation_ids = (
            clarification_context.source_operation_ids
            if clarification_context is not None
            else (
                (finance_retry_context.source_operation_id,)
                if finance_retry_context is not None
                else ()
            )
        )
        clarification = self._clarification_components(clarification_context)
        finance_retry = self._finance_retry_components(finance_retry_context)
        raw, capped = self._raw_events(
            db,
            keyring,
            session_id=session_id,
            exclude_event_id=current_event.event_id,
            exclude_operation_ids=source_operation_ids,
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
        components.extend(clarification)
        components.extend(finance_retry)
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

        outcome, lineage_dropped = self._fit(
            components,
            ancestor_labels=[
                _checkpoint_label(row.checkpoint_id) for _, row in lineage
            ],
        )
        trimmed = (
            tuple(outcome.trimmed)
            + ((RAW_WINDOW_CAPPED,) if capped else ())
            + ((LINEAGE_CHECKPOINTS_TRIMMED,) if lineage_dropped else ())
        )
        surviving_labels = {item.label for item in outcome.components}
        surviving_lineage = tuple(
            row.checkpoint_id
            for _, row in lineage
            if _checkpoint_label(row.checkpoint_id) in surviving_labels
        )

        finance_source_text = (
            finance_retry_context.original_user_text
            if finance_retry_context is not None
            else (
                clarification_context.original_user_text
                if clarification_context is not None
                else user_text
            )
        )
        finance_intent_required = (
            finance_retry_context is not None
            or is_finance_intent_candidate(finance_source_text)
            or is_finance_retry_request(user_text)
        )
        finance_required_tool = (
            FINANCE_QUERY_TOOL
            if is_finance_query_request(finance_source_text)
            else None
        )
        finance_retry_unbound = (
            clarification_context is None
            and finance_retry_context is None
            and is_finance_retry_request(user_text)
        )

        return ContextEnvelope(
            schema_version=SCHEMA_VERSION,
            timeline_id=conversation_id,
            session_id=session_id,
            checkpoint_id=checkpoint_id,
            # What is actually in the input, not what the walk found: a trace
            # that names a Checkpoint the model never saw is a false record.
            lineage_checkpoint_ids=surviving_lineage,
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
                    "clarification_source_operation_ids": list(
                        source_operation_ids
                    ),
                    "finance_intent_required": finance_intent_required,
                    "finance_required_tool": finance_required_tool,
                    "finance_retry_unbound": finance_retry_unbound,
                    "checkpoint_id": checkpoint_id,
                    "lineage": list(surviving_lineage),
                    "components": [
                        [item.kind.value, item.label, item.ordinal]
                        for item in outcome.components
                    ],
                    "config_version": outcome.config_version,
                },
            ),
            compaction_requested=outcome.compaction_requested,
            finance_intent_required=finance_intent_required,
            finance_required_tool=finance_required_tool,
            finance_retry_unbound=finance_retry_unbound,
            trimmed=trimmed,
            dropped_counts=dict(outcome.dropped_counts),
            component_tokens=self._tokens_by_kind(outcome.components),
            _budget_validation_witness=_BUDGET_VALIDATION_WITNESS,
        )

    # -- budget ---------------------------------------------------------

    def _fit(
        self, components: list[ContextComponent], *, ancestor_labels: list[str]
    ) -> tuple[Any, int]:
        """Fit the candidate context, giving up ancestor summaries before the turn.

        The Budgeter never drops a Checkpoint, and for this Session's own
        Checkpoint that is exactly right: it is the only retained form of history
        the raw window no longer carries. An *ancestor* Session's Checkpoint is a
        different thing -- none of its raw events are in this input, so dropping
        it loses recall and nothing else. Without this step a deep lineage of
        large summaries makes every turn in that Session refuse with
        `CONTEXT_BUDGET_EXCEEDED`, which the user has no way to clear.

        Oldest ancestor first, one at a time, and the refusal still stands once
        only this Session's Checkpoint is left.
        """
        remaining = list(components)
        droppable = list(ancestor_labels)
        dropped = 0
        while True:
            try:
                return self._budgeter.fit(remaining), dropped
            except AppError as refused:
                if (
                    refused.code is not ErrorCode.CONTEXT_BUDGET_EXCEEDED
                    or not droppable
                ):
                    raise
                oldest = droppable.pop(0)
                remaining = [
                    item for item in remaining if item.label != oldest
                ]
                dropped += 1

    def _tokens_by_kind(
        self, components: Iterable[ContextComponent]
    ) -> dict[str, int]:
        totals: dict[str, int] = {}
        for item in components:
            totals[item.kind.value] = totals.get(
                item.kind.value, 0
            ) + self._budgeter.estimate(item.text)
        return totals

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

        A malformed entry is refused rather than skipped. Silently dropping one
        preference out of a supplied list is the failure that looks exactly like
        the preference having been honoured.
        """
        components = []
        for index, value in enumerate(preferences):
            if not isinstance(value, str) or not value.strip():
                raise AppError(
                    ErrorCode.CONTEXT_UNAVAILABLE,
                    internal_detail="a preference must be non-empty text",
                )
            components.append(
                self._budgeter.component(
                    ComponentKind.PREFERENCES,
                    _frame("preference", f"pref-{index}", value),
                    ordinal=index,
                    label=f"preference:{index}",
                )
            )
        return components

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
                label=_checkpoint_label(row.checkpoint_id),
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

    def _clarification_components(
        self, context: ClarificationContext | None
    ) -> list[ContextComponent]:
        if context is None:
            return []
        if (
            not isinstance(context.original_user_text, str)
            or not context.original_user_text.strip()
            or not isinstance(context.question, str)
            or not context.question.strip()
            or len(context.question) > MAX_CLARIFICATION_QUESTION_CHARS
        ):
            raise AppError(
                ErrorCode.CONTEXT_UNAVAILABLE,
                internal_detail="sealed clarification context is malformed",
            )
        exchanges: list[dict[str, str]] = []
        for exchange in context.completed_exchanges:
            if (
                not isinstance(exchange.question, str)
                or not exchange.question.strip()
                or len(exchange.question) > MAX_CLARIFICATION_QUESTION_CHARS
                or not isinstance(exchange.answer, str)
                or not exchange.answer.strip()
            ):
                raise AppError(
                    ErrorCode.CONTEXT_UNAVAILABLE,
                    internal_detail="sealed clarification exchange is malformed",
                )
            exchanges.append(
                {"question": exchange.question, "answer": exchange.answer}
            )
        if len(set(context.source_operation_ids)) != len(
            context.source_operation_ids
        ) or (
            context.source_operation_ids
            and len(context.source_operation_ids)
            != len(context.completed_exchanges) + 1
        ):
            raise AppError(
                ErrorCode.CONTEXT_UNAVAILABLE,
                internal_detail="clarification source operation ids are malformed",
            )
        body = canonical_json(
            {
                "original_user_text": context.original_user_text,
                "completed_exchanges": exchanges,
                "pending_question": context.question,
            }
        )
        return [
            self._budgeter.component(
                ComponentKind.CLARIFICATION_CONTEXT,
                _frame(
                    "clarification_context",
                    (
                        context.source_operation_ids[-1]
                        if context.source_operation_ids
                        else "legacy"
                    ),
                    body,
                ),
                label="clarification_context",
            )
        ]

    def _finance_retry_components(
        self, context: FinanceRetryContext | None
    ) -> list[ContextComponent]:
        if context is None:
            return []
        if (
            not isinstance(context.original_user_text, str)
            or not context.original_user_text.strip()
            or not isinstance(context.source_operation_id, str)
            or not context.source_operation_id.strip()
            or context.source_failure_reason
            not in {"model_unavailable", ErrorCode.BOOKKEEPING_TOOL_REQUIRED.value}
        ):
            raise AppError(
                ErrorCode.CONTEXT_UNAVAILABLE,
                internal_detail="sealed finance retry context is malformed",
            )
        exchanges: list[dict[str, str]] = []
        for exchange in context.completed_exchanges:
            if (
                not isinstance(exchange.question, str)
                or not exchange.question.strip()
                or len(exchange.question) > MAX_CLARIFICATION_QUESTION_CHARS
                or not isinstance(exchange.answer, str)
                or not exchange.answer.strip()
            ):
                raise AppError(
                    ErrorCode.CONTEXT_UNAVAILABLE,
                    internal_detail="sealed finance retry exchange is malformed",
                )
            exchanges.append(
                {"question": exchange.question, "answer": exchange.answer}
            )
        body = canonical_json(
            {
                "original_user_text": context.original_user_text,
                "answered_clarifications": exchanges,
            }
        )
        return [
            self._budgeter.component(
                ComponentKind.CLARIFICATION_CONTEXT,
                _frame(
                    "finance_retry_context",
                    context.source_operation_id,
                    body,
                ),
                label="finance_retry_context",
            )
        ]

    def _raw_events(
        self,
        db,
        keyring: KeyRing,
        *,
        session_id: str,
        exclude_event_id: str,
        exclude_operation_ids: Sequence[str] = (),
    ) -> tuple[list[ContextComponent], bool]:
        """This Session's prior model-visible events, newest-bounded, oldest-first.

        Dividers are excluded: they are presentation, and design §6.1 is
        explicit that neither divider event enters the model context. The
        persisted event supplying this turn's separate `USER_INPUT` is excluded
        too, so it cannot be sent twice. Events belonging to the unresolved
        clarification chain are represented by the mandatory exact
        `CLARIFICATION_CONTEXT` component and are excluded before the scan limit,
        so they cannot be duplicated or crowd out older usable history.
        """
        excluded = tuple(exclude_operation_ids)
        if len(set(excluded)) != len(excluded) or any(
            not isinstance(item, str) or not item for item in excluded
        ):
            raise AppError(
                ErrorCode.CONTEXT_UNAVAILABLE,
                internal_detail="clarification source operation ids are malformed",
            )
        query = db.query(ConversationEvent).filter(
            ConversationEvent.session_id == session_id,
            ConversationEvent.event_id != exclude_event_id,
            ConversationEvent.event_type.in_(
                timeline_events.MODEL_VISIBLE_EVENT_TYPES
            ),
        )
        if excluded:
            found = {
                row[0]
                for row in db.query(ConversationEvent.operation_id)
                .filter(
                    ConversationEvent.session_id == session_id,
                    ConversationEvent.operation_id.in_(excluded),
                    ConversationEvent.event_type.in_(
                        timeline_events.MODEL_VISIBLE_EVENT_TYPES
                    ),
                )
                .distinct()
                .all()
            }
            if found != set(excluded):
                raise AppError(
                    ErrorCode.CONTEXT_UNAVAILABLE,
                    internal_detail=(
                        "clarification source operations do not belong to this "
                        "Session"
                    ),
                )
            query = query.filter(
                or_(
                    ConversationEvent.operation_id.is_(None),
                    ConversationEvent.operation_id.not_in(excluded),
                )
            )
        rows = (
            query
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
        # The caller's order is relevance order: the Router puts its best
        # candidate first, and the governed catalog puts the business tool ahead
        # of `meta.*`. The Budgeter drops the *lowest* weight first, so relevance
        # has to be inverted into weight here. Leaving every tool at weight 0
        # would trim by ordinal instead, which drops the tool the user is
        # actually trying to use and keeps the least useful one.
        total = len(selected)
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
                weight=total - index,
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
