"""§4.3's versioned media ceilings, and where the objects live.

The design makes every number here *configuration* rather than a constant, for
a reason it states about the whole set (§5.4): "按实际 ECS 资源盘点形成版本化
配置；**缺配置不启用图片**". A module that ships a default ceiling has shipped a
policy nobody chose, and the failure mode is asymmetric -- too high and an
upload exhausts the box, too low and Henson's photo is refused with no way to
tell that the limit was invented rather than set. So there is one default
anywhere in this file and it is the *absence* of configuration.

That produces the three outcomes an operator can meet, and they are deliberately
different from each other:

- **nothing configured** -- the media surface is not composed. Not an error:
  images are off, `/v1/capabilities` does not advertise them, and the five
  routes refuse. This is also the state of every offline test.
- **a value present but unreadable** -- a startup failure. A typo in a unit
  file is a fact about the deployment that an operator has to see, and silence
  here would surface instead as an upload that fails for no visible reason.
- **configured, but the installation is not ready** -- not composed, with the
  reason logged. §4.1 puts the lock set's creation at install time and forbids
  the runtime from creating it ("运行时不创建/替换"), so a service that started
  anyway would answer every upload with a refusal it could only describe as
  "busy". Images off is the truthful answer; a permanent misconfiguration
  dressed up as a transient one is not.

What this does *not* do at startup is the ownership walk
(`verify_lock_installation`): that check asserts that the running identity
cannot replace a lock, and it is the operator verifier's job for exactly that
reason -- it is a statement about the host and the deployment, and it belongs
where a human reads the report. See §9's verification plan.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from personal_agent.media.locking import (
    STRIPE_COUNT,
    STORAGE_LOCK_NAME,
    locks_directory,
    stripe_lock_name,
)
from personal_agent.media.store import MediaStore
from personal_agent.media.uploads import MediaLimits
from personal_agent_core.crypto import KeyRing

logger = logging.getLogger(__name__)

#: The switch, and the only variable whose absence turns media off on its own.
#: An absolute path: a relative one would resolve against the service's working
#: directory, which is ambient state this service refuses to depend on.
ROOT_ENV = "PERSONAL_AGENT_MEDIA_ROOT"
MAX_CONTENT_ENV = "PERSONAL_AGENT_MEDIA_MAX_CONTENT_BYTES"
MAX_DIMENSION_ENV = "PERSONAL_AGENT_MEDIA_MAX_DIMENSION"
#: Comma-separated. The module registers formats it can *identify*; this set is
#: what it will *accept*, and §5.4 keeps the two apart on purpose.
ALLOWED_MIMES_ENV = "PERSONAL_AGENT_MEDIA_ALLOWED_MIMES"
TARGET_TTL_ENV = "PERSONAL_AGENT_MEDIA_TARGET_TTL_SECONDS"
CLAIM_TTL_ENV = "PERSONAL_AGENT_MEDIA_CLAIM_TTL_SECONDS"
RETENTION_TTL_ENV = "PERSONAL_AGENT_MEDIA_RETENTION_TTL_SECONDS"
#: §8's budget coefficient, and part of the required set rather than an
#: optional extra. §10 lists "预算缺失时均保持关闭" beside the scanner exemption
#: and A2 unreachability: a deployment that has not said what an image may
#: cost has not decided to serve images, and a default here would be this
#: module choosing that policy on the operator's behalf.
IMAGE_PIXELS_PER_TOKEN_ENV = "PERSONAL_AGENT_MEDIA_IMAGE_PIXELS_PER_TOKEN"

_REQUIRED_ENV = (
    MAX_CONTENT_ENV,
    MAX_DIMENSION_ENV,
    ALLOWED_MIMES_ENV,
    TARGET_TTL_ENV,
    CLAIM_TTL_ENV,
    RETENTION_TTL_ENV,
    IMAGE_PIXELS_PER_TOKEN_ENV,
)


class MediaConfigError(RuntimeError):
    """A configured value could not be read. Raised at startup, never at a request."""


@dataclass(frozen=True)
class MediaConfig:
    """The validated configuration, plus the two objects built from it.

    The store and the limits are built together because they must agree: the
    store enforces the byte ceiling while receiving, and the limits enforce it
    while validating the declaration, and two different numbers would mean a
    client could declare a size the writer then refuses.
    """

    root: Path
    max_content_bytes: int
    max_dimension: int
    allowed_mimes: frozenset[str]
    target_ttl: timedelta
    claim_ttl: timedelta
    retention_ttl: timedelta
    image_pixels_per_token: int

    def limits(self) -> MediaLimits:
        return MediaLimits(
            max_content_bytes=self.max_content_bytes,
            max_dimension=self.max_dimension,
            allowed_mimes=self.allowed_mimes,
            target_ttl=self.target_ttl,
            claim_ttl=self.claim_ttl,
            retention_ttl=self.retention_ttl,
            image_pixels_per_token=self.image_pixels_per_token,
        )

    def store(self, keyring: KeyRing) -> MediaStore:
        return MediaStore(
            self.root,
            keyring,
            max_content_bytes=self.max_content_bytes,
        )


def media_config_from_env(
    environ: Mapping[str, str] | None = None,
) -> MediaConfig | None:
    """Read §4.3's configuration, or `None` when media is not configured."""
    source = os.environ if environ is None else environ
    root_raw = (source.get(ROOT_ENV) or "").strip()
    if not root_raw:
        return None

    missing = [name for name in _REQUIRED_ENV if not (source.get(name) or "").strip()]
    if missing:
        # §5.4's "缺配置不启用图片" is about exactly this: a half-filled set is
        # not a deployment that wants images, it is one that has not decided.
        logger.error(
            "media is configured but %s is unset, so images stay off",
            ", ".join(sorted(missing)),
        )
        return None

    root = Path(root_raw)
    if not root.is_absolute():
        raise MediaConfigError(f"{ROOT_ENV} must be an absolute path")
    if not root.is_dir():
        logger.error("media root %s is not a directory, so images stay off", root)
        return None
    absent = _missing_lock_files(root)
    if absent:
        logger.error(
            "media locks are not installed (%s), so images stay off; §4.1 "
            "creates them at install time and the runtime must not",
            ", ".join(absent),
        )
        return None

    allowed = frozenset(
        part.strip().lower()
        for part in source[ALLOWED_MIMES_ENV].split(",")
        if part.strip()
    )
    if not allowed:
        # An empty allow-list is not "media with nothing permitted": it is a
        # deployment whose policy nobody expressed, and it would refuse every
        # upload while still advertising the surface.
        logger.error("%s names no media type, so images stay off", ALLOWED_MIMES_ENV)
        return None

    return MediaConfig(
        root=root,
        max_content_bytes=_positive_int(source, MAX_CONTENT_ENV),
        max_dimension=_positive_int(source, MAX_DIMENSION_ENV),
        allowed_mimes=allowed,
        target_ttl=_positive_seconds(source, TARGET_TTL_ENV),
        claim_ttl=_positive_seconds(source, CLAIM_TTL_ENV),
        retention_ttl=_positive_seconds(source, RETENTION_TTL_ENV),
        image_pixels_per_token=_positive_int(source, IMAGE_PIXELS_PER_TOKEN_ENV),
    )


def _missing_lock_files(root: Path) -> list[str]:
    """Which of §4.1's fixed lock set is absent from this installation.

    Existence only, deliberately. A missing lock file cannot be created by the
    runtime and cannot be recovered from, while an ownership problem is a
    finding for the verifier that reports it -- so this answers the one
    question a starting service has to ask, and leaves the rest to §9.
    """
    directory = locks_directory(root)
    absent: list[str] = []
    if not directory.is_dir():
        return [str(directory)]
    for name in [STORAGE_LOCK_NAME] + [
        stripe_lock_name(stripe) for stripe in range(STRIPE_COUNT)
    ]:
        path = directory / name
        if not path.is_file():
            absent.append(str(path))
    return absent


def _positive_int(source: Mapping[str, str], name: str) -> int:
    raw = (source.get(name) or "").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise MediaConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise MediaConfigError(f"{name} must be positive, got {value}")
    return value


def _positive_seconds(source: Mapping[str, str], name: str) -> timedelta:
    return timedelta(seconds=_positive_int(source, name))


__all__ = [
    "MediaConfig",
    "MediaConfigError",
    "media_config_from_env",
]
