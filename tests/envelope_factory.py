"""The `build_envelope` dependency for API tests, using the real builder.

`AgentApiDeps` requires an `EnvelopeFactory`, and these tests supply the
production `ContextBuilder` rather than a stub. Two reasons:

- a stub would let an API test pass while the real assembly refused, which is
  the class of gap `CLAUDE.md` §5.1 is about;
- it means every chat test in the suite drives the builder against a real
  Timeline, real Sessions and real persisted events for free.
"""

from __future__ import annotations

from typing import Any, Sequence

from cap001_fixtures import IDENTIFIER_KEY
from personal_agent.context.builder import ContextBuilder, ContextEnvelope
from personal_agent.context.compactor import Compactor
from personal_agent.context.config import ContextConfig, default_context_config
from personal_agent.context.continuation import (
    ClarificationContext,
    FinanceRetryContext,
)
from personal_agent.policy.bridge import VisibleTool
from personal_agent.runtime.model_input import InputPart
from personal_agent_core.crypto import KeyRing


TEST_SYSTEM_INSTRUCTION = "你是测试用的个人 Agent。"


def envelope_factory(
    keyring: KeyRing,
    *,
    tools: Sequence[VisibleTool] = (),
    config: ContextConfig | None = None,
    system: str = TEST_SYSTEM_INSTRUCTION,
):
    """Build the `AgentApiDeps.build_envelope` callable for a test service."""
    resolved = config or default_context_config()
    builder = ContextBuilder(resolved, compactor=Compactor(resolved))

    def build_envelope(
        session: Any,
        auth: Any,
        *,
        conversation_id: str,
        session_id: str,
        current_event_id: str,
        user_text: str,
        clarification_context: ClarificationContext | None,
        finance_retry_context: FinanceRetryContext | None,
        input_parts: tuple[InputPart, ...] = (),
    ) -> ContextEnvelope:
        return builder.build(
            session,
            keyring,
            IDENTIFIER_KEY,
            conversation_id=conversation_id,
            session_id=session_id,
            current_event_id=current_event_id,
            system_instruction=system,
            user_text=user_text,
            effective_tools=list(tools),
            clarification_context=clarification_context,
            finance_retry_context=finance_retry_context,
            input_parts=input_parts,
        )

    return build_envelope
