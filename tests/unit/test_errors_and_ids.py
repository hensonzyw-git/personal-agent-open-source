"""DEV-003: stable error codes cannot leak provider text; IDs are canonical."""

from __future__ import annotations

import json

import pytest

from personal_agent_core.errors import (
    ERROR_MESSAGES,
    RETRYABLE_CODES,
    AppError,
    ClarificationQuestion,
    ErrorCode,
)
from personal_agent_core.ids import (
    InvalidIdentifierError,
    is_uuid4,
    new_id,
    parse_uuid,
    require_uuid4,
)


VENDOR_BODY = (
    '{"code":91403,"msg":"Forbidden","data":{"app_token":"bascnSECRET",'
    '"table_id":"tblSECRET","record_id":"recSECRET"}}'
)


def test_every_code_has_an_outward_message() -> None:
    assert set(ERROR_MESSAGES) == set(ErrorCode)


def test_envelope_never_carries_the_internal_detail() -> None:
    error = AppError(
        ErrorCode.SOURCE_COMMIT_UNKNOWN, internal_detail=VENDOR_BODY
    )
    envelope = error.to_envelope(operation_id="op_1", trace_id="tr_1")
    serialised = json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False)

    assert envelope.message == ERROR_MESSAGES[ErrorCode.SOURCE_COMMIT_UNKNOWN]
    for leak in ("bascnSECRET", "tblSECRET", "recSECRET", "Forbidden", "91403"):
        assert leak not in serialised
    assert VENDOR_BODY not in str(error)


def test_the_outward_message_cannot_be_chosen_by_the_caller() -> None:
    # There is no message parameter at all: the catalogue is the only source.
    with pytest.raises(TypeError):
        AppError(ErrorCode.SOURCE_UNAVAILABLE, message="raw vendor text")  # type: ignore[call-arg]


def test_only_clean_unavailability_is_retryable() -> None:
    assert AppError(ErrorCode.SOURCE_UNAVAILABLE).retryable is True
    for code in (
        ErrorCode.SOURCE_COMMIT_UNKNOWN,
        ErrorCode.SOURCE_TIMEOUT_UNKNOWN,
        ErrorCode.SOURCE_COMMITTED_MISMATCH,
        ErrorCode.POSSIBLE_DUPLICATE,
    ):
        assert AppError(code).retryable is False
    assert RETRYABLE_CODES == {ErrorCode.SOURCE_UNAVAILABLE}


def test_envelope_rejects_unknown_fields() -> None:
    envelope = AppError(ErrorCode.SCOPE_DENIED).to_envelope()
    with pytest.raises(Exception):
        type(envelope)(
            code=ErrorCode.SCOPE_DENIED,
            message="x",
            retryable=False,
            vendor_body=VENDOR_BODY,
        )


def test_clarification_question_is_closed_and_never_uses_internal_detail() -> None:
    error = AppError(
        ErrorCode.CLARIFICATION_REQUIRED,
        internal_detail=VENDOR_BODY,
        clarification_question=ClarificationQuestion.EXPENSE_CATEGORY,
    )
    serialised = json.dumps(
        error.to_envelope().model_dump(mode="json"), ensure_ascii=False
    )

    assert ClarificationQuestion.EXPENSE_CATEGORY.value in serialised
    assert "bascnSECRET" not in serialised
    with pytest.raises(ValueError):
        AppError(
            ErrorCode.SOURCE_UNAVAILABLE,
            clarification_question=ClarificationQuestion.EXPENSE_CATEGORY,
        )


def test_new_ids_are_canonical_uuid4() -> None:
    value = new_id()
    assert is_uuid4(value)
    assert require_uuid4(value) == value
    assert value == value.lower()


@pytest.mark.parametrize(
    "value",
    [
        "018F0000-0000-7000-8000-000000000001",
        "{018f0000-0000-7000-8000-000000000001}",
        "urn:uuid:018f0000-0000-7000-8000-000000000001",
        "018f0000000070008000000000000001",
        "not-a-uuid",
        None,
    ],
)
def test_non_canonical_identifiers_are_rejected(value: object) -> None:
    with pytest.raises(InvalidIdentifierError):
        parse_uuid(value)


def test_non_v4_uuids_are_refused_for_write_keys() -> None:
    uuid_v1 = "018f0000-0000-1000-8000-000000000001"
    assert parse_uuid(uuid_v1).version == 1
    assert is_uuid4(uuid_v1) is False
    with pytest.raises(InvalidIdentifierError):
        require_uuid4(uuid_v1)
