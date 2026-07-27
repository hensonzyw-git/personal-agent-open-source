"""DEV-027: the Agent API's key material, loaded the way the service loads it.

These are the failure modes first. A composition root's whole job is to refuse
a deployment that cannot sign, cannot seal, or would sign with the wrong
material, so the cases worth writing are the broken ones -- a public key where a
private one belongs, a truncated data key, one file doing two jobs.

The happy path is checked against the *real* counterparty rather than a fake:
the service ring signs a Host Context and `personal_data_mcp` verifies it with
the public PEM this ring exports, so a key that loads but cannot be verified by
Finance MCP is a failing test, not a later surprise.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from personal_agent.auth.tokens import issue_access_token, verify_access_token
from personal_agent.keys import (
    AgentKeyConfigError,
    CURSOR_ACTIVE_KEY_ENV,
    CURSOR_ACTIVE_KID_ENV,
    CURSOR_PREVIOUS_ENV,
    DATA_ACTIVE_KEY_ENV,
    DATA_ACTIVE_KID_ENV,
    DATA_PREVIOUS_ENV,
    IDENTIFIER_ACTIVE_KEY_ENV,
    IDENTIFIER_ACTIVE_KID_ENV,
    IDENTIFIER_PREVIOUS_ENV,
    SERVICE,
    SERVICE_ACTIVE_KEY_ENV,
    SERVICE_ACTIVE_KID_ENV,
    SERVICE_PREVIOUS_ENV,
    TOKEN_ACTIVE_KEY_ENV,
    TOKEN_ACTIVE_KID_ENV,
    TOKEN_PREVIOUS_ENV,
    load_access_token_ring,
    load_agent_data_keyring,
    load_cursor_key,
    load_identifier_key,
    load_service_signing_ring,
)
from personal_agent_core.host_context import HostContext, sign_host_context
from personal_agent_core.host_context import verify_host_context
from personal_agent_core.timeutil import utc_now
from personal_data_mcp.server.keys import (
    ACTIVE_KID_ENV as MCP_KID_ENV,
    ACTIVE_PEM_ENV as MCP_PEM_ENV,
    load_verification_ring,
)


def write_data_key(path: Path, size: int = 32) -> Path:
    path.write_text(base64.urlsafe_b64encode(b"k" * size).decode("ascii"))
    return path


def write_private(path: Path, curve=ec.SECP256R1()) -> ec.EllipticCurvePrivateKey:
    key = ec.generate_private_key(curve)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return key


def write_public(path: Path, key: ec.EllipticCurvePrivateKey) -> Path:
    path.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return path


# --- refusals ----------------------------------------------------------------


def test_every_ring_refuses_an_unconfigured_environment() -> None:
    for load in (
        load_agent_data_keyring,
        load_access_token_ring,
        load_cursor_key,
        load_identifier_key,
        load_service_signing_ring,
    ):
        with pytest.raises(AgentKeyConfigError):
            load({})


def test_a_data_key_of_the_wrong_length_is_refused(tmp_path: Path) -> None:
    path = write_data_key(tmp_path / "short.key", size=16)
    with pytest.raises(AgentKeyConfigError, match="32 bytes"):
        load_agent_data_keyring(
            {DATA_ACTIVE_KID_ENV: "k1", DATA_ACTIVE_KEY_ENV: str(path)}
        )


@pytest.mark.parametrize(
    ("load", "active_kid_env", "active_key_env", "previous_env"),
    [
        (
            load_cursor_key,
            CURSOR_ACTIVE_KID_ENV,
            CURSOR_ACTIVE_KEY_ENV,
            CURSOR_PREVIOUS_ENV,
        ),
        (
            load_identifier_key,
            IDENTIFIER_ACTIVE_KID_ENV,
            IDENTIFIER_ACTIVE_KEY_ENV,
            IDENTIFIER_PREVIOUS_ENV,
        ),
    ],
)
def test_retired_hmac_keys_remain_available_for_verification(
    tmp_path: Path,
    load,
    active_kid_env: str,
    active_key_env: str,
    previous_env: str,
) -> None:
    active = tmp_path / "active.key"
    retired = tmp_path / "retired.key"
    active.write_text(base64.urlsafe_b64encode(b"a" * 32).decode("ascii"))
    retired.write_text(base64.urlsafe_b64encode(b"r" * 32).decode("ascii"))

    ring = load(
        {
            active_kid_env: "active",
            active_key_env: str(active),
            previous_env: f"retired={retired}",
        }
    )

    assert ring.active.secret == b"a" * 32
    assert [entry.secret for entry in ring.previous] == [b"r" * 32]


def test_a_data_key_that_is_not_base64_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bad.key"
    path.write_text("not base64 !!!")
    with pytest.raises(AgentKeyConfigError):
        load_agent_data_keyring(
            {DATA_ACTIVE_KID_ENV: "k1", DATA_ACTIVE_KEY_ENV: str(path)}
        )


def test_a_missing_key_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AgentKeyConfigError, match="cannot read"):
        load_agent_data_keyring(
            {
                DATA_ACTIVE_KID_ENV: "k1",
                DATA_ACTIVE_KEY_ENV: str(tmp_path / "absent.key"),
            }
        )


def test_a_malformed_retired_entry_is_refused(tmp_path: Path) -> None:
    path = write_data_key(tmp_path / "active.key")
    with pytest.raises(AgentKeyConfigError, match="kid=path"):
        load_agent_data_keyring(
            {
                DATA_ACTIVE_KID_ENV: "k1",
                DATA_ACTIVE_KEY_ENV: str(path),
                DATA_PREVIOUS_ENV: "no-equals-sign",
            }
        )


def test_a_public_pem_where_a_signing_key_belongs_is_refused(tmp_path: Path) -> None:
    private = write_private(tmp_path / "real.pem")
    public_only = write_public(tmp_path / "public.pem", private)
    with pytest.raises(AgentKeyConfigError, match="private key"):
        load_access_token_ring(
            {
                TOKEN_ACTIVE_KID_ENV: "t1",
                TOKEN_ACTIVE_KEY_ENV: str(public_only),
            }
        )


def test_a_non_p256_signing_key_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "p384.pem"
    write_private(path, curve=ec.SECP384R1())
    with pytest.raises(AgentKeyConfigError, match="P-256"):
        load_access_token_ring(
            {TOKEN_ACTIVE_KID_ENV: "t1", TOKEN_ACTIVE_KEY_ENV: str(path)}
        )


@pytest.mark.parametrize(
    ("load", "active_kid_env", "active_key_env", "previous_env"),
    [
        (
            load_access_token_ring,
            TOKEN_ACTIVE_KID_ENV,
            TOKEN_ACTIVE_KEY_ENV,
            TOKEN_PREVIOUS_ENV,
        ),
        (
            load_service_signing_ring,
            SERVICE_ACTIVE_KID_ENV,
            SERVICE_ACTIVE_KEY_ENV,
            SERVICE_PREVIOUS_ENV,
        ),
    ],
)
def test_a_non_p256_retired_verification_key_is_refused(
    tmp_path: Path, load, active_kid_env, active_key_env, previous_env
) -> None:
    active = tmp_path / f"{active_kid_env}.pem"
    retired_private = write_private(
        tmp_path / f"{previous_env}.private.pem", curve=ec.SECP384R1()
    )
    retired_public = write_public(
        tmp_path / f"{previous_env}.public.pem", retired_private
    )
    write_private(active)
    with pytest.raises(AgentKeyConfigError, match="P-256"):
        load(
            {
                active_kid_env: "active",
                active_key_env: str(active),
                previous_env: f"retired={retired_public}",
            }
        )


def test_the_service_ring_refuses_to_share_the_token_signing_key(
    tmp_path: Path,
) -> None:
    """Design 4.3: the two materials are never reused for each other."""
    shared = tmp_path / "one.pem"
    write_private(shared)
    with pytest.raises(AgentKeyConfigError, match="separate material"):
        load_service_signing_ring(
            {
                SERVICE_ACTIVE_KID_ENV: "s1",
                SERVICE_ACTIVE_KEY_ENV: str(shared),
                TOKEN_ACTIVE_KEY_ENV: str(shared),
            }
        )


def test_the_service_ring_refuses_a_copied_token_signing_key(
    tmp_path: Path,
) -> None:
    token = tmp_path / "token.pem"
    copied_service = tmp_path / "service-copy.pem"
    material = write_private(token)
    copied_service.write_bytes(
        material.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    with pytest.raises(AgentKeyConfigError, match="separate material"):
        load_service_signing_ring(
            {
                SERVICE_ACTIVE_KID_ENV: "s1",
                SERVICE_ACTIVE_KEY_ENV: str(copied_service),
                TOKEN_ACTIVE_KEY_ENV: str(token),
            }
        )


@pytest.mark.parametrize(
    ("service_slot", "token_slot"),
    [
        ("previous", "active"),
        ("active", "previous"),
        ("previous", "previous"),
    ],
)
def test_the_service_ring_refuses_shared_material_during_key_overlap(
    tmp_path: Path, service_slot: str, token_slot: str
) -> None:
    service_active = tmp_path / "service-active.pem"
    token_active = tmp_path / "token-active.pem"
    shared_private = write_private(tmp_path / "shared-private.pem")
    shared_public = write_public(tmp_path / "shared-public.pem", shared_private)
    if service_slot == "active":
        service_active.write_bytes(
            shared_private.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    else:
        write_private(service_active)
    if token_slot == "active":
        token_active.write_bytes(
            shared_private.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    else:
        write_private(token_active)

    env = {
        SERVICE_ACTIVE_KID_ENV: "service-active",
        SERVICE_ACTIVE_KEY_ENV: str(service_active),
        TOKEN_ACTIVE_KEY_ENV: str(token_active),
    }
    if service_slot == "previous":
        env[SERVICE_PREVIOUS_ENV] = f"service-retired={shared_public}"
    if token_slot == "previous":
        env[TOKEN_PREVIOUS_ENV] = f"token-retired={shared_public}"

    with pytest.raises(AgentKeyConfigError, match="separate material"):
        load_service_signing_ring(env)


# --- what a loaded ring can actually do --------------------------------------


def test_the_data_ring_seals_under_the_agent_service_name(tmp_path: Path) -> None:
    ring = load_agent_data_keyring(
        {
            DATA_ACTIVE_KID_ENV: "agent-data-2026-01",
            DATA_ACTIVE_KEY_ENV: str(write_data_key(tmp_path / "d.key")),
        }
    )
    assert ring.service == SERVICE
    sealed = ring.encrypt(
        b"secret", table="api_requests", column="payload", row_id="r1"
    )
    assert (
        ring.decrypt(sealed, table="api_requests", column="payload", row_id="r1")
        == b"secret"
    )


def test_a_retired_data_key_stays_readable(tmp_path: Path) -> None:
    old = write_data_key(tmp_path / "old.key")
    retired_ring = load_agent_data_keyring(
        {DATA_ACTIVE_KID_ENV: "old", DATA_ACTIVE_KEY_ENV: str(old)}
    )
    sealed = retired_ring.encrypt(
        b"before rotation", table="t", column="c", row_id="r"
    )

    rotated = load_agent_data_keyring(
        {
            DATA_ACTIVE_KID_ENV: "new",
            DATA_ACTIVE_KEY_ENV: str(write_data_key(tmp_path / "new.key", 32)),
            DATA_PREVIOUS_ENV: f"old={old}",
        }
    )
    assert (
        rotated.decrypt(sealed, table="t", column="c", row_id="r")
        == b"before rotation"
    )


def test_the_token_ring_issues_a_token_it_can_verify(tmp_path: Path) -> None:
    path = tmp_path / "token.pem"
    write_private(path)
    ring = load_access_token_ring(
        {TOKEN_ACTIVE_KID_ENV: "tok-1", TOKEN_ACTIVE_KEY_ENV: str(path)}
    )
    now = utc_now()
    token = issue_access_token(
        ring,
        device_id="dev-1",
        device_key_thumbprint="THUMB",
        scopes=["meta.capabilities.read"],
        allowed_tools_version="v1",
        now=now,
    )
    claims = verify_access_token(ring, token, now=now)
    assert claims["device_id"] == "dev-1"


def test_finance_mcp_verifies_what_the_service_ring_signs(tmp_path: Path) -> None:
    """The real counterparty, not a fake: Finance MCP loads the public half."""
    private_path = tmp_path / "service.pem"
    private = write_private(private_path)
    public_path = write_public(tmp_path / "service.pub.pem", private)

    signing = load_service_signing_ring(
        {
            SERVICE_ACTIVE_KID_ENV: "svc-2026-01",
            SERVICE_ACTIVE_KEY_ENV: str(private_path),
        }
    )
    verifying = load_verification_ring(
        {MCP_KID_ENV: "svc-2026-01", MCP_PEM_ENV: str(public_path)}
    )

    arguments = {"amount": "20.00"}
    context = HostContext(
        agent_id="personal-agent-api",
        device_id="dev-1",
        user_id="henson",
        scopes=("meta.capabilities.read",),
        tool="meta.capabilities",
        request_id="11111111-1111-4111-8111-111111111111",
        trace_id="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        idempotency_key="22222222-2222-4222-8222-222222222222",
        request_fingerprint="fp",
        allowed_tools_version="v1",
        timezone="Asia/Shanghai",
    )
    token = sign_host_context(signing, context, arguments)
    claims = verify_host_context(
        verifying,
        token,
        tool="meta.capabilities",
        idempotency_key=context.idempotency_key,
        request_id=context.request_id,
        user_id=context.user_id,
        trace_id=context.trace_id,
        timezone=context.timezone,
        arguments=arguments,
    )
    assert claims["device_id"] == "dev-1"
