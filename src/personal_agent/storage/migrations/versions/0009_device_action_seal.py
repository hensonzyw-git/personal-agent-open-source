"""seal the issued device action on its operation, for the 202 delivery door

Review R6, 2026-09-08: the chat response was the device action's only delivery
channel, so a request that timed out at 202 handed the client an operation id
and an action nobody would ever deliver — the operation sat parked at
`source_in_progress` until the timeout sweep, with the authorised write lost.

The fix carries the action on the operation itself: `orchestrator._apply_resolve`
seals it into `encrypted_device_action` in the same committed transition that
steps the operation to `source_in_progress`, and `_operation_projection` hands
it over while the operation stays parked — so the 200 reply, the by-id poll and
a replay all converge on one delivery door, and a settled operation refuses to
hand the action over.

Sealed with the same AES-256-GCM envelope as `encrypted_result_record`: the
event fields are the user's personal schedule and the model-authorised intent,
and the operations table's first plaintext copy of personal content would be
the one exposure the rest of the design took care to avoid.

Nullable with no default and no backfill: every existing row predates device
actions entirely, which is exactly what NULL says. The CHECK constraint keeps
the column's meaning structural — an action may only sit on an operation
parked at `source_in_progress`, the one state whose meaning is "the write was
authorised and handed off" — so a future settlement path that forgot to clear
it fails loudly instead of silently re-arming a finished write.

Revision ID: 0009_device_action_seal
Revises: 0008_session_automatic_boundaries
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

import personal_agent_core.sqlite


revision: str = "0009_device_action_seal"
down_revision: str | None = "0008_session_automatic_boundaries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ONLY_WHILE_PARKED = (
    "encrypted_device_action IS NULL OR state = 'source_in_progress'"
)


def upgrade() -> None:
    with op.batch_alter_table("operations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "encrypted_device_action",
                personal_agent_core.sqlite.EncryptedEnvelope(),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "device_action_only_while_parked", _ONLY_WHILE_PARKED
        )


def downgrade() -> None:
    with op.batch_alter_table("operations", schema=None) as batch_op:
        batch_op.drop_constraint("device_action_only_while_parked", type_="check")
        batch_op.drop_column("encrypted_device_action")
