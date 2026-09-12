"""§4.3's configuration reader, and the three outcomes it must keep apart.

The design gives one instruction about a missing configuration -- §5.4's
"缺配置不启用图片" -- and the temptation is to read it as covering all three of
"unset", "mis-typed" and "installed badly". They are not the same fact and must
not look the same to an operator:

- unset (or half-set) means images are off, which is a choice;
- mis-typed is a startup failure, because the alternative is an upload that
  fails for a reason no log names;
- installed badly (no lock set) is off as well, because §4.1 forbids the
  runtime from creating one, so the alternative is answering every request with
  a "busy" that never clears.

The lock-existence check is what makes the third case reachable here; the
ownership walk is the operator verifier's, and is deliberately absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_agent.media.config import (
    ALLOWED_MIMES_ENV,
    CLAIM_TTL_ENV,
    IMAGE_PIXELS_PER_TOKEN_ENV,
    MAX_CONTENT_ENV,
    MAX_DIMENSION_ENV,
    RETENTION_TTL_ENV,
    ROOT_ENV,
    TARGET_TTL_ENV,
    MediaConfigError,
    media_config_from_env,
)
from personal_agent.media.locking import ensure_lock_files


def _installed(tmp_path: Path) -> Path:
    """A root with §4.1's lock set, which the runtime may not create itself."""
    root = tmp_path / "media"
    for name in ("staging", "final", "quarantine"):
        (root / name).mkdir(parents=True, exist_ok=True)
    ensure_lock_files(root)
    return root


def _configured(root: Path, **overrides) -> dict[str, str]:
    environ = {
        ROOT_ENV: str(root),
        MAX_CONTENT_ENV: "31457280",
        MAX_DIMENSION_ENV: "12000",
        ALLOWED_MIMES_ENV: "image/jpeg",
        TARGET_TTL_ENV: "1800",
        CLAIM_TTL_ENV: "600",
        RETENTION_TTL_ENV: "604800",
        IMAGE_PIXELS_PER_TOKEN_ENV: "750",
        "PERSONAL_AGENT_MEDIA_MAX_TOTAL_BYTES": "1073741824",
        "PERSONAL_AGENT_MEDIA_MAX_UNBOUND_OBJECTS": "20",
        "PERSONAL_AGENT_MEDIA_MAX_CONCURRENT_UPLOADS": "2",
    }
    environ.update(overrides)
    return environ


def test_nothing_configured_leaves_media_off():
    assert media_config_from_env({}) is None


@pytest.mark.parametrize(
    "mutilate",
    [
        lambda environ: environ.pop(MAX_DIMENSION_ENV),
        lambda environ: environ.pop(ALLOWED_MIMES_ENV),
        # An empty value is what a `Environment=X=` line in a unit file
        # produces, and it is not distinguishable in practice from an unset
        # one. Both are "not configured" rather than "misconfigured" for that
        # reason: there is no typo to report, only a decision not made.
        lambda environ: environ.update({RETENTION_TTL_ENV: ""}),
        # §8's coefficient is part of the set, not a bonus on top of it. A
        # deployment that has decided to store images but not what one may cost
        # the context has not decided to *serve* them (§5.4's "缺配置不启用图片",
        # §10's "预算缺失时均保持关闭").
        lambda environ: environ.pop(IMAGE_PIXELS_PER_TOKEN_ENV),
    ],
)
def test_a_half_filled_set_leaves_media_off(tmp_path: Path, mutilate):
    """Not a boot failure: a deployment that has not decided is a valid state.

    The switch itself is present, which is what makes this different from
    "nothing configured" -- the choice was made and then not finished, and the
    answer to both is that images are off.
    """
    root = _installed(tmp_path)
    environ = _configured(root)
    mutilate(environ)
    assert media_config_from_env(environ) is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (MAX_CONTENT_ENV, "30MiB"),
        (MAX_CONTENT_ENV, "0"),
        (MAX_CONTENT_ENV, "-1"),
        (MAX_DIMENSION_ENV, "12000.5"),
        (TARGET_TTL_ENV, "half an hour"),
        (CLAIM_TTL_ENV, "0"),
        (RETENTION_TTL_ENV, "7d"),
        (IMAGE_PIXELS_PER_TOKEN_ENV, "750.5"),
        (IMAGE_PIXELS_PER_TOKEN_ENV, "0"),
    ],
)
def test_an_unreadable_value_stops_the_boot(tmp_path: Path, name, value):
    root = _installed(tmp_path)
    with pytest.raises(MediaConfigError):
        media_config_from_env(_configured(root, **{name: value}))


def test_a_relative_root_is_refused(tmp_path: Path):
    # A relative root resolves against the service's working directory, which
    # is ambient state this service refuses to depend on anywhere else.
    with pytest.raises(MediaConfigError):
        media_config_from_env(_configured(Path("media")))


def test_a_root_that_is_not_a_directory_leaves_media_off(tmp_path: Path):
    assert media_config_from_env(_configured(tmp_path / "absent")) is None


def test_a_missing_lock_set_leaves_media_off(tmp_path: Path):
    """§4.1: install creates the lock set, the runtime never does.

    Without this check the service would compose, and every upload would answer
    with the lock error's mapping -- a retryable "busy" that no retry clears.
    """
    root = tmp_path / "media"
    for name in ("staging", "final", "quarantine"):
        (root / name).mkdir(parents=True, exist_ok=True)
    assert media_config_from_env(_configured(root)) is None

    ensure_lock_files(root)
    assert media_config_from_env(_configured(root)) is not None


def test_an_empty_allow_list_leaves_media_off(tmp_path: Path):
    root = _installed(tmp_path)
    assert media_config_from_env(_configured(root, **{ALLOWED_MIMES_ENV: " , "})) is None


def test_a_valid_set_reads_back_as_the_two_objects_it_configures(tmp_path: Path):
    root = _installed(tmp_path)
    config = media_config_from_env(
        _configured(root, **{ALLOWED_MIMES_ENV: "image/jpeg, IMAGE/PNG"})
    )
    assert config is not None
    assert config.root == root
    assert config.allowed_mimes == frozenset({"image/jpeg", "image/png"})

    limits = config.limits()
    assert limits.max_content_bytes == 31457280
    assert limits.claim_ttl.total_seconds() == 600
    assert limits.target_ttl.total_seconds() == 1800
    assert limits.retention_ttl.total_seconds() == 604800

    # §8's coefficient travels with the limits, which is where both the
    # declaration check and the budgeter read it from. It reaches the budget as
    # a per-image upper bound: the server never decodes (§5.4), so the declared
    # pixels are the only dimensions that exist, and the bound rounds up.
    assert limits.image_pixels_per_token == 750
    assert limits.image_token_upper_bound(1500, 2000) == 4000
    assert limits.image_token_upper_bound(1001, 1000) == 1335

    # The two objects come off one `MediaConfig` on purpose: the store enforces
    # the byte ceiling while receiving and the limits enforce it while
    # validating the declaration, so two different numbers would let a client
    # declare a size the writer then refuses. The store is checked on the part
    # of that wiring a test can see -- where it writes.
    assert config.store(_NullKeyRing()).roots.root == root


class _NullKeyRing:
    """Enough of a keyring for `MediaStore` to hold. Nothing here encrypts."""

    service = "test"
