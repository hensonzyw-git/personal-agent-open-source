"""DEV-029: the device identity surface over HTTP.

Three of these endpoints are unauthenticated and public, so this file is written
the way §5.1 requires for an attacker-facing boundary: the failure shapes were
chosen first, and each one is a test. The clean round trip is a single test near
the top; everything after it is a way the surface could have been wrong.

The device key here is a software P-256 key from `cryptography`, standing in for
the Secure Enclave. That substitution is only honest because the *wire* contract
is what is under test and it is fixed to the byte: one test enrolls the frozen
cross-language vector from `auth/vectors/device_auth_vectors.json` and presents
that vector's exact signature, so a Swift implementation that reproduces the same
vector is talking to the same server contract.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from personal_agent.api.app import AgentApiDeps, build_app
from personal_agent.api.composition import device_authorization
from personal_agent.api.device_api import SELF_READ_SCOPE, SELF_REVOKE_SCOPE
from personal_agent.auth.device_keys import (
    b64u_encode,
    build_signing_input,
    der_to_jose,
    device_key_thumbprint,
    encode_device_public_key,
)
from personal_agent.auth.enrollment import (
    DEFAULT_DEVICE_SCOPES,
    DEVICE_MANAGE_SCOPE,
    create_enrollment_code,
    encode_device_scopes,
)
from personal_agent.auth.tokens import (
    SigningKey,
    TokenKeyRing,
    verify_access_token,
)
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import AuthChallenge, Device
from personal_agent_core.crypto import KeyRing, generate_key


VECTORS_PATH = (
    Path(__file__).parents[2]
    / "src/personal_agent/auth/vectors/device_auth_vectors.json"
)
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))

NOW = datetime(2026, 7, 26, 7, 0, tzinfo=timezone.utc)
MANIFEST_VERSION = "manifest-under-test"
PUSH_TOKEN = "a" * 64


# --- harness -----------------------------------------------------------------


@pytest.fixture()
def token_ring() -> TokenKeyRing:
    private = ec.generate_private_key(ec.SECP256R1())
    return TokenKeyRing(active=SigningKey("tok-2026", private, private.public_key()))


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing(
        [generate_key("agent-data-2026", state="active")], service="personal-agent"
    )


@pytest.fixture()
def engine(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    yield engine
    engine.dispose()


class Harness:
    """The app, its clock and the operator actions an administrator would take."""

    def __init__(self, engine, token_ring, keyring, *, manifest_version) -> None:
        self.engine = engine
        self.sessions = session_factory(engine)
        self.token_ring = token_ring
        self.keyring = keyring
        self.now = NOW
        deps = AgentApiDeps(
            session_factory=self.sessions,
            token_ring=token_ring,
            keyring=keyring,
            build_interpreter=lambda auth: None,
            build_dispatcher=lambda auth, trace_id: None,
            build_authorizer=lambda auth: (lambda *, tool, model_args: model_args),
            capabilities=lambda auth: [{"alias": "meta.capabilities"}],
            now=lambda: self.now,
            enrollment_manifest_version=manifest_version,
        )
        self.client = TestClient(build_app(deps))

    # operator side ----------------------------------------------------------

    def issue_code(self, *, manage: bool = False) -> str:
        with self.sessions() as session:
            issued = create_enrollment_code(
                session, now=self.now, grants_device_manage=manage
            )
            session.commit()
            return issued.code

    def set_scopes(self, device_id: str, scopes: list[str]) -> None:
        with self.sessions() as session:
            device = session.get(Device, device_id)
            device.scopes = encode_device_scopes(scopes)
            session.commit()

    def revoke(self, device_id: str) -> None:
        with self.sessions() as session:
            device = session.get(Device, device_id)
            device.status = "revoked"
            device.revoked_at = self.now
            session.commit()

    def devices(self) -> list[Device]:
        with self.sessions() as session:
            return session.query(Device).all()

    # device side ------------------------------------------------------------

    def enroll(self, *, code: str, manage: bool = False) -> "EnrolledDevice":
        private = ec.generate_private_key(ec.SECP256R1())
        public = encode_device_public_key(private.public_key())
        response = self.client.post(
            "/v1/enrollments/claim",
            json={"code": code, "public_key": public, "display_name": "iPhone"},
        )
        assert response.status_code == 201, response.text
        return EnrolledDevice(self, private, response.json()["device_id"])


class EnrolledDevice:
    def __init__(self, harness: Harness, private, device_id: str) -> None:
        self.harness = harness
        self.private = private
        self.device_id = device_id

    def challenge(self) -> dict:
        response = self.harness.client.post(
            "/v1/auth/challenges", json={"device_id": self.device_id}
        )
        assert response.status_code == 200, response.text
        return response.json()

    def sign(self, *, challenge_id: str, nonce: str) -> str:
        message = build_signing_input(
            challenge_id=challenge_id, nonce_b64u=nonce, device_id=self.device_id
        )
        der = self.private.sign(message, ec.ECDSA(hashes.SHA256()))
        return b64u_encode(der_to_jose(der))

    def token_response(self, *, challenge: dict | None = None):
        issued = challenge if challenge is not None else self.challenge()
        return self.harness.client.post(
            "/v1/auth/tokens",
            json={
                "challenge_id": issued["challenge_id"],
                "device_id": self.device_id,
                "nonce": issued["nonce"],
                "signature": self.sign(
                    challenge_id=issued["challenge_id"], nonce=issued["nonce"]
                ),
            },
        )

    def token(self) -> str:
        response = self.token_response()
        assert response.status_code == 200, response.text
        return response.json()["access_token"]

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}"}


@pytest.fixture()
def harness(engine, token_ring, keyring) -> Harness:
    return Harness(engine, token_ring, keyring, manifest_version=MANIFEST_VERSION)


# --- the clean round trip ----------------------------------------------------


def test_a_device_can_enroll_get_a_token_and_call_an_authenticated_endpoint(
    harness: Harness, token_ring
) -> None:
    device = harness.enroll(code=harness.issue_code())

    issued = device.token_response().json()
    assert issued["token_type"] == "Bearer"
    assert issued["expires_in"] == 600
    assert issued["device_id"] == device.device_id
    assert set(issued["scopes"]) == set(DEFAULT_DEVICE_SCOPES)
    assert issued["allowed_tools_version"] == MANIFEST_VERSION

    claims = verify_access_token(token_ring, issued["access_token"], now=NOW)
    assert claims["device_id"] == device.device_id
    assert claims["sub"] == f"device:{device.device_id}"
    assert claims["allowed_tools_version"] == MANIFEST_VERSION

    response = harness.client.get(
        "/v1/capabilities", headers={"Authorization": f"Bearer {issued['access_token']}"}
    )
    assert response.status_code == 200


def test_the_enrolled_scopes_column_is_readable_by_the_composition_layer(
    harness: Harness,
) -> None:
    """The regression test for the format that only production would have hit.

    `claim_enrollment_code` used to write this column with `repr()` while every
    reader parsed it as JSON. Nothing caught it because nothing enrolled a device
    outside the enrollment module, and a `repr` list still answers a substring
    `in` check. The first real iPhone would have enrolled successfully and then
    been granted nothing.
    """
    device = harness.enroll(code=harness.issue_code())
    with harness.sessions() as session:
        authorization = device_authorization(
            session,
            device.device_id,
            enabled_tools=frozenset({"meta.capabilities"}),
            manifest_version=MANIFEST_VERSION,
        )
    assert authorization is not None
    assert authorization.scopes == frozenset(DEFAULT_DEVICE_SCOPES)
    assert authorization.allowed_tools == frozenset({"meta.capabilities"})


def test_the_frozen_cross_language_vector_authenticates_over_the_wire(
    harness: Harness,
) -> None:
    """The exact bytes a Swift implementation must produce.

    The challenge id and nonce are server-generated, so the vector's challenge is
    seeded directly. What this proves is the part Swift has to get right: the
    signing input, the JOSE `R || S` encoding and the base64url form are accepted
    by the running server, and the DER form of the *same* signature is not.
    """
    with harness.sessions() as session:
        session.add(
            Device(
                device_id=VECTORS["device_id"],
                display_name="vector",
                public_key=VECTORS["public_key_x963_b64u"],
                device_key_thumbprint=device_key_thumbprint(
                    VECTORS["public_key_x963_b64u"]
                ),
                status="active",
                scopes=encode_device_scopes(list(DEFAULT_DEVICE_SCOPES)),
                allowed_tools_version=MANIFEST_VERSION,
                created_at=NOW,
            )
        )
        session.add(
            AuthChallenge(
                challenge_id=VECTORS["challenge_id"],
                device_id=VECTORS["device_id"],
                nonce_hash=hashlib.sha256(
                    VECTORS["nonce_b64u"].encode("utf-8")
                ).hexdigest(),
                failed_attempts=0,
                created_at=NOW,
                expires_at=NOW + timedelta(minutes=2),
            )
        )
        session.commit()

    body = {
        "challenge_id": VECTORS["challenge_id"],
        "device_id": VECTORS["device_id"],
        "nonce": VECTORS["nonce_b64u"],
        "signature": VECTORS["signature_der_b64u"],
    }
    assert harness.client.post("/v1/auth/tokens", json=body).status_code == 401

    body["signature"] = VECTORS["signature_jose_b64u"]
    accepted = harness.client.post("/v1/auth/tokens", json=body)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["device_id"] == VECTORS["device_id"]
    assert VECTORS["device_key_thumbprint_b64u"] == device_key_thumbprint(
        VECTORS["public_key_x963_b64u"]
    )


# --- enrollment refusals -----------------------------------------------------


def _claim(harness: Harness, **body) -> object:
    key = ec.generate_private_key(ec.SECP256R1())
    payload = {
        "code": harness.issue_code(),
        "public_key": encode_device_public_key(key.public_key()),
        "display_name": "iPhone",
    }
    payload.update(body)
    return harness.client.post("/v1/enrollments/claim", json=payload)


def test_an_unknown_code_is_refused_and_enrolls_nothing(harness: Harness) -> None:
    response = _claim(harness, code="not-a-real-code")
    assert response.status_code == 403
    assert response.json() == {"error": {"code": "ENROLLMENT_REJECTED"}}
    assert harness.devices() == []


def test_a_code_cannot_be_claimed_twice(harness: Harness) -> None:
    code = harness.issue_code()
    harness.enroll(code=code)
    second = _claim(harness, code=code)
    assert second.status_code == 403
    assert len(harness.devices()) == 1


def test_an_expired_code_is_refused(harness: Harness) -> None:
    code = harness.issue_code()
    harness.now = NOW + timedelta(minutes=11)
    assert _claim(harness, code=code).status_code == 403
    assert harness.devices() == []


def test_a_client_cannot_declare_its_own_scopes(harness: Harness) -> None:
    """A smuggled field is a refusal, not a field that is quietly dropped."""
    response = _claim(harness, scopes=["device.manage"])
    assert response.status_code == 400
    assert harness.devices() == []


@pytest.mark.parametrize(
    "field, value",
    [
        ("device_id", "chosen-by-the-client"),
        ("allowed_tools_version", "whatever-i-like"),
        ("status", "active"),
    ],
)
def test_a_client_cannot_declare_server_owned_fields(
    harness: Harness, field: str, value: str
) -> None:
    assert _claim(harness, **{field: value}).status_code == 400
    assert harness.devices() == []


@pytest.mark.parametrize(
    "public_key",
    [
        "",
        "not-base64url!!",
        b64u_encode(b"\x04" + b"\x00" * 63),  # one byte short
        b64u_encode(b"\x02" + b"\x00" * 64),  # compressed prefix
        b64u_encode(b"\x04" + b"\xff" * 64),  # not a point on P-256
    ],
)
def test_a_malformed_public_key_is_refused(harness: Harness, public_key: str) -> None:
    assert _claim(harness, public_key=public_key).status_code == 400
    assert harness.devices() == []


def test_a_padded_base64_public_key_is_refused(harness: Harness) -> None:
    """The wire contract says unpadded. Accepting both would let the two
    implementations drift until a real device fails to authenticate."""
    key = ec.generate_private_key(ec.SECP256R1())
    padded = encode_device_public_key(key.public_key()) + "="
    assert _claim(harness, public_key=padded).status_code == 400


@pytest.mark.parametrize("display_name", ["", "   ", "x" * 65, "iPhone\nadmin"])
def test_a_bad_display_name_is_refused(harness: Harness, display_name: str) -> None:
    assert _claim(harness, display_name=display_name).status_code == 400
    assert harness.devices() == []


def test_device_manage_is_granted_only_by_the_code(harness: Harness) -> None:
    plain = harness.enroll(code=harness.issue_code())
    managed = harness.enroll(code=harness.issue_code(manage=True))
    plain_scopes = plain.token_response().json()["scopes"]
    managed_scopes = managed.token_response().json()["scopes"]
    assert DEVICE_MANAGE_SCOPE not in plain_scopes
    assert DEVICE_MANAGE_SCOPE in managed_scopes


def test_enrollment_refuses_when_no_manifest_version_is_composed(
    engine, token_ring, keyring
) -> None:
    harness = Harness(engine, token_ring, keyring, manifest_version=None)
    response = _claim(harness)
    assert response.status_code == 500
    assert harness.devices() == []


# --- challenge and token refusals -------------------------------------------


def test_an_unknown_device_and_a_revoked_device_are_the_same_answer(
    harness: Harness,
) -> None:
    device = harness.enroll(code=harness.issue_code())
    harness.revoke(device.device_id)

    revoked = harness.client.post(
        "/v1/auth/challenges", json={"device_id": device.device_id}
    )
    unknown = harness.client.post(
        "/v1/auth/challenges", json={"device_id": "018f0000-dead-4000-8000-000000000000"}
    )
    assert revoked.status_code == unknown.status_code == 401
    assert revoked.json() == unknown.json() == {
        "error": {"code": "DEVICE_AUTH_REJECTED"}
    }


def test_a_signature_over_a_different_challenge_is_refused(harness: Harness) -> None:
    device = harness.enroll(code=harness.issue_code())
    first = device.challenge()
    second = device.challenge()
    response = harness.client.post(
        "/v1/auth/tokens",
        json={
            "challenge_id": second["challenge_id"],
            "device_id": device.device_id,
            "nonce": second["nonce"],
            "signature": device.sign(
                challenge_id=first["challenge_id"], nonce=first["nonce"]
            ),
        },
    )
    assert response.status_code == 401


def test_a_replayed_nonce_from_another_challenge_is_refused(harness: Harness) -> None:
    device = harness.enroll(code=harness.issue_code())
    first = device.challenge()
    second = device.challenge()
    response = harness.client.post(
        "/v1/auth/tokens",
        json={
            "challenge_id": second["challenge_id"],
            "device_id": device.device_id,
            "nonce": first["nonce"],
            "signature": device.sign(
                challenge_id=second["challenge_id"], nonce=first["nonce"]
            ),
        },
    )
    assert response.status_code == 401


def test_another_devices_challenge_cannot_be_used(harness: Harness) -> None:
    first = harness.enroll(code=harness.issue_code())
    second = harness.enroll(code=harness.issue_code())
    issued = first.challenge()
    response = harness.client.post(
        "/v1/auth/tokens",
        json={
            "challenge_id": issued["challenge_id"],
            "device_id": second.device_id,
            "nonce": issued["nonce"],
            "signature": second.sign(
                challenge_id=issued["challenge_id"], nonce=issued["nonce"]
            ),
        },
    )
    assert response.status_code == 401


def test_a_challenge_is_single_use(harness: Harness) -> None:
    device = harness.enroll(code=harness.issue_code())
    issued = device.challenge()
    assert device.token_response(challenge=issued).status_code == 200
    assert device.token_response(challenge=issued).status_code == 401


def test_an_expired_challenge_is_refused(harness: Harness) -> None:
    device = harness.enroll(code=harness.issue_code())
    issued = device.challenge()
    harness.now = NOW + timedelta(minutes=3)
    assert device.token_response(challenge=issued).status_code == 401


def test_a_failed_attempt_is_committed_so_the_attempt_limit_is_real(
    harness: Harness,
) -> None:
    """The bug this pins: a refusal that rolls back its own bookkeeping.

    `consume_challenge` counts a failed attempt and the handler's reflex on any
    failure is `rollback()`. If the count is rolled back, a stolen challenge id
    gets unlimited signature guesses and `MAX_CHALLENGE_ATTEMPTS` is decoration.
    """
    device = harness.enroll(code=harness.issue_code())
    issued = device.challenge()
    other = ec.generate_private_key(ec.SECP256R1())

    def wrong_signature() -> str:
        message = build_signing_input(
            challenge_id=issued["challenge_id"],
            nonce_b64u=issued["nonce"],
            device_id=device.device_id,
        )
        return b64u_encode(der_to_jose(other.sign(message, ec.ECDSA(hashes.SHA256()))))

    for attempt in range(5):
        response = harness.client.post(
            "/v1/auth/tokens",
            json={
                "challenge_id": issued["challenge_id"],
                "device_id": device.device_id,
                "nonce": issued["nonce"],
                "signature": wrong_signature(),
            },
        )
        assert response.status_code == 401, attempt

    with harness.sessions() as session:
        challenge = session.get(AuthChallenge, issued["challenge_id"])
        assert challenge.failed_attempts == 5

    # The correct signature no longer helps: the challenge is dead.
    assert device.token_response(challenge=issued).status_code == 401


def test_a_device_revoked_after_its_challenge_gets_no_token(harness: Harness) -> None:
    device = harness.enroll(code=harness.issue_code())
    issued = device.challenge()
    harness.revoke(device.device_id)
    assert device.token_response(challenge=issued).status_code == 401


def test_a_token_states_the_authority_the_device_holds_now(harness: Harness) -> None:
    device = harness.enroll(code=harness.issue_code())
    issued = device.challenge()
    harness.set_scopes(device.device_id, [SELF_READ_SCOPE])
    body = device.token_response(challenge=issued).json()
    assert body["scopes"] == [SELF_READ_SCOPE]


def test_a_token_request_must_not_carry_unexpected_fields(harness: Harness) -> None:
    device = harness.enroll(code=harness.issue_code())
    issued = device.challenge()
    response = harness.client.post(
        "/v1/auth/tokens",
        json={
            "challenge_id": issued["challenge_id"],
            "device_id": device.device_id,
            "nonce": issued["nonce"],
            "signature": device.sign(
                challenge_id=issued["challenge_id"], nonce=issued["nonce"]
            ),
            "scopes": ["device.manage"],
        },
    )
    assert response.status_code == 400


def test_the_auth_endpoints_require_json(harness: Harness) -> None:
    for path in ("/v1/enrollments/claim", "/v1/auth/challenges", "/v1/auth/tokens"):
        response = harness.client.post(
            path, content=b"device_id=x", headers={"Content-Type": "text/plain"}
        )
        assert response.status_code == 400, path


# --- GET /v1/devices ---------------------------------------------------------


def test_a_plain_device_sees_only_itself(harness: Harness) -> None:
    device = harness.enroll(code=harness.issue_code())
    harness.enroll(code=harness.issue_code())
    body = harness.client.get("/v1/devices", headers=device.headers()).json()
    assert [row["device_id"] for row in body["devices"]] == [device.device_id]
    assert body["devices"][0]["is_self"] is True


def test_a_managing_device_sees_every_device(harness: Harness) -> None:
    manager = harness.enroll(code=harness.issue_code(manage=True))
    other = harness.enroll(code=harness.issue_code())
    body = harness.client.get("/v1/devices", headers=manager.headers()).json()
    assert {row["device_id"] for row in body["devices"]} == {
        manager.device_id,
        other.device_id,
    }


def test_the_device_list_carries_no_key_material(harness: Harness) -> None:
    device = harness.enroll(code=harness.issue_code())
    harness.client.put(
        f"/v1/devices/{device.device_id}/push-token",
        json={"push_token": PUSH_TOKEN},
        headers=device.headers(),
    )
    row = harness.client.get("/v1/devices", headers=device.headers()).json()["devices"][0]
    assert set(row) == {
        "device_id",
        "display_name",
        "status",
        "is_self",
        "has_push_token",
        "allowed_tools_version",
        "created_at",
        "revoked_at",
    }
    assert row["has_push_token"] is True
    assert PUSH_TOKEN not in str(row)


def test_a_device_without_the_self_read_scope_is_refused(harness: Harness) -> None:
    device = harness.enroll(code=harness.issue_code())
    harness.set_scopes(device.device_id, [SELF_REVOKE_SCOPE])
    assert harness.client.get("/v1/devices", headers=device.headers()).status_code == 403


# --- DELETE /v1/devices/{id} ------------------------------------------------


def test_a_device_can_revoke_itself_and_is_refused_immediately_after(
    harness: Harness,
) -> None:
    device = harness.enroll(code=harness.issue_code())
    headers = device.headers()  # minted while still active
    response = harness.client.delete(f"/v1/devices/{device.device_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "revoked"
    # The token is still cryptographically valid; the device is not.
    assert harness.client.get("/v1/capabilities", headers=headers).status_code == 401


def test_revoking_another_device_needs_device_manage(harness: Harness) -> None:
    first = harness.enroll(code=harness.issue_code())
    second = harness.enroll(code=harness.issue_code())
    refused = harness.client.delete(
        f"/v1/devices/{second.device_id}", headers=first.headers()
    )
    assert refused.status_code == 403

    manager = harness.enroll(code=harness.issue_code(manage=True))
    allowed = harness.client.delete(
        f"/v1/devices/{second.device_id}", headers=manager.headers()
    )
    assert allowed.status_code == 200


def test_self_revocation_needs_the_self_revoke_scope(harness: Harness) -> None:
    device = harness.enroll(code=harness.issue_code())
    harness.set_scopes(device.device_id, [SELF_READ_SCOPE])
    response = harness.client.delete(
        f"/v1/devices/{device.device_id}", headers=device.headers()
    )
    assert response.status_code == 403


def test_revoking_an_unknown_device_is_a_400(harness: Harness) -> None:
    manager = harness.enroll(code=harness.issue_code(manage=True))
    response = harness.client.delete(
        "/v1/devices/018f0000-dead-4000-8000-000000000000",
        headers=manager.headers(),
    )
    assert response.status_code == 400


def test_revoking_twice_is_not_an_error(harness: Harness) -> None:
    manager = harness.enroll(code=harness.issue_code(manage=True))
    other = harness.enroll(code=harness.issue_code())
    headers = manager.headers()
    first = harness.client.delete(f"/v1/devices/{other.device_id}", headers=headers)
    second = harness.client.delete(
        f"/v1/devices/{other.device_id}", headers=manager.headers()
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "revoked"


# --- PUT /v1/devices/{id}/push-token ----------------------------------------


def test_a_push_token_is_stored_sealed_and_never_returned(harness: Harness) -> None:
    device = harness.enroll(code=harness.issue_code())
    response = harness.client.put(
        f"/v1/devices/{device.device_id}/push-token",
        json={"push_token": PUSH_TOKEN},
        headers=device.headers(),
    )
    assert response.status_code == 200
    assert response.json() == {"device_id": device.device_id, "has_push_token": True}

    with harness.sessions() as session:
        envelope = session.get(Device, device.device_id).encrypted_push_token
    assert PUSH_TOKEN not in str(envelope)
    assert harness.keyring.decrypt(
        envelope,
        table="devices",
        column="encrypted_push_token",
        row_id=device.device_id,
    ) == PUSH_TOKEN.encode("ascii")


def test_a_push_token_can_be_cleared(harness: Harness) -> None:
    device = harness.enroll(code=harness.issue_code())
    headers = device.headers()
    harness.client.put(
        f"/v1/devices/{device.device_id}/push-token",
        json={"push_token": PUSH_TOKEN},
        headers=headers,
    )
    response = harness.client.put(
        f"/v1/devices/{device.device_id}/push-token",
        json={"push_token": None},
        headers=device.headers(),
    )
    assert response.json() == {"device_id": device.device_id, "has_push_token": False}
    with harness.sessions() as session:
        assert session.get(Device, device.device_id).encrypted_push_token is None


def test_even_a_managing_device_cannot_set_another_devices_push_token(
    harness: Harness,
) -> None:
    """A push token decides where a notification lands; `device.manage` does not
    extend to redirecting another device's notifications."""
    manager = harness.enroll(code=harness.issue_code(manage=True))
    other = harness.enroll(code=harness.issue_code())
    response = harness.client.put(
        f"/v1/devices/{other.device_id}/push-token",
        json={"push_token": PUSH_TOKEN},
        headers=manager.headers(),
    )
    assert response.status_code == 403
    with harness.sessions() as session:
        assert session.get(Device, other.device_id).encrypted_push_token is None


@pytest.mark.parametrize(
    "push_token",
    ["", "z" * 64, "a" * 63, "a" * 257, 12345, "A" * 63 + "!"],
)
def test_a_malformed_push_token_is_refused(harness: Harness, push_token) -> None:
    device = harness.enroll(code=harness.issue_code())
    response = harness.client.put(
        f"/v1/devices/{device.device_id}/push-token",
        json={"push_token": push_token},
        headers=device.headers(),
    )
    assert response.status_code == 400
    with harness.sessions() as session:
        assert session.get(Device, device.device_id).encrypted_push_token is None


def test_a_push_token_body_must_not_carry_extra_fields(harness: Harness) -> None:
    device = harness.enroll(code=harness.issue_code())
    response = harness.client.put(
        f"/v1/devices/{device.device_id}/push-token",
        json={"push_token": PUSH_TOKEN, "device_id": "someone-else"},
        headers=device.headers(),
    )
    assert response.status_code == 400
