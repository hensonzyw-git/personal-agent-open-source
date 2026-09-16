"""carry the written ledger row on a governed write, for the receipt card

`G1`. A write receipt has until now carried a `record_id` and nothing else, so
the iOS receipt card had no business fields to draw and fell back to its
lightest form -- `ChatView.receiptFields` returns `[]` with a comment pointing
at exactly this gap. The connector already returned the written row on its MCP
result; the Agent dropped it at the dispatcher.

Why a separate column rather than more content in `safe_result`:

- for a governed write `safe_result` **is** the external record id, and the
  whole receipt projection turns on reading it as one. Overloading it would put
  the strongest claim this system makes -- "this row exists in the ledger" --
  behind a parse;
- the two facts are not equally certain. The record id is proof of the write;
  these fields are presentation, and an `idempotent_replay` has the first
  without the second by contract.

Why it is sealed: this is the first place ledger content -- a name and an exact
amount -- would sit in this database as plaintext. `conversation_events` already
carries its copy inside `encrypted_content`, and the write audit's
`never_recorded` list forbids `exact_amount` outright. A clear column here would
be the single exposure the rest of the design took care to avoid.

Nullable with no default and no backfill: every existing row means "this write
predates the receipt fields", which is what NULL says. Reconstructing the fields
for historical writes would mean reading them out of Feishu now and presenting
them as what was written then -- a different claim entirely.

Revision ID: 0006_receipt_record_fields
Revises: 0005_finance_safe_retry
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

import personal_agent_core.sqlite


revision: str = "0006_receipt_record_fields"
down_revision: str | None = "0005_finance_safe_retry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: A record may only accompany a state that actually reached the ledger. It is
#: not a general annotation slot: a `failed_safe` or `cancelled_pre_submit` row
#: carrying business fields would render as a receipt for a write that never
#: happened. `needs_manual_review` is included because it can hold a record id
#: too -- the write may well have landed, which is precisely why a person is
#: being asked to look.
_ONLY_WHERE_A_RECORD_COULD_EXIST = (
    "encrypted_result_record IS NULL"
    " OR state IN ('succeeded', 'needs_manual_review')"
)


def upgrade() -> None:
    with op.batch_alter_table("operations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "encrypted_result_record",
                personal_agent_core.sqlite.EncryptedEnvelope(),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "result_record_only_where_written", _ONLY_WHERE_A_RECORD_COULD_EXIST
        )


def downgrade() -> None:
    with op.batch_alter_table("operations", schema=None) as batch_op:
        batch_op.drop_constraint("result_record_only_where_written", type_="check")
        batch_op.drop_column("encrypted_result_record")
