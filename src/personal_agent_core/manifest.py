"""Generate the trusted tool manifest and its schema hashes.

The governed bridge compares every discovered MCP tool against this manifest
before the model may see it. A server's own description and annotations are
untrusted metadata, so the comparison must be against something generated from
the IR rather than from the wire.

Two hashes, deliberately separate:

- `input_schema_hash` covers only the model-visible input schema. That is what an
  MCP server actually advertises, so it is the value the bridge can compare
  against a discovered tool;
- `contract_hash` covers the whole contract, including scopes, risk, idempotency
  and audit rules, and is what changes when the business contract changes.

Generation is deterministic: no timestamps, no environment, no ordering by hash
map. Regenerating must produce byte-identical output, otherwise a drift test
cannot mean anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Final

from personal_agent_core.tool_ir import (
    HOST_CONTEXT_SCHEMA,
    IR_VERSION,
    TOOL_CONTRACTS,
    ToolContract,
)


MANIFEST_PATH: Final[Path] = (
    Path(__file__).parent / "generated" / "tool_manifest.json"
)


def canonical_json(value: Any) -> str:
    """Stable serialisation used for every hash and for the artifact itself."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_of(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def contract_entry(contract: ToolContract) -> dict[str, Any]:
    """One manifest entry, including both hashes."""
    body = contract.model_dump(mode="json")
    return {
        **body,
        "input_schema_hash": sha256_of(contract.model_input_schema),
        "contract_hash": sha256_of(body),
    }


def build_manifest() -> dict[str, Any]:
    """Build the manifest in memory. Deterministic and side-effect free."""
    tools = [contract_entry(contract) for contract in TOOL_CONTRACTS]
    enabled = [tool["name"] for tool in tools if tool["enabled"]]
    return {
        "manifest_version": "1.0.0",
        "ir_version": IR_VERSION,
        "generated_from": [
            "docs/MCP工具IR_v0.1.md",
            "docs/Finance MCP工具设计草案_v0.1.md",
            "docs/Phase1技术方案_v0.1.md",
        ],
        "host_context_schema": HOST_CONTEXT_SCHEMA,
        "enabled_tools": enabled,
        "disabled_tools": [
            tool["name"] for tool in tools if not tool["enabled"]
        ],
        #: The subset of enabled tools a model may be offered. Everything else
        #: enabled is reachable only by a deterministic, device-authenticated
        #: route. Emitted explicitly so the allowlist the service builds is read
        #: from the signed artifact rather than re-derived from the IR by a
        #: second expression that could disagree with the first.
        "model_callable_tools": [
            tool["name"]
            for tool in tools
            if tool["enabled"] and tool["model_callable"]
        ],
        "allowed_tools_version": sha256_of(
            [
                {"name": tool["name"], "contract_hash": tool["contract_hash"]}
                for tool in tools
                if tool["enabled"]
            ]
        ),
        "tools": tools,
    }


def render_manifest() -> str:
    """The exact bytes of the checked-in artifact."""
    return json.dumps(build_manifest(), ensure_ascii=False, indent=2) + "\n"


def load_manifest() -> dict[str, Any]:
    """Read the checked-in artifact.

    Callers get the generated file rather than a fresh build so that a tampered
    artifact is caught by the drift test instead of being silently regenerated
    at runtime.
    """
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def write_manifest() -> Path:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(render_manifest(), encoding="utf-8")
    return MANIFEST_PATH


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate or verify the trusted tool manifest."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the checked-in artifact matches the IR, without writing",
    )
    args = parser.parse_args()

    expected = render_manifest()
    if args.check:
        if not MANIFEST_PATH.is_file():
            raise SystemExit(f"missing generated manifest: {MANIFEST_PATH}")
        if MANIFEST_PATH.read_text(encoding="utf-8") != expected:
            raise SystemExit(
                "tool manifest is out of date; "
                "run personal-agent-generate-contracts"
            )
        print(f"tool manifest is current: {MANIFEST_PATH}")
        return

    path = write_manifest()
    print(f"wrote {path}", file=sys.stdout)
