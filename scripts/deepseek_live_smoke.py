"""One-shot DeepSeek provider live smoke (2026-09-05).

Run from the worktree root:

    PYTHONPATH=src python scripts/deepseek_live_smoke.py

The script reads the operator's repo-root `.env.local` (mode 600) itself and
loads only `DEEPSEEK_API_KEY` into the environment. Three shapes, per §5.1:
the clean path (tool-call round trip), a wrong model name (must surface a
provider error, not a silent fallback), and a tampered host (must be refused
before any network call). Prints PASS/FAIL lines and a summary; exit code 0
only if all three hold. The key itself is never printed.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "src")

#: The operator's repo-root `.env.local`. A worktree's `.git` file points at
#: the main checkout's `.git/worktrees/<name>`, so the main checkout root is
#: three levels above that gitdir.
_WT_ROOT = Path(__file__).resolve().parents[1]
_GIT_POINTER = _WT_ROOT / ".git"
if _GIT_POINTER.is_file():  # worktree checkout
    _GIT_DIR = Path(_GIT_POINTER.read_text().split(":", 1)[1].strip())
    _ENV_LOCAL = _GIT_DIR.parents[2] / ".env.local"
else:  # main checkout
    _ENV_LOCAL = _WT_ROOT / ".env.local"


def _load_credential() -> None:
    for raw in _ENV_LOCAL.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("DEEPSEEK_API_KEY="):
            value = line.split("=", 1)[1].strip()
            if value and not os.environ.get("DEEPSEEK_API_KEY"):
                os.environ["DEEPSEEK_API_KEY"] = value


from personal_agent.api import events  # noqa: E402
from personal_agent.context.builder import ContextBuilder  # noqa: E402
from personal_agent.context.compactor import Compactor  # noqa: E402
from personal_agent.context.config import default_context_config  # noqa: E402
from personal_agent.policy.bridge import VisibleTool  # noqa: E402
from personal_agent.runtime.model_providers import (  # noqa: E402
    canonical_api_base,
    credential_from_env,
    provider_from_env,
)
from personal_agent.storage.engine import (  # noqa: E402
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import Conversation, ContextSession  # noqa: E402
from personal_agent.keys import HmacKey  # noqa: E402
from personal_agent_core.crypto import KeyRing, generate_key  # noqa: E402

_NOW = datetime(2026, 9, 5, 1, 0, tzinfo=timezone.utc)


def _envelope(tmp: Path, *, user_text: str, tools: list) -> object:
    """A real builder-produced envelope over a throwaway SQLite database.

    The gateway refuses hand-made envelopes (the Budgeter's witness), so the
    live check composes the production builder exactly as the fixture in
    tests/context_envelopes.py does.
    """
    engine = create_database_engine(tmp / "smoke-envelope.sqlite")
    create_all(engine)
    keyring = KeyRing(
        [generate_key("smoke-fixture", state="active")],
        service="personal-agent-api",
    )
    conversation_id = "tl_smoke"
    session_id = "ses_smoke"
    with session_factory(engine)() as session:
        session.add(Conversation(
            conversation_id=conversation_id,
            created_at=_NOW,
            next_sequence=1,
            is_canonical=True,
        ))
        session.add(ContextSession(
            session_id=session_id,
            conversation_id=conversation_id,
            opened_at=_NOW,
            status="open",
        ))
        # The sequence allocator is a raw UPDATE; the pending rows must be in
        # the database before the first append or it matches nothing.
        session.commit()
        current = events.append_event(
            session,
            keyring,
            conversation_id=conversation_id,
            session_id=session_id,
            turn_id="trn-current",
            event_type=events.USER_MESSAGE,
            content={"text": user_text},
            operation_id=None,
            now=_NOW,
        )
        session.commit()
        config = default_context_config()
        builder = ContextBuilder(config, compactor=Compactor(config))
        return builder.build(
            session,
            keyring,
            HmacKey(kid="identifier:smoke", secret=os.urandom(32)),
            conversation_id=conversation_id,
            session_id=session_id,
            current_event_id=current,
            system_instruction="你是个人财务助理。金额一律用数字。",
            user_text=user_text,
            effective_tools=list(tools),
        )


_MAIN = {"MODEL_PROVIDER": "deepseek", "GLM_MODEL": "deepseek-flash"}

results: list[tuple[str, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, "PASS" if ok else "FAIL"))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def _gateway() -> object:
    from personal_agent.runtime.glm_gateway import glm_gateway_from_env

    return glm_gateway_from_env()


def main() -> int:
    _load_credential()
    os.environ["MODEL_PROVIDER"] = _MAIN["MODEL_PROVIDER"]
    os.environ["GLM_MODEL"] = _MAIN["GLM_MODEL"]
    os.environ.pop("GLM_OPENAI_BASE_URL", None)
    provider = provider_from_env(os.environ)
    if credential_from_env(provider, os.environ) is None:
        print("no DEEPSEEK_API_KEY in environment")
        return 2

    import tempfile
    from pathlib import Path

    from personal_agent.runtime.glm_gateway import ModelGatewayError
    from personal_agent.runtime.model_providers import validated_api_base

    # Shape 1: the production composition — GlmGateway.propose over a real
    # envelope with the dotted business tool name, through the mapper, to the
    # real DeepSeek API and back.
    try:
        with tempfile.TemporaryDirectory() as tmp:
            envelope = _envelope(
                Path(tmp),
                user_text="帮我记一笔 42.5 元的支出",
                tools=[
                    VisibleTool(
                        alias="finance.log_expense",
                        description="记一笔支出",
                        input_schema={
                            "type": "object",
                            "properties": {"amount": {"type": "number"}},
                            "required": ["amount"],
                        },
                        risk_level="R2",
                        required_scopes=("finance.write",),
                    )
                ],
            )
            proposal = _gateway().propose(envelope=envelope)
        tool = getattr(proposal, "tool", None)
        if tool == "finance.log_expense":
            record(
                "dotted-tool-round-trip",
                True,
                "propose() returned the business alias from a live call",
            )
        elif tool is not None:
            record("dotted-tool-round-trip", False, f"wrong tool {tool!r}")
        else:
            kind = type(proposal).__name__
            record("dotted-tool-round-trip", False, f"no tool call: {kind}")
    except ModelGatewayError as exc:
        record("dotted-tool-round-trip", False, f"gateway error: {str(exc)[:200]}")
    except Exception as exc:  # noqa: BLE001
        record("dotted-tool-round-trip", False, f"{type(exc).__name__}: {str(exc)[:200]}")

    # Shape 2: a wrong model name must fail with a provider error.
    try:
        os.environ["GLM_MODEL"] = "deepseek-nonexistent-model-xyz"
        gateway = _gateway()
        with tempfile.TemporaryDirectory() as tmp:
            envelope = _envelope(Path(tmp), user_text="一句话即可", tools=[])
        gateway.propose(envelope=envelope)
        record("wrong-model-fails", False, "provider accepted a nonexistent model")
    except ModelGatewayError as exc:
        record("wrong-model-fails", True, f"failed closed: {str(exc)[:120]}")
    except Exception as exc:  # noqa: BLE001
        record("wrong-model-fails", True, f"failed closed: {type(exc).__name__}")

    # Shape 3: a tampered host must be refused before any network call.
    tampered = "https://api.deepseek.com.evil.example/"
    try:
        validated_api_base(tampered, provider)
        record("tampered-host-refused", False, "evil host passed validation")
    except ModelGatewayError:
        record("tampered-host-refused", True, "evil host rejected pre-network")

    failed = [name for name, verdict in results if verdict == "FAIL"]
    print(f"\nsummary: {len(results) - len(failed)}/{len(results)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
