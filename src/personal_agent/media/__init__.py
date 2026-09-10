"""Encrypted media storage for chat images.

Multimodal design §4, §5.1 and §5.3. The server never decodes and never derives
content under the adopted option 1: the bytes it seals while receiving an upload
*are* the persisted image, and publishing is a rename rather than a conversion.
That is why this package holds no image library, no subprocess and no derived
file -- only a sealed container format, a lock set and a publish protocol.
"""

from __future__ import annotations

from personal_agent.media.container import (
    CONTAINER_MAGIC,
    CONTAINER_VERSION,
    DEFAULT_CHUNK_BYTES,
    DEFAULT_MAX_CONTENT_BYTES,
    DEFAULT_MEDIA_ROLE,
    MAX_CHUNK_BYTES,
    ChunkHasher,
    ContainerError,
    SealRecord,
    StagingWriter,
    open_container,
    read_container,
    write_container,
)
from personal_agent.media.locking import (
    STRIPE_COUNT,
    MediaLockError,
    ensure_lock_files,
    media_locks,
    stripe_of,
    verify_lock_installation,
)
from personal_agent.media.store import (
    FinalOutcome,
    MediaIntegrityError,
    MediaRoots,
    MediaStore,
    MediaStoreError,
    probe_non_overwriting_publish,
    verify_media_installation,
)

__all__ = [
    "CONTAINER_MAGIC",
    "CONTAINER_VERSION",
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_MAX_CONTENT_BYTES",
    "DEFAULT_MEDIA_ROLE",
    "MAX_CHUNK_BYTES",
    "STRIPE_COUNT",
    "ChunkHasher",
    "ContainerError",
    "FinalOutcome",
    "MediaIntegrityError",
    "MediaLockError",
    "MediaRoots",
    "MediaStore",
    "MediaStoreError",
    "SealRecord",
    "StagingWriter",
    "ensure_lock_files",
    "media_locks",
    "open_container",
    "probe_non_overwriting_publish",
    "read_container",
    "stripe_of",
    "verify_lock_installation",
    "verify_media_installation",
    "write_container",
]
