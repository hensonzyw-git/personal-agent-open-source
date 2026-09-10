"""retain the authorised request and the device's report, for 「仍要创建」

Design 3.3 (review R1-F4). A calendar write that the phone refused to duplicate
is not a failure the user can only accept: the receipt carries a 「仍要创建」
button. That decision cannot be a new conversation turn -- a model turn would
re-derive the arguments, and a double tap or a retry whose response was lost
would produce two independent actions and two real events. So the decision is a
server-side binding: the endpoint derives one deterministic operation from the
original, and the write resumes from what was originally authorised, not from
anything re-asked.

Three columns make that possible, and each is the reason for one of them:

- `encrypted_request` -- the resolved tool call (the *attested* arguments, after
  Host-only stripping and schema validation), sealed on the same transition that
  issues the device action. It is the raw material an override resumes from. It
  **survives settlement**, which is what separates it from the device-action
  seal beside it: a duplicate is only discovered once the phone has reported,
  and by then `encrypted_device_action` has already been cleared. It is not
  `api_requests.encrypted_request_payload` either -- that column holds the chat
  request the turn replays, and overwriting it would cost the request's own
  idempotent replay.

  Sealed for the same reason as every other `encrypted_*` column here: the
  arguments name the user's calendar, title and time, which is personal
  content, and bound by AAD to the operation that holds them so a ciphertext
  cannot be lifted onto another row.

- `parent_operation_id` -- the operation an override was derived from. It is
  the durable audit trail that makes the override visible as *two* operations
  rather than one silent pass. The unique constraint is the structural form of
  design 3.3's promise "一次 duplicate → 至多一条派生 operation": today the
  derived key is `uuid5(namespace, "<parent>:calendar-override")` and could not
  produce two children, but the promise is what a double tap depends on, and it
  should not rest on a derivation someone may later change.

- `device_result` -- the phone's report verbatim (`created`/`duplicate`/
  `denied`/`failed`). The override endpoint admits exactly one case: a
  `calendar.create_event` that succeeded *because the event was already there*.
  Both success reports settle to `succeeded` with the EventKit id in
  `safe_result`, so without this column "was it a duplicate?" is not decidable
  from the row at all -- and a 「仍要创建」 offered on a genuinely created event
  would write a second copy. Storing the report itself, rather than a boolean
  derived from it, keeps the row an honest record of what the device testified.

Nullable with no default and no backfill: existing rows predate device actions,
predate overrides, and carry no report -- which is what NULL says.

Revision ID: 0010_calendar_override
Revises: 0009_device_action_seal
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

import personal_agent_core.sqlite


revision: str = "0010_calendar_override"
down_revision: str | None = "0009_device_action_seal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("operations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "encrypted_request",
                personal_agent_core.sqlite.EncryptedEnvelope(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("parent_operation_id", sa.Text(), nullable=True)
        )
        batch_op.add_column(sa.Column("device_result", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            "parent_operation",
            "operations",
            ["parent_operation_id"],
            ["operation_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "parent_operation_derives_once", ["parent_operation_id"]
        )
        batch_op.create_check_constraint(
            "parent_operation_is_not_self",
            "parent_operation_id IS NULL OR "
            "parent_operation_id <> operation_id",
        )
        batch_op.create_check_constraint(
            "device_result",
            "device_result IS NULL OR device_result IN "
            "('created', 'duplicate', 'denied', 'failed')",
        )


def downgrade() -> None:
    with op.batch_alter_table("operations", schema=None) as batch_op:
        batch_op.drop_constraint("device_result", type_="check")
        batch_op.drop_constraint("parent_operation_is_not_self", type_="check")
        batch_op.drop_constraint(
            "parent_operation_derives_once", type_="unique"
        )
        batch_op.drop_constraint("parent_operation", type_="foreignkey")
        batch_op.drop_column("device_result")
        batch_op.drop_column("parent_operation_id")
        batch_op.drop_column("encrypted_request")
