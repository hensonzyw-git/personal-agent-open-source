"""Writing a deletion-manifest entry, and the AAD that binds it.

Design §6: a deletion "在事务内标记 deleting、写 manifest、提交". The manifest
is what stops a restore from resurrecting something the user removed, and its
whole value depends on the entry being written *in the same transaction* as the
state change -- an entry written afterwards can be lost to a crash in between,
and the deletion then has nothing to replay it.

This module exists at the storage layer because two callers need it and neither
may own it: the media state machine marks an image deleted, and the backup
replay applies deletions. :mod:`personal_agent.backup.deletion_manifest` keeps
its names by re-exporting them, so the AAD a value was sealed under and the AAD
it is opened under can never come from two different definitions -- the failure
that would cause is a decryption error at restore time, on the one code path
that has to work when everything else has already gone wrong.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import insert
from sqlalchemy.orm import Session

from personal_agent_core.crypto import KeyRing
from personal_agent_core.ids import new_id
from personal_agent.storage.models import DeletionManifest


#: The table and column the sealed object id is bound to. The AAD must match
#: what sealed it, or decryption fails closed -- this is the binding that makes
#: a manifest entry copied into another table, or replayed against a different
#: row, refuse rather than open.
MANIFEST_TABLE = "deletion_manifest"
MANIFEST_COLUMN = "encrypted_object_id"


def write_manifest_entry(
    session: Session,
    *,
    object_type: str,
    object_id: str,
    keyring: KeyRing,
    now: datetime,
    backup_expiry_after: datetime | None = None,
    entry_id: str | None = None,
) -> str:
    """Seal one object id into the manifest and return the entry id.

    The object id stays sealed: a manifest that named conversations or photos in
    plaintext would leak exactly what the user asked to remove, and the manifest
    deliberately outlives the live rows it refers to.

    `backup_expiry_after` is the point before which an older backup may still
    contain the object (design §6: the entry is kept "到所有可能含该对象的备份
    过期"). It is optional here because the caller that knows the backup
    generation is the one that has to compute it, and a caller that does not
    know must not invent a value -- a wrong deadline would authorise deleting
    the entry while a restorable copy still exists.
    """
    entry = entry_id or new_id()
    session.execute(
        insert(DeletionManifest).values(
            entry_id=entry,
            object_type=object_type,
            encrypted_object_id=keyring.encrypt(
                object_id.encode("utf-8"),
                table=MANIFEST_TABLE,
                column=MANIFEST_COLUMN,
                row_id=entry,
            ),
            deleted_at=now,
            backup_expiry_after=backup_expiry_after,
        )
    )
    return entry
