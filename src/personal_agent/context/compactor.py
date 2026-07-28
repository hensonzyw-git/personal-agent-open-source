"""The Compactor: immutable Checkpoints that bound a Session's context.

Cross-cutting design §8. A long topic stays in one Session; instead of feeding
the whole history to the model, the Compactor builds a structured
`context_checkpoint_v1` summary over a closed range of events. The raw archive
is never deleted -- every Checkpoint traces back to an immutable event range,
and a tampered range, a grafted summary, a credential or a revived superseded
decision each fail validation before the current active Checkpoint is touched.

The provider is a framework-agnostic structured adapter (§8.1). It receives
history text marked as untrusted data and exact operation projections; it gets
no tools, no credentials, and no access to the full archive. Its output is
validated against §8.4 before it is allowed to become `active`. Any check
failure leaves the current active Checkpoint untouched.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal, Protocol

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from personal_agent.api import events
from personal_agent.context.config import ContextConfig
from personal_agent.keys import HmacKey, HmacKeyRing
from personal_agent.storage.models import (
    ContextCheckpoint,
    ContextCheckpointSource,
    ContextSession,
    ConversationEvent,
    Operation,
)
from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.manifest import canonical_json


COMPACTOR_VERSION: Final[str] = "compactor-v1"
SCHEMA_VERSION: Final[str] = "context_checkpoint_v1"
DEFAULT_PROVIDER_TIMEOUT_SECONDS: Final[float] = 25.0

_TABLE = "context_checkpoints"
_COLUMN = "encrypted_payload"

_REQUIRED_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "session_id",
        "goal",
        "constraints",
        "decisions",
        "entities",
        "completed_steps",
        "open_items",
        "superseded_items",
        "evidence_refs",
        "exact_refs",
        "covered_from_sequence",
        "covered_through_sequence",
    }
)

_LIST_ITEM_KEYS: Final[frozenset[str]] = frozenset(
    {
        "constraints",
        "decisions",
        "entities",
        "completed_steps",
        "open_items",
        "superseded_items",
    }
)

#: Patterns that mark a historical user message as a possible prompt-injection
#: attempt. If one appears verbatim in a source event, the Compactor must not
#: let the provider promote it into `constraints` or `decisions` as an
#: instruction. The payload may still quote it as an `exact_ref`.
_INJECTION_MARKERS: Final[tuple[str, ...]] = (
    "ignore previous instructions",
    "ignore previous",
    "you are now",
    "system:",
    "reveal all secrets",
    "reveal every secret",
    "disregard the policy",
)

#: Credential-like patterns. A false-positive refusal is safe (no Checkpoint is
#: built, raw events are retained); a false-negative is not (a credential would
#: be served to the model as context).
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"Bearer\s+[A-Za-z0-9\-_\.]+", re.IGNORECASE),
    re.compile(r"sk-ant-[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9\-_]+\.eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+"),
    re.compile(
        r"(?:app_id|app_secret|access_token|api_key)[\"\s:]+[A-Za-z0-9]{16,}",
        re.IGNORECASE,
    ),
)

_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"\d+(?:\.\d+)?")
_PERMISSION_MARKERS: Final[tuple[str, ...]] = (
    "permission",
    "scope",
    "allowlist",
    "权限",
    "授权",
)
_COMPLETION_MARKERS: Final[tuple[str, ...]] = (
    "completed",
    "succeeded",
    "success",
    "done",
    "已完成",
    "已记录",
    "已写入",
    "成功",
)
_TERMINAL_OPERATION_STATES: Final[frozenset[str]] = frozenset(
    {"succeeded", "failed_safe", "needs_manual_review", "cancelled_pre_submit"}
)


class BuildMode(StrEnum):
    FIRST = "first"
    INCREMENTAL = "incremental"
    FULL_REBUILD = "full_rebuild"


@dataclass(frozen=True)
class RawEventSource:
    event_id: str
    timeline_sequence: int
    event_type: str
    content: dict[str, Any]
    operation_id: str | None
    turn_id: str
    content_fingerprint: str


@dataclass(frozen=True)
class OperationProjection:
    operation_id: str
    state: str
    state_version: int
    tool: str | None
    record_id: str | None
    duplicate_check_id: str | None
    safe_result: str | None
    idempotency_key: str | None = None
    failure_reason: str | None = None
    cancel_requested: bool = False


@dataclass(frozen=True)
class SourceBundle:
    session_id: str
    events: tuple[RawEventSource, ...]
    operations: tuple[OperationProjection, ...]
    parent_checkpoint_id: str | None
    parent_source_hash: str | None
    parent_payload: dict[str, Any] | None
    covered_from_sequence: int
    covered_through_sequence: int
    mode: BuildMode


@dataclass(frozen=True)
class CompactorRequest:
    session_id: str
    schema_version: str
    compactor_version: str
    mode: BuildMode
    parent_checkpoint_id: str | None
    parent_source_hash: str | None
    parent_checkpoint: dict[str, Any] | None
    raw_events: tuple[RawEventSource, ...]
    operation_projections: tuple[OperationProjection, ...]
    covered_from_sequence: int
    covered_through_sequence: int


class CompactorProvider(Protocol):
    def compact(self, request: CompactorRequest) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ValidationFailure:
    code: str
    detail: str


@dataclass(frozen=True)
class BuildResult:
    status: Literal[
        "active",
        "validation_failed",
        "provider_failed",
        "lost_race",
        "stale_parent",
        "no_events",
    ]
    checkpoint_id: str | None = None
    failure: ValidationFailure | None = None


# -- source hashing --------------------------------------------------------


def _event_content_fingerprint(envelope: dict[str, Any]) -> str:
    """A hash over the encrypted envelope, not the plaintext.

    No decryption key is needed. The envelope is immutable (written once at
    `append_event`, never modified), so the same row always yields the same
    fingerprint. AES-GCM's AAD binds the ciphertext to its `row_id`, so a
    ciphertext lifted to another row cannot decrypt -- the fingerprint is
    implicitly bound to the event identity.
    """
    return hashlib.sha256(
        canonical_json(
            {
                "nonce": envelope["nonce"],
                "ciphertext": envelope["ciphertext"],
                "tag": envelope["tag"],
            }
        ).encode("utf-8")
    ).hexdigest()


def _hmac_key(key: HmacKey | HmacKeyRing) -> HmacKey:
    if isinstance(key, HmacKeyRing):
        return key.active
    return key


def source_hmac(identifier_key: HmacKey | HmacKeyRing, event_id: str) -> str:
    """The stored form of a Checkpoint source event id.

    Domain-separated from `timeline_alias_hmac` by the `checkpoint-source`
    prefix, so the same identifier key cannot produce a collision across the
    two purposes.
    """
    return hmac.new(
        _hmac_key(identifier_key).secret,
        f"checkpoint-source\x1f{event_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def compute_source_hash(
    *,
    parent_checkpoint_id: str | None,
    parent_source_hash: str | None,
    events: tuple[RawEventSource, ...],
    operations: tuple[OperationProjection, ...],
    schema_version: str,
    compactor_version: str,
) -> str:
    """The §8.1 source hash.

    Covers the parent Checkpoint id and hash, the ordered source event ids and
    their content fingerprints, the ordered operation projections, the schema
    version and the compactor version. Any one of these changing makes a build
    result invalid for a different stretch of history.
    """
    body = {
        "parent_checkpoint_id": parent_checkpoint_id,
        "parent_source_hash": parent_source_hash,
        "events": [
            {"event_id": e.event_id, "fingerprint": e.content_fingerprint}
            for e in events
        ],
        "operations": [
            {
                "operation_id": o.operation_id,
                "state": o.state,
                "state_version": o.state_version,
                "tool": o.tool,
                "record_id": o.record_id,
                "duplicate_check_id": o.duplicate_check_id,
                "safe_result": o.safe_result,
                "idempotency_key": o.idempotency_key,
                "failure_reason": o.failure_reason,
                "cancel_requested": o.cancel_requested,
            }
            for o in operations
        ],
        "schema_version": schema_version,
        "compactor_version": compactor_version,
    }
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


# -- validation ------------------------------------------------------------


def _payload_text_values(payload: dict[str, Any]) -> list[str]:
    """Every free-text `value` the payload may carry, for fact scanning."""
    texts: list[str] = []
    goal = payload.get("goal")
    if isinstance(goal, dict):
        texts.append(str(goal.get("value", "")))
    for key in _LIST_ITEM_KEYS:
        items = payload.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    texts.append(str(item.get("value", "")))
    return texts


def _validate_schema(
    payload: dict[str, Any], sources: SourceBundle
) -> ValidationFailure | None:
    if not isinstance(payload, dict):
        return ValidationFailure("schema_invalid", "payload is not a dict")
    if payload.get("schema_version") != SCHEMA_VERSION:
        return ValidationFailure("schema_invalid", "schema_version mismatch")
    missing = _REQUIRED_PAYLOAD_KEYS - set(payload)
    if missing:
        return ValidationFailure(
            "missing_goal" if "goal" in missing else "schema_invalid",
            f"missing keys: {sorted(missing)}",
        )
    unexpected = set(payload) - _REQUIRED_PAYLOAD_KEYS
    if unexpected:
        return ValidationFailure(
            "schema_invalid", f"unexpected keys: {sorted(unexpected)}"
        )
    if payload.get("session_id") != sources.session_id:
        return ValidationFailure(
            "schema_invalid", "session_id does not match the source Session"
        )
    known_refs = _known_source_refs(sources)
    goal = payload.get("goal")
    if not isinstance(goal, dict) or set(goal) != {"value", "source_refs"}:
        return ValidationFailure("missing_goal", "goal is malformed")
    if not isinstance(goal.get("value"), str) or not goal["value"].strip():
        return ValidationFailure("empty_summary", "goal.value is empty")
    failure = _validate_refs(goal.get("source_refs"), known_refs, label="goal")
    if failure is not None:
        return failure
    for key in _LIST_ITEM_KEYS:
        items = payload.get(key)
        if not isinstance(items, list):
            return ValidationFailure("schema_invalid", f"{key} is not a list")
        for item in items:
            if not isinstance(item, dict) or set(item) != {"value", "source_refs"}:
                return ValidationFailure(
                    "schema_invalid", f"{key} entry is not {{value, source_refs}}"
                )
            if not isinstance(item.get("value"), str) or not item["value"]:
                return ValidationFailure(
                    "schema_invalid", f"{key} entry has empty value"
                )
            failure = _validate_refs(
                item.get("source_refs"), known_refs, label=f"{key} entry"
            )
            if failure is not None:
                return failure
    if not isinstance(payload.get("evidence_refs"), list):
        return ValidationFailure("schema_invalid", "evidence_refs is not a list")
    if not isinstance(payload.get("exact_refs"), list):
        return ValidationFailure("schema_invalid", "exact_refs is not a list")
    return None


def _known_source_refs(sources: SourceBundle) -> set[str]:
    known = {event.event_id for event in sources.events}
    known.update(operation.operation_id for operation in sources.operations)
    parent = sources.parent_payload
    if not isinstance(parent, dict):
        return known
    goal = parent.get("goal")
    if isinstance(goal, dict):
        known.update(_string_refs(goal.get("source_refs")))
    for key in _LIST_ITEM_KEYS:
        for item in parent.get(key, []):
            if isinstance(item, dict):
                known.update(_string_refs(item.get("source_refs")))
    return known


def _string_refs(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str) and item}


def _validate_refs(
    value: Any, known_refs: set[str], *, label: str
) -> ValidationFailure | None:
    refs = _string_refs(value)
    if not isinstance(value, list) or len(refs) != len(value) or not refs:
        return ValidationFailure(
            "schema_invalid", f"{label} needs non-empty string source_refs"
        )
    unknown = sorted(refs - known_refs)
    if unknown:
        return ValidationFailure(
            "source_ref_missing", f"{label} references unknown sources: {unknown}"
        )
    return None


def _validate_source_range(
    payload: dict[str, Any], sources: SourceBundle
) -> ValidationFailure | None:
    if payload.get("covered_from_sequence") != sources.covered_from_sequence:
        return ValidationFailure(
            "source_range_discontinuous",
            "covered_from_sequence does not match sources",
        )
    if payload.get("covered_through_sequence") != sources.covered_through_sequence:
        return ValidationFailure(
            "source_range_discontinuous",
            "covered_through_sequence does not match sources",
        )
    return None


def _source_number_set(sources: SourceBundle) -> set[str]:
    numbers: set[str] = set()
    for event in sources.events:
        for blob in (event.content,):
            for match in _NUMBER_RE.finditer(json.dumps(blob, ensure_ascii=False)):
                numbers.add(match.group())
    for op in sources.operations:
        if op.record_id:
            for match in _NUMBER_RE.finditer(op.record_id):
                numbers.add(match.group())
        if op.safe_result:
            for match in _NUMBER_RE.finditer(op.safe_result):
                numbers.add(match.group())
    if sources.parent_payload:
        for match in _NUMBER_RE.finditer(canonical_json(sources.parent_payload)):
            numbers.add(match.group())
    return numbers


def _validate_no_invented_facts(
    payload: dict[str, Any], sources: SourceBundle
) -> ValidationFailure | None:
    allowed = _source_number_set(sources)
    for text_value in _payload_text_values(payload):
        for match in _NUMBER_RE.finditer(text_value):
            if match.group() not in allowed:
                return ValidationFailure(
                    "invented_facts",
                    f"payload references number {match.group()!r} not in sources",
                )
    event_text = {
        event.event_id: canonical_json(event.content).lower()
        for event in sources.events
    }
    if isinstance(sources.parent_payload, dict):
        parent_items = [sources.parent_payload.get("goal")]
        for key in _LIST_ITEM_KEYS:
            parent_items.extend(sources.parent_payload.get(key, []))
        for item in parent_items:
            if not isinstance(item, dict):
                continue
            value = str(item.get("value", "")).lower()
            for source_ref in _string_refs(item.get("source_refs")):
                event_text[source_ref] = (
                    event_text.get(source_ref, "") + " " + value
                ).strip()
    operation_by_id = {
        operation.operation_id: operation for operation in sources.operations
    }
    for key in ("goal", *_LIST_ITEM_KEYS):
        items = [payload.get(key)] if key == "goal" else payload.get(key, [])
        for item in items:
            if not isinstance(item, dict):
                continue
            value = str(item.get("value", "")).lower()
            refs = _string_refs(item.get("source_refs"))
            if any(marker in value for marker in _PERMISSION_MARKERS):
                if not any(
                    any(
                        marker in event_text.get(ref, "")
                        for marker in _PERMISSION_MARKERS
                    )
                    for ref in refs
                ):
                    return ValidationFailure(
                        "invented_facts",
                        "permission claim is not grounded in a referenced event",
                    )
            if key == "completed_steps" and any(
                marker in value for marker in _COMPLETION_MARKERS
            ):
                supported = any(
                    operation_by_id.get(ref) is not None
                    and operation_by_id[ref].state in _TERMINAL_OPERATION_STATES
                    for ref in refs
                ) or any(
                    any(
                        marker in event_text.get(ref, "")
                        for marker in _COMPLETION_MARKERS
                    )
                    for ref in refs
                )
                if not supported:
                    return ValidationFailure(
                        "invented_facts",
                        "completed step has no terminal operation or completion event",
                    )
    return None


def _validate_superseded_consistency(
    payload: dict[str, Any], sources: SourceBundle
) -> ValidationFailure | None:
    if not sources.parent_payload:
        return None
    parent_superseded = sources.parent_payload.get("superseded_items", [])
    if not isinstance(parent_superseded, list):
        return None
    superseded_values = {
        item.get("value")
        for item in parent_superseded
        if isinstance(item, dict)
    }
    for decision in payload.get("decisions", []):
        if not isinstance(decision, dict):
            continue
        value = decision.get("value")
        if value in superseded_values:
            return ValidationFailure(
                "revived_superseded",
                "a decision the user superseded was revived in decisions",
            )
    return None


def _validate_no_instruction_promotion(
    payload: dict[str, Any], sources: SourceBundle
) -> ValidationFailure | None:
    injected: list[str] = []
    for event in sources.events:
        text = str(event.content.get("text", ""))
        for marker in _INJECTION_MARKERS:
            if marker in text.lower():
                injected.append(marker)
    if not injected:
        return None
    for text_value in _payload_text_values(payload):
        lower = text_value.lower()
        for marker in injected:
            if marker in lower:
                return ValidationFailure(
                    "injection_promoted",
                    "a historical injection marker was promoted into the payload",
                )
    return None


def _validate_uncompressible_state(
    payload: dict[str, Any], sources: SourceBundle
) -> ValidationFailure | None:
    exact_refs: set[tuple[str, str]] = set()
    known_operations = {
        operation.operation_id: operation for operation in sources.operations
    }
    for ref in payload.get("exact_refs", []):
        if not isinstance(ref, dict) or set(ref) != {"kind", "id", "field"}:
            return ValidationFailure(
                "schema_invalid", "exact_ref is not {kind, id, field}"
            )
        if ref.get("kind") != "operation":
            return ValidationFailure(
                "schema_invalid", "exact_ref kind must be operation"
            )
        operation_id = ref.get("id")
        field_name = ref.get("field")
        if (
            not isinstance(operation_id, str)
            or operation_id not in known_operations
            or not isinstance(field_name, str)
            or field_name not in {
                "state",
                "state_version",
                "tool",
                "idempotency_key",
                "cancel_requested",
                "record_id",
                "duplicate_check_id",
                "failure_reason",
                "safe_result",
            }
        ):
            return ValidationFailure(
                "evidence_ref_missing",
                "exact_ref points at an unknown operation or field",
            )
        if (operation_id, field_name) in exact_refs:
            return ValidationFailure("schema_invalid", "exact_ref is duplicated")
        exact_refs.add((operation_id, field_name))

    uncompressible_values: list[str] = []
    for op in sources.operations:
        fields: dict[str, Any] = {
            "state": op.state,
            "state_version": op.state_version,
            "tool": op.tool,
            "idempotency_key": op.idempotency_key,
            "cancel_requested": op.cancel_requested,
            "record_id": op.record_id,
            "duplicate_check_id": op.duplicate_check_id,
            "failure_reason": op.failure_reason,
        }
        if op.state not in _TERMINAL_OPERATION_STATES:
            fields["safe_result"] = op.safe_result
        for field_name, value in fields.items():
            if value is None:
                continue
            if (op.operation_id, field_name) not in exact_refs:
                return ValidationFailure(
                    "uncompressible_rewritten",
                    f"{op.operation_id}.{field_name} is not in exact_refs",
                )
            if isinstance(value, str) and value:
                uncompressible_values.append(value)

    for value in uncompressible_values:
        for text_value in _payload_text_values(payload):
            if value in text_value:
                return ValidationFailure(
                    "uncompressible_rewritten",
                    "an uncompressible identifier appears as free text",
                )
    return None


def _validate_open_items_alignment(
    payload: dict[str, Any],
    sources: SourceBundle,
    waiting_operations: tuple[OperationProjection, ...],
) -> ValidationFailure | None:
    if not waiting_operations:
        return None
    open_refs = [
        _string_refs(item.get("source_refs"))
        for item in payload.get("open_items", [])
        if isinstance(item, dict)
    ]
    for op in waiting_operations:
        if (
            op.state not in _TERMINAL_OPERATION_STATES
            and not any(op.operation_id in refs for refs in open_refs)
        ):
            return ValidationFailure(
                "open_items_misaligned",
                "a waiting operation has no matching open_item",
            )
    return None


def _validate_evidence_refs(
    payload: dict[str, Any], sources: SourceBundle
) -> ValidationFailure | None:
    known_ops = {op.operation_id: op for op in sources.operations}
    for ref in payload.get("evidence_refs", []):
        if not isinstance(ref, dict) or set(ref) != {
            "kind",
            "id",
            "safe_summary",
        }:
            return ValidationFailure(
                "evidence_ref_missing",
                "evidence_ref is not {kind, id, safe_summary}",
            )
        ref_id = ref.get("id")
        if (
            ref.get("kind") != "operation"
            or not isinstance(ref_id, str)
            or ref_id not in known_ops
        ):
            return ValidationFailure(
                "evidence_ref_missing",
                "evidence_ref points at an operation not in sources",
            )
        if ref.get("safe_summary") != known_ops[ref_id].safe_result:
            return ValidationFailure(
                "evidence_ref_missing",
                "evidence_ref safe_summary does not match the projection",
            )
    return None


def _validate_no_secrets(
    payload: dict[str, Any], sources: SourceBundle
) -> ValidationFailure | None:
    blob = canonical_json(payload)
    for pattern in _SECRET_PATTERNS:
        if pattern.search(blob):
            return ValidationFailure(
                "secret_detected",
                "checkpoint payload contains a credential-like pattern",
            )
    return None


def validate_checkpoint(
    payload: dict[str, Any],
    *,
    sources: SourceBundle,
    expected_source_hash: str,
    waiting_operations: tuple[OperationProjection, ...] = (),
) -> ValidationFailure | None:
    """Run the §8.4 validation pipeline. Returns None on success."""
    computed_source_hash = compute_source_hash(
        parent_checkpoint_id=sources.parent_checkpoint_id,
        parent_source_hash=sources.parent_source_hash,
        events=sources.events,
        operations=sources.operations,
        schema_version=SCHEMA_VERSION,
        compactor_version=COMPACTOR_VERSION,
    )
    if not hmac.compare_digest(computed_source_hash, expected_source_hash):
        return ValidationFailure(
            "source_hash_mismatch", "source hash does not match the source bundle"
        )
    validators = (
        _validate_schema,
        _validate_source_range,
        _validate_no_secrets,
        _validate_no_invented_facts,
        _validate_superseded_consistency,
        _validate_no_instruction_promotion,
        _validate_uncompressible_state,
        _validate_evidence_refs,
    )
    for validator in validators:
        failure = validator(payload, sources)
        if failure is not None:
            return failure
    return _validate_open_items_alignment(
        payload, sources, waiting_operations
    )


# -- Compactor -------------------------------------------------------------


class Compactor:
    def __init__(
        self,
        config: ContextConfig,
        *,
        provider: CompactorProvider | None = None,
        provider_timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ) -> None:
        if provider_timeout_seconds <= 0:
            raise ValueError("provider_timeout_seconds must be positive")
        self._config = config
        self._provider = provider
        self._provider_timeout_seconds = provider_timeout_seconds

    def active_checkpoint(
        self, db, keyring: KeyRing, *, session_id: str
    ) -> tuple[dict[str, Any], ContextCheckpoint] | None:
        row = (
            db.query(ContextCheckpoint)
            .filter_by(session_id=session_id, status="active")
            .one_or_none()
        )
        if row is None:
            return None
        plaintext = keyring.decrypt(
            row.encrypted_payload,
            table=_TABLE,
            column=_COLUMN,
            row_id=row.checkpoint_id,
        )
        payload = json.loads(plaintext.decode("utf-8"))
        sources = self._sources_for_stored_checkpoint(db, keyring, row)
        failure = validate_checkpoint(
            payload,
            sources=sources,
            expected_source_hash=row.source_hash,
            waiting_operations=sources.operations,
        )
        if (
            failure is not None
            or row.schema_version != SCHEMA_VERSION
            or row.compactor_version != COMPACTOR_VERSION
        ):
            row.status = "invalid"
            db.flush()
            return None
        return payload, row

    def build_checkpoint(
        self,
        db,
        keyring: KeyRing,
        identifier_key: HmacKey | HmacKeyRing,
        *,
        session_id: str,
        now: datetime,
    ) -> BuildResult:
        session = db.get(ContextSession, session_id)
        if session is None:
            raise AppError(
                ErrorCode.CONTEXT_UNAVAILABLE,
                internal_detail="no such Session to compact",
            )

        verified_active = self.active_checkpoint(
            db, keyring, session_id=session_id
        )
        mode = self._decide_build_mode(db, session_id)
        parent_row = (
            verified_active[1] if verified_active is not None else None
        )
        parent_payload = None
        parent_checkpoint_id = None
        parent_source_hash = None
        if mode is BuildMode.INCREMENTAL and parent_row is not None:
            parent_checkpoint_id = parent_row.checkpoint_id
            parent_source_hash = parent_row.source_hash
            parent_payload = verified_active[0]

        sources = self._gather_sources(
            db,
            keyring,
            session_id=session_id,
            mode=mode,
            parent_row=parent_row if mode is BuildMode.INCREMENTAL else None,
            parent_checkpoint_id=parent_checkpoint_id,
            parent_source_hash=parent_source_hash,
            parent_payload=parent_payload,
        )
        if not sources.events:
            return BuildResult(status="no_events")

        if self._provider is None:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail="compactor has no provider configured",
            )

        request = CompactorRequest(
            session_id=session_id,
            schema_version=SCHEMA_VERSION,
            compactor_version=COMPACTOR_VERSION,
            mode=mode,
            parent_checkpoint_id=(
                parent_checkpoint_id
                if mode is BuildMode.INCREMENTAL
                else None
            ),
            parent_source_hash=(
                parent_source_hash if mode is BuildMode.INCREMENTAL else None
            ),
            parent_checkpoint=parent_payload,
            raw_events=sources.events,
            operation_projections=sources.operations,
            covered_from_sequence=sources.covered_from_sequence,
            covered_through_sequence=sources.covered_through_sequence,
        )
        payload = self._call_provider(request)
        if payload is None:
            return BuildResult(status="provider_failed")
        if not isinstance(payload, dict):
            return BuildResult(status="provider_failed")

        expected_hash = compute_source_hash(
            parent_checkpoint_id=(
                parent_checkpoint_id
                if mode is BuildMode.INCREMENTAL
                else None
            ),
            parent_source_hash=(
                parent_source_hash if mode is BuildMode.INCREMENTAL else None
            ),
            events=sources.events,
            operations=sources.operations,
            schema_version=SCHEMA_VERSION,
            compactor_version=COMPACTOR_VERSION,
        )

        failure = validate_checkpoint(
            payload,
            sources=sources,
            expected_source_hash=expected_hash,
            waiting_operations=sources.operations,
        )
        if failure is not None:
            return BuildResult(status="validation_failed", failure=failure)

        if mode is not BuildMode.FIRST and parent_row is not None:
            fresh_status = db.execute(
                text(
                    "SELECT status FROM context_checkpoints "
                    "WHERE checkpoint_id = :cid"
                ),
                {"cid": parent_row.checkpoint_id},
            ).scalar_one_or_none()
            if fresh_status != "active":
                return BuildResult(status="stale_parent")

        supersede_id: str | None = None
        if mode is BuildMode.INCREMENTAL:
            supersede_id = parent_checkpoint_id
        elif mode is BuildMode.FULL_REBUILD and parent_row is not None:
            supersede_id = parent_row.checkpoint_id

        return self._commit_checkpoint(
            db,
            keyring,
            identifier_key,
            session_id=session_id,
            payload=payload,
            sources=sources,
            expected_hash=expected_hash,
            parent_checkpoint_id=(
                parent_checkpoint_id
                if mode is BuildMode.INCREMENTAL
                else None
            ),
            supersede_id=supersede_id,
            now=now,
        )

    def _call_provider(self, request: CompactorRequest) -> Any | None:
        """Call the non-deterministic boundary behind a hard wall-clock limit.

        A provider should still enforce its own HTTP timeout and cancellation.
        The daemon worker is the last-resort process boundary: even a broken
        adapter that never returns cannot hold the request open indefinitely.
        """
        assert self._provider is not None
        outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                outcome.put((True, self._provider.compact(request)))
            except BaseException as exc:
                outcome.put((False, exc))

        worker = threading.Thread(
            target=invoke,
            name="personal-agent-compactor-provider",
            daemon=True,
        )
        worker.start()
        worker.join(self._provider_timeout_seconds)
        if worker.is_alive():
            return None
        succeeded, value = outcome.get_nowait()
        return value if succeeded else None

    def _commit_checkpoint(
        self,
        db,
        keyring: KeyRing,
        identifier_key: HmacKey | HmacKeyRing,
        *,
        session_id: str,
        payload: dict[str, Any],
        sources: SourceBundle,
        expected_hash: str,
        parent_checkpoint_id: str | None,
        supersede_id: str | None,
        now: datetime,
    ) -> BuildResult:
        checkpoint_id = f"ckpt_{uuid.uuid4().hex}"
        encrypted_payload = keyring.encrypt(
            canonical_json(payload).encode("utf-8"),
            table=_TABLE,
            column=_COLUMN,
            row_id=checkpoint_id,
        )
        estimated_tokens = len(canonical_json(payload).encode("utf-8"))
        checkpoint = ContextCheckpoint(
            checkpoint_id=checkpoint_id,
            session_id=session_id,
            parent_checkpoint_id=parent_checkpoint_id,
            status="building",
            covered_from_sequence=sources.covered_from_sequence,
            covered_through_sequence=sources.covered_through_sequence,
            encrypted_payload=encrypted_payload,
            source_hash=expected_hash,
            schema_version=SCHEMA_VERSION,
            compactor_version=COMPACTOR_VERSION,
            estimated_tokens=estimated_tokens,
            created_at=now,
        )
        db.add(checkpoint)
        hmac_key = _hmac_key(identifier_key)
        for ordinal, event in enumerate(sources.events):
            db.add(
                ContextCheckpointSource(
                    checkpoint_id=checkpoint_id,
                    ordinal=ordinal,
                    source_type="event",
                    source_hmac=source_hmac(hmac_key, event.event_id),
                )
            )
        db.flush()

        try:
            with db.begin_nested():
                if supersede_id is not None:
                    db.execute(
                        text(
                            "UPDATE context_checkpoints SET status = 'superseded' "
                            "WHERE checkpoint_id = :sid AND status = 'active'"
                        ),
                        {"sid": supersede_id},
                    )
                checkpoint.status = "active"
                db.flush()
        except IntegrityError:
            checkpoint.status = "invalid"
            db.flush()
            return BuildResult(status="lost_race", checkpoint_id=checkpoint_id)

        return BuildResult(status="active", checkpoint_id=checkpoint_id)

    def _decide_build_mode(self, db, session_id: str) -> BuildMode:
        active = self._active_checkpoint_row(db, session_id)
        if active is None:
            return BuildMode.FIRST
        depth = 0
        cursor = active
        while cursor is not None and cursor.parent_checkpoint_id is not None:
            depth += 1
            cursor = db.get(ContextCheckpoint, cursor.parent_checkpoint_id)
        if depth >= self._config.full_rebuild_after_incrementals:
            return BuildMode.FULL_REBUILD
        return BuildMode.INCREMENTAL

    def _active_checkpoint_row(
        self, db, session_id: str
    ) -> ContextCheckpoint | None:
        return (
            db.query(ContextCheckpoint)
            .filter_by(session_id=session_id, status="active")
            .one_or_none()
        )

    def _decrypt_payload(
        self, keyring: KeyRing, row: ContextCheckpoint
    ) -> dict[str, Any]:
        plaintext = keyring.decrypt(
            row.encrypted_payload,
            table=_TABLE,
            column=_COLUMN,
            row_id=row.checkpoint_id,
        )
        return json.loads(plaintext.decode("utf-8"))

    def _gather_sources(
        self,
        db,
        keyring: KeyRing,
        *,
        session_id: str,
        mode: BuildMode,
        parent_row: ContextCheckpoint | None,
        parent_checkpoint_id: str | None,
        parent_source_hash: str | None,
        parent_payload: dict[str, Any] | None,
    ) -> SourceBundle:
        query = (
            db.query(ConversationEvent)
            .filter(
                ConversationEvent.session_id == session_id,
                ConversationEvent.event_type.in_(
                    events.MODEL_VISIBLE_EVENT_TYPES
                ),
            )
            .order_by(ConversationEvent.timeline_sequence)
        )
        if mode is BuildMode.INCREMENTAL and parent_row is not None:
            query = query.filter(
                ConversationEvent.timeline_sequence
                > parent_row.covered_through_sequence
            )
        event_rows = query.all()
        if not event_rows:
            return SourceBundle(
                session_id=session_id,
                events=(),
                operations=(),
                parent_checkpoint_id=parent_checkpoint_id,
                parent_source_hash=parent_source_hash,
                parent_payload=parent_payload,
                covered_from_sequence=0,
                covered_through_sequence=0,
                mode=mode,
            )

        sources = tuple(
            self._event_source(row, keyring) for row in event_rows
        )
        operation_ids = {
            operation_id
            for (operation_id,) in (
                db.query(ConversationEvent.operation_id)
                .filter(
                    ConversationEvent.session_id == session_id,
                    ConversationEvent.operation_id.is_not(None),
                )
                .distinct()
                .all()
            )
            if operation_id is not None
        }
        operations = self._operation_projections(db, operation_ids)

        if mode is BuildMode.INCREMENTAL and parent_row is not None:
            covered_from = parent_row.covered_from_sequence
        else:
            covered_from = sources[0].timeline_sequence
        covered_through = sources[-1].timeline_sequence

        return SourceBundle(
            session_id=session_id,
            events=sources,
            operations=operations,
            parent_checkpoint_id=parent_checkpoint_id,
            parent_source_hash=parent_source_hash,
            parent_payload=parent_payload,
            covered_from_sequence=covered_from,
            covered_through_sequence=covered_through,
            mode=mode,
        )

    def _operation_projections(
        self, db, operation_ids: set[str]
    ) -> tuple[OperationProjection, ...]:
        if not operation_ids:
            return ()
        op_rows = (
            db.query(Operation)
            .filter(Operation.operation_id.in_(operation_ids))
            .order_by(Operation.operation_id)
            .all()
        )
        return tuple(
            OperationProjection(
                operation_id=op.operation_id,
                state=op.state,
                state_version=op.state_version,
                tool=op.tool,
                record_id=extract_record_id(op.safe_result),
                duplicate_check_id=op.duplicate_check_id,
                safe_result=op.safe_result,
                idempotency_key=op.idempotency_key,
                failure_reason=op.failure_reason,
                cancel_requested=op.cancel_requested,
            )
            for op in op_rows
        )

    def _sources_for_stored_checkpoint(
        self,
        db,
        keyring: KeyRing,
        row: ContextCheckpoint,
    ) -> SourceBundle:
        parent = (
            db.get(ContextCheckpoint, row.parent_checkpoint_id)
            if row.parent_checkpoint_id is not None
            else None
        )
        lower_bound = (
            parent.covered_through_sequence + 1
            if parent is not None
            else row.covered_from_sequence
        )
        event_rows = (
            db.query(ConversationEvent)
            .filter(
                ConversationEvent.session_id == row.session_id,
                ConversationEvent.event_type.in_(events.MODEL_VISIBLE_EVENT_TYPES),
                ConversationEvent.timeline_sequence >= lower_bound,
                ConversationEvent.timeline_sequence <= row.covered_through_sequence,
            )
            .order_by(ConversationEvent.timeline_sequence)
            .all()
        )
        operation_ids = {
            operation_id
            for (operation_id,) in (
                db.query(ConversationEvent.operation_id)
                .filter(
                    ConversationEvent.session_id == row.session_id,
                    ConversationEvent.timeline_sequence
                    <= row.covered_through_sequence,
                    ConversationEvent.operation_id.is_not(None),
                )
                .distinct()
                .all()
            )
            if operation_id is not None
        }
        parent_payload = (
            self._decrypt_payload(keyring, parent) if parent is not None else None
        )
        return SourceBundle(
            session_id=row.session_id,
            events=tuple(
                self._event_source(event_row, keyring)
                for event_row in event_rows
            ),
            operations=self._operation_projections(db, operation_ids),
            parent_checkpoint_id=parent.checkpoint_id if parent is not None else None,
            parent_source_hash=parent.source_hash if parent is not None else None,
            parent_payload=parent_payload,
            covered_from_sequence=row.covered_from_sequence,
            covered_through_sequence=row.covered_through_sequence,
            mode=BuildMode.INCREMENTAL if parent is not None else BuildMode.FIRST,
        )

    def _event_source(
        self, row: ConversationEvent, keyring: KeyRing
    ) -> RawEventSource:
        envelope = row.encrypted_content
        plaintext = keyring.decrypt(
            envelope,
            table="conversation_events",
            column="encrypted_content",
            row_id=row.event_id,
        )
        return RawEventSource(
            event_id=row.event_id,
            timeline_sequence=row.timeline_sequence,
            event_type=row.event_type,
            content=json.loads(plaintext.decode("utf-8")),
            operation_id=row.operation_id,
            turn_id=row.turn_id,
            content_fingerprint=_event_content_fingerprint(envelope),
        )


def extract_record_id(safe_result: str | None) -> str | None:
    """The external record id, if the safe result carries one."""
    if not safe_result:
        return None
    candidate = safe_result.strip()
    return candidate if re.fullmatch(r"rec[A-Za-z0-9_-]+", candidate) else None


# -- deletion propagation (F-F13) ------------------------------------------


def invalidate_checkpoints_referencing(
    db, *, source_hmacs: list[str]
) -> int:
    """Flip active Checkpoints referencing deleted sources to `invalid`.

    Phase 1 retains events permanently, but this is the mechanism a Timeline
    deletion would call. It proves that old Checkpoints do not remain
    retrievable for text whose source events no longer exist.
    """
    if not source_hmacs:
        return 0
    placeholders = ", ".join(f":h{i}" for i in range(len(source_hmacs)))
    params = {f"h{i}": value for i, value in enumerate(source_hmacs)}
    result = db.execute(
        text(
            "WITH RECURSIVE affected(checkpoint_id) AS ("
            "  SELECT DISTINCT checkpoint_id "
            "  FROM context_checkpoint_sources "
            f"  WHERE source_hmac IN ({placeholders}) "
            "  UNION "
            "  SELECT child.checkpoint_id "
            "  FROM context_checkpoints AS child "
            "  JOIN affected AS parent "
            "    ON child.parent_checkpoint_id = parent.checkpoint_id"
            ") "
            "UPDATE context_checkpoints SET status = 'invalid' "
            "WHERE status <> 'invalid' "
            "AND checkpoint_id IN (SELECT checkpoint_id FROM affected) "
            "RETURNING checkpoint_id"
        ),
        params,
    )
    return len(result.all())
