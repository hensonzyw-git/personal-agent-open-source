"""The full-fidelity turn transcript, and the boundaries it must not break.

The sink touches a filesystem, arbitrary provider objects and the environment's
credentials, so the cases here are written from the failure shapes first
(AGENTS.md §5.1): an unwritable directory, an object that cannot be serialised,
a `__repr__` that raises, a secret buried in a nested payload, concurrent
writers, and a provider call that fails before it ever returns.

The single property every one of them checks is the same: **recording never
changes what the system does.** A transcript that could refuse a turn would be
a worse defect than the blindness it removes.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_agent.diagnostics import transcript
from personal_agent.diagnostics.recording_dispatcher import RecordingDispatcher
from personal_agent.diagnostics.transcript import (
    DIRECTORY_ENV,
    REDACTED,
    RETENTION_ENV,
    NullRecorder,
    TranscriptRecorder,
    TurnIdentity,
    jsonable,
    recorder_from_env,
    secrets_from_environment,
)

_NOW = datetime(2026, 8, 14, 9, 30, tzinfo=timezone.utc)


def _recorder(tmp_path: Path, **kwargs) -> TranscriptRecorder:
    kwargs.setdefault("service", "api")
    kwargs.setdefault("now", lambda: _NOW)
    return TranscriptRecorder(tmp_path / "transcript", **kwargs)


def _lines(recorder: TranscriptRecorder) -> list[dict]:
    files = sorted(recorder.directory.glob("*.jsonl"))
    return [
        json.loads(line)
        for path in files
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


# --- the switch --------------------------------------------------------------


def test_no_directory_configured_records_nothing(tmp_path: Path) -> None:
    recorder = recorder_from_env(service="api", environ={})
    assert isinstance(recorder, NullRecorder)
    # The disabled shape still has to satisfy the whole seam, or a call site
    # would work in production and fail in a default deployment.
    with recorder.turn(TurnIdentity(operation_id="op-1")):
        recorder.record(transcript.USER_MESSAGE, {"text": "hi"})
    assert list(tmp_path.iterdir()) == []


def test_directory_configured_enables_recording(tmp_path: Path) -> None:
    recorder = recorder_from_env(
        service="api",
        environ={DIRECTORY_ENV: str(tmp_path / "t")},
        now=lambda: _NOW,
    )
    assert isinstance(recorder, TranscriptRecorder)
    recorder.record(transcript.USER_MESSAGE, {"text": "hi"})
    assert len(_lines(recorder)) == 1


def test_malformed_retention_is_a_startup_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        recorder_from_env(
            service="api",
            environ={DIRECTORY_ENV: str(tmp_path), RETENTION_ENV: "forever"},
        )


def test_non_positive_retention_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _recorder(tmp_path, retention_days=0)


# --- what a record contains --------------------------------------------------


def test_record_carries_the_turn_identity(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    identity = TurnIdentity(
        operation_id="op-1",
        trace_id="tr-1",
        turn_id="trn-1",
        session_id="ses-1",
        conversation_id="cnv-1",
        device_id="dev-1",
    )
    with recorder.turn(identity):
        recorder.record(transcript.MODEL_REQUEST, {"messages": [{"role": "user"}]})

    (record,) = _lines(recorder)
    assert record["kind"] == transcript.MODEL_REQUEST
    assert record["service"] == "api"
    assert record["recorded_at"] == _NOW.isoformat()
    assert record["turn"]["operation_id"] == "op-1"
    assert record["turn"]["device_id"] == "dev-1"
    assert record["payload"]["messages"] == [{"role": "user"}]


def test_turn_identity_does_not_leak_past_its_block(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    with recorder.turn(TurnIdentity(operation_id="op-1")):
        recorder.record(transcript.USER_MESSAGE, {"text": "inside"})
    recorder.record(transcript.USER_MESSAGE, {"text": "outside"})

    inside, outside = _lines(recorder)
    assert inside["turn"]["operation_id"] == "op-1"
    # A worker thread is reused between messages. A stale identity would file
    # one message's records under the previous message's operation.
    assert outside["turn"] is None


def test_records_are_one_json_object_per_line(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    recorder.record(transcript.USER_MESSAGE, {"text": "多行\n文本"})
    recorder.record(transcript.TURN_RESULT, {"state": "succeeded"})

    path = next(recorder.directory.glob("*.jsonl"))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["payload"]["text"] == "多行\n文本"


def test_chinese_text_is_not_escaped(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    recorder.record(transcript.USER_MESSAGE, {"text": "我今年打网球花了多少钱"})
    path = next(recorder.directory.glob("*.jsonl"))
    # The file has to be readable with `grep`, which is the whole reason it is
    # plaintext rather than sealed like `conversation_events`.
    assert "我今年打网球花了多少钱" in path.read_text(encoding="utf-8")


def test_file_and_directory_are_owner_only(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    recorder.record(transcript.USER_MESSAGE, {"text": "hi"})
    path = next(recorder.directory.glob("*.jsonl"))
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert oct(recorder.directory.stat().st_mode)[-3:] == "700"


def test_pre_existing_file_permissions_are_repaired(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    path = recorder.directory / "api-2026-08-14.jsonl"
    path.touch(mode=0o644)
    recorder.record(transcript.USER_MESSAGE, {"text": "hi"})
    assert oct(path.stat().st_mode)[-3:] == "600"


# --- the disabled shape costs nothing ----------------------------------------


def test_enabled_distinguishes_the_two_shapes(tmp_path: Path) -> None:
    assert NullRecorder().enabled is False
    assert _recorder(tmp_path).enabled is True


def test_a_disabled_transcript_opens_no_database_session() -> None:
    """The early return in the API's response recorder, at its own seam.

    `_record_operation_http_response` re-reads the operation and its anchor to
    build an identity. With recording off there is nobody to hand that identity
    to, and a default deployment would pay for two reads per polled response.
    """
    from fastapi.responses import JSONResponse

    from personal_agent.api.app import _record_operation_http_response

    opened: list[int] = []

    def session_factory():
        opened.append(1)
        raise AssertionError("a disabled transcript must not read the database")

    deps = SimpleNamespace(recorder=NullRecorder(), session_factory=session_factory)
    response = JSONResponse({"state": "succeeded"}, status_code=200)

    returned = _record_operation_http_response(
        deps, "op-1", "dev-1", response, delivery="operation_poll"
    )

    assert returned is response
    assert opened == []


# --- the leak check ----------------------------------------------------------


def test_secret_in_a_nested_payload_is_scrubbed(tmp_path: Path) -> None:
    secret = "zai-key-0123456789abcdef"
    recorder = _recorder(tmp_path, secrets=frozenset({secret}))
    recorder.record(
        transcript.MODEL_REQUEST,
        {"headers": {"authorization": f"Bearer {secret}"}, "model": "glm-5.3-flash"},
    )
    (record,) = _lines(recorder)
    assert secret not in json.dumps(record)
    assert REDACTED in record["payload"]["headers"]["authorization"]
    assert record["payload"]["model"] == "glm-5.3-flash"


def test_secret_is_scrubbed_in_its_json_escaped_form(tmp_path: Path) -> None:
    # A credential containing a quote reaches the file escaped. Scrubbing only
    # the raw form would leave the escaped one on disk.
    secret = 'key"with\\quote-0123'
    recorder = _recorder(tmp_path, secrets=frozenset({secret}))
    recorder.record(transcript.MODEL_REQUEST, {"note": f"sent {secret} upstream"})
    raw = next(recorder.directory.glob("*.jsonl")).read_text(encoding="utf-8")
    assert json.dumps(secret)[1:-1] not in raw
    assert REDACTED in raw


def test_overlapping_secrets_are_fully_scrubbed(tmp_path: Path) -> None:
    # The shorter value is a prefix of the longer one. Scrubbing shortest-first
    # would leave the longer secret's tail in the file.
    short = "abcdefgh12"
    long = "abcdefgh12345678"
    recorder = _recorder(tmp_path, secrets=frozenset({short, long}))
    recorder.record(transcript.MODEL_REQUEST, {"note": long})
    raw = next(recorder.directory.glob("*.jsonl")).read_text(encoding="utf-8")
    assert long not in raw
    assert "345678" not in raw


def test_secrets_are_collected_by_name_pattern() -> None:
    found = secrets_from_environment(
        {
            "ZAI_API_KEY": "zai-0123456789",
            "PERSONAL_AGENT_DATA_ACTIVE_KEY": "data-0123456789",
            "SOME_SECRET": "s3cret-value",
            "PERSONAL_AGENT_USER_ID": "henson",
            "SHORT_TOKEN": "abc",
        }
    )
    assert "zai-0123456789" in found
    assert "data-0123456789" in found
    assert "s3cret-value" in found
    # Not credential-shaped by name, and too short to scrub safely by value.
    assert "henson" not in found
    assert "abc" not in found


def test_environment_secrets_reach_the_env_built_recorder(tmp_path: Path) -> None:
    recorder = recorder_from_env(
        service="api",
        environ={DIRECTORY_ENV: str(tmp_path / "t"), "ZAI_API_KEY": "zai-0123456789"},
        now=lambda: _NOW,
    )
    recorder.record(transcript.MODEL_REQUEST, {"note": "key zai-0123456789 used"})
    raw = next(recorder.directory.glob("*.jsonl")).read_text(encoding="utf-8")
    assert "zai-0123456789" not in raw


# --- rendering arbitrary objects ---------------------------------------------


class _Unserializable:
    def __init__(self) -> None:
        self.value = object()


class _Exploding:
    def __repr__(self) -> str:
        raise RuntimeError("repr exploded")


def test_unknown_objects_are_marked_not_dropped(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    recorder.record(transcript.MODEL_RESPONSE, {"raw": _Unserializable()})
    (record,) = _lines(recorder)
    # Explicit about what could not be rendered. A silently missing field would
    # read as "the provider sent nothing", which is a different bug.
    assert "__repr__" in record["payload"]["raw"]


def test_a_raising_repr_still_produces_a_record(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    recorder.record(transcript.MODEL_RESPONSE, {"raw": _Exploding()})
    (record,) = _lines(recorder)
    assert "unrenderable" in record["payload"]["raw"]["__repr__"]


def test_bytes_are_recorded_by_length_only(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    recorder.record(transcript.MODEL_RESPONSE, {"raw": b"secret-bytes"})
    (record,) = _lines(recorder)
    assert record["payload"]["raw"] == {"__bytes__": 12}


def test_pydantic_objects_are_rendered_in_full() -> None:
    from google.genai import types

    part = types.Part(text="hello")
    assert jsonable(part)["text"] == "hello"


def test_dataclasses_and_enums_render_as_data(tmp_path: Path) -> None:
    from personal_agent_core.errors import ModelFailureReason

    recorder = _recorder(tmp_path)
    recorder.record(
        transcript.MODEL_FAILURE,
        {"reason": ModelFailureReason.UNAVAILABLE, "turn": TurnIdentity(trace_id="t")},
    )
    (record,) = _lines(recorder)
    assert record["payload"]["reason"] == ModelFailureReason.UNAVAILABLE.value
    assert record["payload"]["turn"]["trace_id"] == "t"


# --- failure of the sink itself ----------------------------------------------


def test_an_unwritable_directory_does_not_raise(tmp_path: Path, caplog) -> None:
    recorder = _recorder(tmp_path)
    recorder.directory.chmod(0o500)
    try:
        recorder.record(transcript.USER_MESSAGE, {"text": "hi"})
    finally:
        recorder.directory.chmod(0o700)
    # Dropped, and said so. A silent drop would make a missing turn in the file
    # indistinguishable from a turn that never ran.
    assert "transcript record dropped" in caplog.text


def test_a_directory_that_cannot_be_created_fails_at_construction(
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    with pytest.raises(OSError):
        TranscriptRecorder(blocked / "inner", service="api", now=lambda: _NOW)


def test_concurrent_writers_produce_whole_lines(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    payload = {"text": "x" * 20_000}
    barrier = threading.Barrier(8)

    def write(index: int) -> None:
        barrier.wait()
        with recorder.turn(TurnIdentity(operation_id=f"op-{index}")):
            for _ in range(10):
                recorder.record(transcript.USER_MESSAGE, payload)

    threads = [threading.Thread(target=write, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    records = _lines(recorder)
    assert len(records) == 80
    assert {record["turn"]["operation_id"] for record in records} == {
        f"op-{index}" for index in range(8)
    }


def test_short_os_writes_are_completed(tmp_path: Path, monkeypatch) -> None:
    recorder = _recorder(tmp_path)
    real_write = transcript.os.write
    calls = 0

    def short_write(handle, data):
        nonlocal calls
        calls += 1
        return real_write(handle, data[: max(1, len(data) // 3)])

    monkeypatch.setattr(transcript.os, "write", short_write)
    recorder.record(transcript.USER_MESSAGE, {"text": "x" * 20_000})

    assert calls > 1
    assert _lines(recorder)[0]["payload"]["text"] == "x" * 20_000


# --- retention ---------------------------------------------------------------


def test_files_past_the_window_are_deleted(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path, retention_days=2)
    stale = recorder.directory / "api-2026-08-12.jsonl"
    recent = recorder.directory / "api-2026-08-13.jsonl"
    foreign = recorder.directory / "notes.txt"
    for path in (stale, recent, foreign):
        path.write_text("{}\n")

    recorder.record(transcript.USER_MESSAGE, {"text": "hi"})

    assert not stale.exists()
    assert recent.exists()
    # Retention deletes only files this recorder wrote. Anything else in the
    # directory belongs to whoever put it there.
    assert foreign.exists()


def test_retention_sweeps_other_services_files(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path, retention_days=2)
    other = recorder.directory / "review-2026-08-01.jsonl"
    other.write_text("{}\n")
    recorder.record(transcript.USER_MESSAGE, {"text": "hi"})
    # The review job may not run for weeks; the API is then the only process
    # that can expire its files.
    assert not other.exists()


def test_retention_runs_once_per_day(tmp_path: Path) -> None:
    moment = _NOW
    recorder = TranscriptRecorder(
        tmp_path / "t", service="api", retention_days=2, now=lambda: moment
    )
    recorder.record(transcript.USER_MESSAGE, {"text": "day one"})
    late = recorder.directory / "api-2026-08-01.jsonl"
    late.write_text("{}\n")
    recorder.record(transcript.USER_MESSAGE, {"text": "still day one"})
    assert late.exists()

    moment = _NOW + timedelta(days=1)
    recorder.record(transcript.USER_MESSAGE, {"text": "day two"})
    assert not late.exists()


# --- the dispatcher wrapper --------------------------------------------------


class _FakeDispatcher:
    def __init__(self, outcome=None, error: Exception | None = None) -> None:
        self.outcome = outcome
        self.error = error
        self.calls: list[tuple] = []

    def resolve(self, *, tool: str, model_args: dict, idempotency_key: str | None = None):
        self.calls.append(("resolve", tool, model_args))
        if self.error is not None:
            raise self.error
        return self.outcome

    def commit(self, *, intent, idempotency_key: str, duplicate_override):
        self.calls.append(("commit", intent, idempotency_key, duplicate_override))
        if self.error is not None:
            raise self.error
        return self.outcome


def test_dispatcher_wrapper_is_transparent(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    inner = _FakeDispatcher(outcome=SimpleNamespace(kind="read"))
    wrapped = RecordingDispatcher(inner, recorder)

    outcome = wrapped.resolve(tool="finance.query_expenses", model_args={"period": "y"})

    assert outcome is inner.outcome
    assert inner.calls == [("resolve", "finance.query_expenses", {"period": "y"})]
    call, result = _lines(recorder)
    assert call["kind"] == transcript.TOOL_CALL
    assert call["payload"]["model_args"] == {"period": "y"}
    assert result["kind"] == transcript.TOOL_RESULT
    assert result["payload"]["outcome_type"] == "SimpleNamespace"


def test_dispatcher_wrapper_records_a_raising_tool(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    inner = _FakeDispatcher(error=RuntimeError("connector down"))
    wrapped = RecordingDispatcher(inner, recorder)

    with pytest.raises(RuntimeError):
        wrapped.resolve(tool="finance.query_expenses", model_args={})

    # A request with no matching result line would read as a hung tool call.
    _, result = _lines(recorder)
    assert result["payload"]["outcome_type"] == "raised"
    assert result["payload"]["error_type"] == "RuntimeError"


def _gateway(tmp_path: Path, generate, recorder):
    from personal_agent.runtime.glm_gateway import GlmGateway

    return GlmGateway(
        model="openai/glm-5.3-flash",
        api_key="zai-secret-0123456789",
        generate=generate,
        recorder=recorder,
    )


def test_model_request_is_recorded_before_the_provider_is_called(
    tmp_path: Path,
) -> None:
    from context_envelopes import envelope_for
    from personal_agent.runtime.model_gateway import ModelGatewayError

    recorder = _recorder(tmp_path)

    def generate(**kwargs):
        raise ModelGatewayError("connection reset")

    gateway = _gateway(tmp_path, generate, recorder)
    with pytest.raises(ModelGatewayError):
        gateway.propose(envelope=envelope_for(tmp_path))

    request, failure = _lines(recorder)
    # A request that never came back is exactly the case with nothing else to
    # look at, so the input has to be on disk before the call, not after it.
    assert request["kind"] == transcript.MODEL_REQUEST
    assert request["payload"]["messages"][-1]["content"].endswith("午饭 45 个人支出")
    assert request["payload"]["system_instruction"] == "SYS"
    assert failure["kind"] == transcript.MODEL_FAILURE
    assert failure["payload"]["phase"] == "provider_call"


def test_the_api_key_never_reaches_the_transcript(tmp_path: Path) -> None:
    from context_envelopes import envelope_for
    from personal_agent.runtime.model_gateway import ModelGatewayError

    recorder = _recorder(tmp_path, secrets=frozenset({"zai-secret-0123456789"}))

    def generate(**kwargs):
        # A provider that echoes the credential back in its error message is
        # the shape that would otherwise put it on disk.
        raise ModelGatewayError("bad key zai-secret-0123456789")

    gateway = _gateway(tmp_path, generate, recorder)
    with pytest.raises(ModelGatewayError):
        gateway.propose(envelope=envelope_for(tmp_path))

    raw = next(recorder.directory.glob("*.jsonl")).read_text(encoding="utf-8")
    assert "zai-secret-0123456789" not in raw
    assert REDACTED in raw


def test_an_unparseable_response_is_recorded_before_it_is_rejected(
    tmp_path: Path,
) -> None:
    from context_envelopes import envelope_for
    from personal_agent.runtime.model_gateway import ModelGatewayError

    recorder = _recorder(tmp_path)
    gateway = _gateway(
        tmp_path, lambda **kwargs: SimpleNamespace(content=None), recorder
    )

    with pytest.raises(ModelGatewayError):
        gateway.propose(envelope=envelope_for(tmp_path))

    kinds = [record["kind"] for record in _lines(recorder)]
    # The rejected response is the one whose exact shape has to be inspectable.
    assert kinds == [
        transcript.MODEL_REQUEST,
        transcript.MODEL_RESPONSE,
        transcript.MODEL_FAILURE,
    ]


def test_a_successful_response_is_recorded_unparsed(tmp_path: Path) -> None:
    from google.genai import types
    from context_envelopes import envelope_for

    recorder = _recorder(tmp_path)
    response = SimpleNamespace(
        content=types.Content(role="model", parts=[types.Part(text="你好")])
    )
    gateway = _gateway(tmp_path, lambda **kwargs: response, recorder)

    gateway.propose(envelope=envelope_for(tmp_path))

    _, recorded = _lines(recorder)
    assert recorded["kind"] == transcript.MODEL_RESPONSE
    assert "你好" in json.dumps(recorded["payload"]["raw"], ensure_ascii=False)


def test_structured_model_request_and_raw_response_are_recorded(tmp_path: Path) -> None:
    from personal_agent.runtime.structured import StructuredModelClient, StructuredRequest

    recorder = _recorder(tmp_path)
    response = SimpleNamespace(
        error_code=None,
        partial=False,
        interrupted=False,
        content=SimpleNamespace(
            parts=[
                SimpleNamespace(
                    text=None,
                    thought=False,
                    function_call=SimpleNamespace(
                        name="session_boundary_decision",
                        args={"decision": "continue_session"},
                    ),
                )
            ]
        ),
    )
    client = StructuredModelClient(
        model="openai/glm-5.3-flash",
        api_key="zai-secret-0123456789",
        input_budget_tokens=10_000,
        generate=lambda **kwargs: response,
        recorder=recorder,
        purpose="session_classifier",
    )

    result = client.call(
        StructuredRequest(
            system="classify",
            user_content="完整用户原文",
            function_name="session_boundary_decision",
            parameters_schema={"type": "object"},
        )
    )

    request, recorded = _lines(recorder)
    assert result == {"decision": "continue_session"}
    assert request["payload"]["purpose"] == "session_classifier"
    assert request["payload"]["messages"][0]["content"] == "完整用户原文"
    assert recorded["kind"] == transcript.MODEL_RESPONSE
    assert "continue_session" in json.dumps(recorded["payload"]["raw"])


def test_structured_model_failure_is_recorded(tmp_path: Path) -> None:
    from personal_agent.runtime.structured import (
        StructuredCallError,
        StructuredModelClient,
        StructuredRequest,
    )

    recorder = _recorder(tmp_path)

    def fail(**kwargs):
        raise RuntimeError("provider unavailable")

    client = StructuredModelClient(
        model="openai/glm-5.3-flash",
        api_key="zai-secret-0123456789",
        input_budget_tokens=10_000,
        generate=fail,
        recorder=recorder,
        purpose="compactor",
    )

    with pytest.raises(StructuredCallError):
        client.call(
            StructuredRequest(
                system="compact",
                user_content="source",
                function_name="context_checkpoint",
                parameters_schema={"type": "object"},
            )
        )

    request, failure = _lines(recorder)
    assert request["kind"] == transcript.MODEL_REQUEST
    assert failure["kind"] == transcript.MODEL_FAILURE
    assert failure["payload"]["purpose"] == "compactor"


def test_dispatcher_wrapper_records_commit_arguments(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    inner = _FakeDispatcher(outcome=SimpleNamespace(record_id="rec-1"))
    wrapped = RecordingDispatcher(inner, recorder)

    wrapped.commit(
        intent=SimpleNamespace(tool="finance.log_expense", amount="45"),
        idempotency_key="idem-1",
        duplicate_override=None,
    )

    call, _ = _lines(recorder)
    assert call["payload"]["phase"] == "commit"
    assert call["payload"]["tool"] == "finance.log_expense"
    assert call["payload"]["idempotency_key"] == "idem-1"
