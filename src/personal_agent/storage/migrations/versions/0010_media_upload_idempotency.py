"""add media_objects.client_request_id, the upload-create idempotency key

Design §5.2's create row takes an "上传幂等 key". The vocabulary already exists
in this schema: `api_requests` has carried `(device_id, client_request_id)`
since the first migration, and the header that fills it is read by
`idempotency_key(request)` on every mutating route. This column is that same
key, spelled the same way, so a client can be told "the same key from the same
device can only ever name one request" here too.

What it buys is narrow and worth stating precisely, because the other four
media endpoints are idempotent *through their state* and this one cannot be:
`PUT` refuses a live claim and takes over an expired one, `complete` returns the
same result once the object is `ready`, `DELETE` is a no-op on a tombstone. But
`POST` creates the object that all of those act on, so a lost response leaves
the client with no media id to retry *with* -- and without this column its only
recoveries are a second `pending` object (which expires and wastes a row) or a
guess at the id (which §5.4 forbids precisely because a guessed path must not
reach someone else's file).

A partial unique index, not a plain one. Most `media_objects` rows are created
by paths that have no client request at all -- a test, a restored database, a
future server-side producer -- and those must not collide on a shared NULL.
`api_requests` needs no such qualifier because every row there comes from a
request by construction; this table is the one where the key is optional.

The revision number is whatever this file's name says once it merges; see
`0009_media_objects` for why that is stated rather than assumed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_media_upload_idempotency"
down_revision: str | None = "0009_media_objects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, and no server default: an absent key means "this object was not
    # created by a keyed request", which is a different statement from the empty
    # string and must not be conflated with one.
    op.add_column(
        "media_objects",
        sa.Column("client_request_id", sa.Text(), nullable=True),
    )
    # The name and the two columns match `models.py` byte for byte. A migrated
    # database and a `create_all` one would otherwise disagree about which
    # requests are the same request, and that disagreement would only ever show
    # up as a duplicate media object.
    op.create_index(
        "uq_media_objects_client_request",
        "media_objects",
        ["device_id", "client_request_id"],
        unique=True,
        sqlite_where=sa.text("client_request_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Drops the index with the column, but the index is named explicitly above
    # so the drop order is not left to be inferred from a cascade.
    op.drop_index("uq_media_objects_client_request", table_name="media_objects")
    op.drop_column("media_objects", "client_request_id")
