"""A transcript wrapper around the Finance dispatcher.

Wrapping rather than threading a recorder through the orchestrator keeps the
tool boundary recorded at its real edge: the arguments as dispatched and the
outcome object as returned, before any projection turns it into something the
client can render. When a query answers with the wrong shape, this is what says
whether the tool returned the wrong data or the projection lost it.

The wrapper is transparent. It returns what the dispatcher returned, re-raises
what it raised, and records failures as an outcome of their own so a raising
tool call does not leave a request with no matching result line.
"""

from __future__ import annotations

from typing import Any, Protocol

from personal_agent.diagnostics import transcript
from personal_agent.diagnostics.transcript import Recorder


class _Dispatcher(Protocol):
    """The orchestrator's dispatcher contract, duplicated to avoid a cycle."""

    def resolve(self, *, tool: str, model_args: dict[str, Any]) -> Any: ...

    def commit(
        self,
        *,
        intent: Any,
        idempotency_key: str,
        duplicate_override: str | None,
    ) -> Any: ...


class RecordingDispatcher:
    """Records every dispatched tool call and its outcome."""

    def __init__(self, inner: _Dispatcher, recorder: Recorder) -> None:
        self._inner = inner
        self._recorder = recorder

    def resolve(self, *, tool: str, model_args: dict[str, Any]) -> Any:
        self._recorder.record(
            transcript.TOOL_CALL,
            {"phase": "resolve", "tool": tool, "model_args": model_args},
        )
        try:
            outcome = self._inner.resolve(tool=tool, model_args=model_args)
        except Exception as exc:
            self._record_raised("resolve", tool, exc)
            raise
        self._record_outcome("resolve", tool, outcome)
        return outcome

    def commit(
        self,
        *,
        intent: Any,
        idempotency_key: str,
        duplicate_override: str | None,
    ) -> Any:
        tool = getattr(intent, "tool", None)
        self._recorder.record(
            transcript.TOOL_CALL,
            {
                "phase": "commit",
                "tool": tool,
                "intent": intent,
                "idempotency_key": idempotency_key,
                "duplicate_override": duplicate_override,
            },
        )
        try:
            outcome = self._inner.commit(
                intent=intent,
                idempotency_key=idempotency_key,
                duplicate_override=duplicate_override,
            )
        except Exception as exc:
            self._record_raised("commit", tool, exc)
            raise
        self._record_outcome("commit", tool, outcome)
        return outcome

    def _record_outcome(self, phase: str, tool: str | None, outcome: Any) -> None:
        self._recorder.record(
            transcript.TOOL_RESULT,
            {
                "phase": phase,
                "tool": tool,
                "outcome_type": type(outcome).__name__,
                "outcome": outcome,
            },
        )

    def _record_raised(self, phase: str, tool: str | None, error: Exception) -> None:
        self._recorder.record(
            transcript.TOOL_RESULT,
            {
                "phase": phase,
                "tool": tool,
                "outcome_type": "raised",
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )
