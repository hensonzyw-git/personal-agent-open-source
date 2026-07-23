"""Shared deterministic primitives for the Agent API and the Finance MCP.

Both services must agree, byte for byte, on money, dates, identifiers, tool
contracts and error codes. Duplicating those rules per service is exactly the
drift the project forbids, so they live here once and are imported by both.

This package holds no business policy, no credentials and no I/O.
"""

__version__ = "0.1.0"
