"""Structured input for one exact clarification continuation."""

from __future__ import annotations

from dataclasses import dataclass


MAX_CLARIFICATION_QUESTION_CHARS = 160


@dataclass(frozen=True)
class ClarificationExchange:
    """One answered clarification inside an unresolved turn."""

    question: str
    answer: str


@dataclass(frozen=True)
class ClarificationContext:
    """The exact unresolved transcript, separate from compressible history."""

    original_user_text: str
    question: str
    completed_exchanges: tuple[ClarificationExchange, ...] = ()
    source_operation_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class FinanceRetryContext:
    """Exact finance request whose previous attempt provably made no call."""

    original_user_text: str
    completed_exchanges: tuple[ClarificationExchange, ...] = ()
    source_operation_id: str = ""
    source_failure_reason: str = ""
