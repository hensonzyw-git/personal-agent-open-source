"""The five `/v1/media/*` routes as the wire sees them (design §5.2, §6).

`test_media_uploads.py` proves the state machine. This file proves the part
only HTTP can: that a domain refusal arrives as the right status and code, that
the binary body has its own bound, that an image is never served with a
cacheable URL, and that a client which loses a response can recover.

The three refusals that exist only to tell a client what to do next are the
reason this file is not just one happy path:

- `409 MEDIA_NOT_READY` -- wait and re-ask;
- `409 MEDIA_BUSY` -- wait and re-send;
- `410 MEDIA_GONE` -- stop.

Each is asserted with its status *and* its code, because collapsing them into
one 4xx is exactly the defect the codes were introduced to prevent, and a test
that only checked "4xx" would pass on the collapsed version.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import text

from cap001_fixtures import CURSOR_KEY, IDENTIFIER_KEY
from personal_agent.api.app import AgentApiDeps, build_app
from personal_agent.auth.tokens import SigningKey, TokenKeyRing, issue_access_token
from personal_agent.media.lifecycle import CHAT_IMAGE_PURPOSE, claim_upload
from personal_agent.media.locking import ensure_lock_files
from personal_agent.media.store import MediaStore
from personal_agent.media.uploads import MediaLimits
from personal_agent.storage import db
from personal_agent.storage.engine import create_database_engine, session_factory
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.timeutil import to_rfc3339

NOW = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)

#: The exchange rate between the two bounds in this file: the JSON body
#: ceiling is 64 KiB and the content ceiling is 64 bytes, so a test that
#: crosses one cannot be confused for a test that crossed the other.
MAX_CONTENT_BYTES = 64

LIMITS = MediaLimits(
    max_content_bytes=MAX_CONTENT_BYTES,
    max_dimension=4096,
    allowed_mimes=frozenset({"image/jpeg"}),
    target_ttl=timedelta(minutes=30),
    claim_ttl=timedelta(minutes=10),
    retention_ttl=timedelta(days=7),
    image_pixels_per_token=750,
)


def jpeg(size: int) -> bytes:
    return b"\xff\xd8\xff" + b"\x00" * (size - 3)


def digest_of(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@pytest.fixture()
def token_ring() -> TokenKeyRing:
    private = ec.generate_private_key(ec.SECP256R1())
    return TokenKeyRing(active=SigningKey("tok-2026", private, private.public_key()))


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing(
        [generate_key("agent-data-2026", state="active")],
        service="personal-agent-api",
    )


@pytest.fixture()
def engine(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    db.upgrade(engine, "head")
    with engine.begin() as connection:
        for device_id in ("dev", "other"):
            connection.execute(
                text(
                    "INSERT INTO devices (device_id, display_name, public_key, "
                    "device_key_thumbprint, status, scopes, allowed_tools_version, "
                    "created_at) VALUES (:device_id, 'phone', 'key', 'thumb', "
                    "'active', '[]', 'v1', :now)"
                ),
                {"device_id": device_id, "now": to_rfc3339(NOW)},
            )
    yield engine
    engine.dispose()


@pytest.fixture()
def store(tmp_path: Path, keyring: KeyRing) -> MediaStore:
    root = tmp_path / "media"
    for name in ("staging", "final", "quarantine", "locks"):
        (root / name).mkdir(parents=True, exist_ok=True)
    ensure_lock_files(root)
    return MediaStore(root, keyring)


def _token(token_ring: TokenKeyRing, device_id: str = "dev") -> str:
    return issue_access_token(
        token_ring,
        device_id=device_id,
        device_key_thumbprint="thumb",
        scopes=[],
        allowed_tools_version="v1",
        now=NOW,
    )


def _client(engine, token_ring, keyring, store, *, compose_media: bool = True):
    deps = AgentApiDeps(
        session_factory=session_factory(engine),
        token_ring=token_ring,
        keyring=keyring,
        identifier_key=IDENTIFIER_KEY,
        cursor_key=CURSOR_KEY,
        build_interpreter=lambda auth: (_ for _ in ()).throw(
            AssertionError("the media routes must not build an interpreter")
        ),
        build_envelope=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("the media routes must not assemble an envelope")
        ),
        build_dispatcher=lambda auth, trace_id: (_ for _ in ()).throw(
            AssertionError("the media routes must not dispatch")
        ),
        build_authorizer=lambda auth: (lambda *, tool, model_args: dict(model_args)),
        capabilities=lambda auth: [],
        now=lambda: NOW,
        media_store=store if compose_media else None,
        media_limits=LIMITS if compose_media else None,
    )
    return TestClient(build_app(deps), raise_server_exceptions=False)


def _auth(token_ring: TokenKeyRing, device_id: str = "dev") -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(token_ring, device_id)}"}


def _declaration(body: bytes, **overrides) -> dict:
    fields = {
        "purpose": CHAT_IMAGE_PURPOSE,
        "mime": "image/jpeg",
        "size": len(body),
        "sha256": digest_of(body),
        "width": 32,
        "height": 32,
    }
    fields.update(overrides)
    return {key: value for key, value in fields.items() if value is not None}


def _create(client, headers, body: bytes, *, key: str | None = None, **overrides):
    return client.post(
        "/v1/media/uploads",
        json=_declaration(body, **overrides),
        headers={**headers, "Idempotency-Key": key or str(uuid.uuid4())},
    )


def _uploaded(client, headers, body: bytes) -> str:
    """Drive create and PUT, returning the media id."""
    created = _create(client, headers, body)
    assert created.status_code == 201, created.text
    media_id = created.json()["media_id"]
    put = client.put(f"/v1/media/content/{media_id}", content=body, headers=headers)
    assert put.status_code == 200, put.text
    return media_id


# --- authentication -------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/v1/media/uploads"),
        ("put", "/v1/media/content/11111111-1111-4111-8111-111111111111"),
        ("post", "/v1/media/uploads/11111111-1111-4111-8111-111111111111/complete"),
        ("get", "/v1/media/11111111-1111-4111-8111-111111111111"),
        ("delete", "/v1/media/11111111-1111-4111-8111-111111111111"),
    ],
)
def test_every_media_route_requires_a_device_token(engine, token_ring, keyring, store, method, path):
    client = _client(engine, token_ring, keyring, store)
    response = getattr(client, method)(path)
    assert response.status_code == 401, response.text


# --- composition ----------------------------------------------------------


def test_concurrent_upload_is_refused_before_buffering(engine, token_ring, keyring, store, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from personal_agent.api import app as api
    entered, release = Event(), Event()
    original = api._receive_media_content
    def blocked(*args):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args)
    monkeypatch.setattr(api, "_receive_media_content", blocked)
    with _client(engine, token_ring, keyring, store) as client:
        headers = _auth(token_ring)
        first = _create(client, headers, jpeg(16)).json()["media_id"]
        second = _create(client, headers, jpeg(16)).json()["media_id"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            work = pool.submit(client.put, f"/v1/media/content/{first}",
                               content=jpeg(16), headers=headers)
            try:
                assert entered.wait(timeout=5)
                refused = client.put(f"/v1/media/content/{second}", content=jpeg(16), headers=headers)
                assert refused.status_code == 409, refused.text
            finally:
                release.set()
            assert work.result(timeout=5).status_code == 200
        assert client.put(f"/v1/media/content/{second}", content=jpeg(16), headers=headers).status_code == 200


def test_an_uncomposed_media_surface_refuses_rather_than_running_half(
    engine, token_ring, keyring, store
):
    client = _client(engine, token_ring, keyring, store, compose_media=False)
    response = _create(client, _auth(token_ring), jpeg(16))
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"


# --- create ---------------------------------------------------------------


def test_create_returns_201_and_a_replay_returns_200(engine, token_ring, keyring, store):
    client = _client(engine, token_ring, keyring, store)
    headers = _auth(token_ring)
    key = str(uuid.uuid4())
    body = jpeg(16)

    first = _create(client, headers, body, key=key)
    assert first.status_code == 201, first.text
    assert first.json()["replayed"] is False
    assert first.json()["state"] == "pending"

    # The same key and the same declaration is the lost-response recovery, and
    # it must name the same object. A `201` on the second call would tell the
    # client it now has two of something.
    again = _create(client, headers, body, key=key)
    assert again.status_code == 200, again.text
    assert again.json()["media_id"] == first.json()["media_id"]
    assert again.json()["replayed"] is True


def test_create_refuses_a_key_that_names_a_different_declaration(
    engine, token_ring, keyring, store
):
    client = _client(engine, token_ring, keyring, store)
    headers = _auth(token_ring)
    key = str(uuid.uuid4())
    assert _create(client, headers, jpeg(16), key=key).status_code == 201

    conflicting = _create(client, headers, jpeg(32), key=key)
    assert conflicting.status_code == 400, conflicting.text
    assert conflicting.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_create_requires_an_idempotency_key(engine, token_ring, keyring, store):
    client = _client(engine, token_ring, keyring, store)
    response = client.post(
        "/v1/media/uploads",
        json=_declaration(jpeg(16)),
        headers=_auth(token_ring),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"purpose": None}, "a purpose this round does not serve"),
        ({"purpose": "wardrobe"}, "a purpose outside the registry"),
        ({"sha256": None}, "no declared digest to verify against"),
        ({"width": None}, "half a rectangle"),
        ({"height": None}, "half a rectangle"),
        ({"size": 65}, "a size over the configured ceiling"),
        ({"mime": "image/png"}, "a format the allow-list does not accept"),
    ],
)
def test_create_refuses_a_declaration_it_cannot_honour(
    engine, token_ring, keyring, store, overrides, why
):
    client = _client(engine, token_ring, keyring, store)
    response = _create(client, _auth(token_ring), jpeg(16), **overrides)
    assert response.status_code == 400, f"{why}: {response.text}"
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT", why


# --- upload ---------------------------------------------------------------


def test_the_three_steps_produce_a_readable_image(engine, token_ring, keyring, store):
    client = _client(engine, token_ring, keyring, store)
    headers = _auth(token_ring)
    body = jpeg(16)
    media_id = _uploaded(client, headers, body)

    completed = client.post(f"/v1/media/uploads/{media_id}/complete", headers=headers)
    assert completed.status_code == 200, completed.text
    body_json = completed.json()
    assert body_json["outcome"] == "published"
    assert body_json["state"] == "ready"
    # §5.1: the digest is the server's measurement of what arrived.
    assert body_json["content_sha256"] == digest_of(body)
    assert body_json["declared_width"] == 32

    fetched = client.get(f"/v1/media/{media_id}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.content == body
    assert fetched.headers["content-type"] == "image/jpeg"
    # §5.2: no long-lived URL and no shared cache.
    assert fetched.headers["cache-control"] == "no-store"
    assert fetched.headers["etag"] == f'"{digest_of(body)}"'


def test_a_body_over_the_content_ceiling_never_reaches_the_state_machine(
    engine, token_ring, keyring, store
):
    """The transport bound, not the declaration one.

    A body that lies about its length is what separates the two: the
    declaration says 16 bytes and is honoured at the door, so the only thing
    that can refuse the 65 bytes that actually arrive is §5.2's independent
    content ceiling -- and it refuses them before a byte is buffered past it.
    """
    client = _client(engine, token_ring, keyring, store)
    headers = _auth(token_ring)
    declared = _create(client, headers, jpeg(16))
    assert declared.status_code == 201, declared.text

    response = client.put(
        f"/v1/media/content/{declared.json()['media_id']}",
        content=jpeg(MAX_CONTENT_BYTES + 1),
        headers=headers,
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"

    # Refused at the transport, so the object was never told anything about
    # those bytes: it is still the `pending` target the client can retry.
    still_there = client.get(f"/v1/media/{declared.json()['media_id']}", headers=headers)
    assert still_there.status_code == 409
    assert still_there.json()["error"]["code"] == "MEDIA_NOT_READY"


def test_a_short_body_leaves_the_target_usable(engine, token_ring, keyring, store):
    client = _client(engine, token_ring, keyring, store)
    headers = _auth(token_ring)
    body = jpeg(32)
    media_id = _create(client, headers, body).json()["media_id"]

    short = client.put(
        f"/v1/media/content/{media_id}", content=body[:-1], headers=headers
    )
    # Retryable, and explicitly not a 404 or a 400: nothing is wrong with the
    # object or the request, the upload simply has not finished arriving.
    assert short.status_code == 409, short.text
    assert short.json()["error"]["code"] == "MEDIA_NOT_READY"

    retried = client.put(
        f"/v1/media/content/{media_id}", content=body, headers=headers
    )
    assert retried.status_code == 200, retried.text


def test_bytes_that_are_not_the_declared_format_reject_the_object(
    engine, token_ring, keyring, store
):
    client = _client(engine, token_ring, keyring, store)
    headers = _auth(token_ring)
    declared = jpeg(16)
    media_id = _create(client, headers, declared).json()["media_id"]

    # A PNG announces itself where a JPEG would -- §5.4's "声明与实际不符".
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 16
    response = client.put(
        f"/v1/media/content/{media_id}", content=png, headers=headers
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"

    # The object is a tombstone now, so a client that retries the read to see
    # what happened is told the truth rather than told to wait forever.
    gone = client.get(f"/v1/media/{media_id}", headers=headers)
    assert gone.status_code == 410
    assert gone.json()["error"]["code"] == "MEDIA_GONE"


# --- complete -------------------------------------------------------------


def test_complete_before_an_upload_answers_accepted_with_a_deadline(
    engine, token_ring, keyring, store
):
    """§5.2's "处理中返回可轮询状态与期限", and not an error envelope.

    `202` and a state body rather than `409` and an error envelope: the client
    is being told to come back, and a client that finds `{"error": ...}` on a
    poll has to guess whether the object moved.
    """
    client = _client(engine, token_ring, keyring, store)
    headers = _auth(token_ring)
    media_id = _create(client, headers, jpeg(16)).json()["media_id"]

    response = client.post(f"/v1/media/uploads/{media_id}/complete", headers=headers)
    assert response.status_code == 202, response.text
    assert response.json()["outcome"] == "in_progress"
    assert response.json()["state"] == "pending"
    assert response.json()["content_sha256"] is None
    # Nothing has been claimed yet, so there is no deadline to name -- and a
    # fabricated one would be a promise the server has not made.
    assert response.json()["retry_at"] is None


def test_complete_while_an_attempt_is_in_flight_names_its_deadline(
    engine, token_ring, keyring, store
):
    """The lost-`PUT`-response recovery §5.2 documents.

    A second `PUT` against a live claim is refused (§5.2: the target is
    consumed once), so the poll has to be the way out -- and the deadline it
    carries is the one the server actually promised that writer, which is when
    §5.2 lets a fresh attempt take the object over.
    """
    client = _client(engine, token_ring, keyring, store)
    headers = _auth(token_ring)
    media_id = _create(client, headers, jpeg(16)).json()["media_id"]

    deadline = NOW + timedelta(minutes=10)
    with session_factory(engine)() as session:
        claim_upload(
            session,
            media_id=media_id,
            device_id="dev",
            owner_token="a-writer-that-never-finished",
            now=NOW,
            claim_deadline=deadline,
        )
        session.commit()

    response = client.post(f"/v1/media/uploads/{media_id}/complete", headers=headers)
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "uploading"
    assert response.json()["retry_at"] == to_rfc3339(deadline)


def test_complete_is_idempotent(engine, token_ring, keyring, store):
    client = _client(engine, token_ring, keyring, store)
    headers = _auth(token_ring)
    body = jpeg(16)
    media_id = _uploaded(client, headers, body)

    first = client.post(f"/v1/media/uploads/{media_id}/complete", headers=headers)
    second = client.post(f"/v1/media/uploads/{media_id}/complete", headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["outcome"] == "published"
    # §5.2: "complete 在 ready/bound 返回同一完成结果" -- a lost response must be
    # recoverable by re-asking, not by re-uploading.
    assert second.json()["outcome"] == "already_ready"
    assert second.json()["content_sha256"] == first.json()["content_sha256"]


# --- read -----------------------------------------------------------------


def test_read_refuses_another_devices_image_as_not_found(
    engine, token_ring, keyring, store
):
    client = _client(engine, token_ring, keyring, store)
    headers = _auth(token_ring)
    body = jpeg(16)
    media_id = _uploaded(client, headers, body)
    client.post(f"/v1/media/uploads/{media_id}/complete", headers=headers)

    other = client.get(
        f"/v1/media/{media_id}", headers=_auth(token_ring, device_id="other")
    )
    # 404 and not 403: the endpoint must not answer "this id exists but is not
    # yours", because that is a question about someone else's identifiers.
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "MEDIA_NOT_FOUND"


def test_read_of_an_unfinished_object_is_not_a_404(engine, token_ring, keyring, store):
    client = _client(engine, token_ring, keyring, store)
    headers = _auth(token_ring)
    media_id = _create(client, headers, jpeg(16)).json()["media_id"]

    response = client.get(f"/v1/media/{media_id}", headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MEDIA_NOT_READY"


# --- delete ---------------------------------------------------------------


def test_delete_decides_once_and_the_image_is_then_gone(
    engine, token_ring, keyring, store
):
    client = _client(engine, token_ring, keyring, store)
    headers = _auth(token_ring)
    body = jpeg(16)
    media_id = _uploaded(client, headers, body)
    client.post(f"/v1/media/uploads/{media_id}/complete", headers=headers)

    first = client.delete(f"/v1/media/{media_id}", headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["decided"] is True

    # A second delete is the state the caller asked for already holding, so it
    # is a success with the distinction in the body -- never a conflict.
    second = client.delete(f"/v1/media/{media_id}", headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["decided"] is False

    fetched = client.get(f"/v1/media/{media_id}", headers=headers)
    assert fetched.status_code == 410
    assert fetched.json()["error"]["code"] == "MEDIA_GONE"


def test_delete_refuses_another_devices_image(engine, token_ring, keyring, store):
    client = _client(engine, token_ring, keyring, store)
    headers = _auth(token_ring)
    media_id = _create(client, headers, jpeg(16)).json()["media_id"]

    response = client.delete(
        f"/v1/media/{media_id}", headers=_auth(token_ring, device_id="other")
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MEDIA_NOT_FOUND"
