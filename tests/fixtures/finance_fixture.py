"""Contract-derived MCP fixture data.

The spike's fixture server still advertises the pre-freeze shape: an `amount`
field instead of `input_amount`, no `entry_kind`, and a tool called
`meta.get_capabilities`. Testing the production path against it would prove the
wrong contract, so the fixture catalog is derived from the generated manifest
rather than written out again by hand. A fixture cannot drift from the contract
if it has no independent copy of it.

The transports that serve this catalog belong to DEV-010 and DEV-011. This
module only supplies the catalog and deterministic receipts.
"""

from __future__ import annotations

import hashlib
from typing import Any

from personal_agent_core.manifest import canonical_json, load_manifest


def fixture_catalog() -> list[dict[str, Any]]:
    """The tools a fixture server may advertise: enabled tools only.

    A disabled tool is absent from the catalog rather than present and refusing,
    so a fixture can never be the reason a disabled tool reaches the model.
    """
    manifest = load_manifest()
    return [
        {
            "name": entry["name"],
            "description": entry["summary"],
            "inputSchema": entry["model_input_schema"],
        }
        for entry in manifest["tools"]
        if entry["enabled"]
    ]


def fixture_record_id(tool_name: str, arguments: dict[str, Any]) -> str:
    """A stable fake external id.

    Deterministic so that a replayed call yields the same receipt, which is what
    lets idempotency tests distinguish a genuine replay from a second write.
    """
    digest = hashlib.sha256(
        canonical_json({"tool": tool_name, "arguments": arguments}).encode("utf-8")
    ).hexdigest()
    return f"fixture_{digest[:12]}"


def fixture_receipt(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """A structured receipt shaped like the contract's output schema."""
    record_id = fixture_record_id(tool_name, arguments)
    return {
        "status": "created",
        "record_id": record_id,
        "source_system": "feishu_bitable",
        "table": "支出记录",
        "committed_at": "2026-07-23T15:00:00+08:00",
        "record": dict(arguments),
        "evidence": {"kind": "feishu_record", "external_id": record_id},
    }
