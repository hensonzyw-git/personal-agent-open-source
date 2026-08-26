"""DAL-R06: the remote worker transport, against the real service and adversaries.

The failure cases here were written before the adapter, because a client for a
security boundary that is only tested against its own happy path proves nothing
(CLAUDE.md §5.1). Two counterparties are used deliberately:

* the **real** `create_app` service over an ASGI transport, so the digest fence,
  the lease fence, replay and conflict are answered by the actual authority
  rather than by a fake built from the same assumptions as the client; and
* a **hostile** `MockTransport` for the shapes a correct server never produces —
  a redirect to another host, an endless 429, a response with an extra field.

What is asserted throughout: the client fails closed, never repairs a malformed
answer, never follows a redirect, retries a bounded number of times with the
same request identity, and never lets a credential reach a log, an exception
message, or a host other than the pinned one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from personal_agent_dal.service.app import create_app
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine
from personal_agent_dal.worker import queue
from personal_agent_dal.worker.checkpoint import CHECKPOINT_SCHEMA, CheckpointBundle
from personal_agent_dal.worker.config import (
    LocalTransportConfig,
    RemoteTransportConfig,
    load_worker_config,
)
from personal_agent_dal.worker.remote import (
    BODY_DIGEST_HEADER,
    EndpointError,
    RemoteHttpAdapter,
    RemoteTransportSettings,
    read_token_expiry,
    validate_endpoint,
)
from personal_agent_dal.worker.transport import (
    JobLease,
    TransportDisabledError,
    TransportError,
)

BASE_SHA = "0" * 40
SERVICE_KEY = b"test-service-key"
ENROLLMENT_SECRET = b"test-enrollment-secret"
PINNED = "https://dws.example.invalid"


# --- harness -----------------------------------------------------------------


@pytest.fixture()
def engine(tmp_path: Path):
    eng = create_database_engine(tmp_path / "service.db")
    db.upgrade(eng)
    yield eng
    eng.dispose()


def _secret_file(tmp_path: Path, value: bytes = ENROLLMENT_SECRET, mode: int = 0o600) -> Path:
    path = tmp_path / "enrollment.secret"
    path.write_bytes(value)
    os.chmod(path, mode)
    return path


def _settings(tmp_path: Path, **overrides: Any) -> RemoteTransportSettings:
    defaults: dict[str, Any] = {
        "endpoint": PINNED,
        "worker_id": "worker-macmini-001",
        "machine_id": "macmini-001",
        "capabilities": ("coding", "verification", "checkpoint"),
        # Only written when the test did not supply its own: a test about a
        # badly-permissioned secret must not have it rewritten underneath.
        "enrollment_secret_path": overrides.get("enrollment_secret_path")
        or _secret_file(tmp_path),
        "token_cache_path": tmp_path / "token" / "cache.json",
        "checkpoint_root": tmp_path / "checkpoints",
        "ca_bundle_path": None,
        "request_timeout_seconds": 5.0,
        "retry_attempts": 2,
        "backoff_base_seconds": 0.0001,
        "backoff_max_seconds": 0.0002,
    }
    defaults.update(overrides)
    return RemoteTransportSettings(**defaults)


class SyncASGITransport(httpx.BaseTransport):
    """Drive the real ASGI app from a synchronous client.

    The adapter under test is synchronous, and its counterparty here has to be
    the actual FastAPI application rather than a stub, so each request is run
    through `ASGITransport` on its own event loop and read back into a plain
    response.
    """

    def __init__(self, app: Any) -> None:
        self._inner = httpx.ASGITransport(app=app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        outbound = httpx.Request(
            request.method, request.url, headers=request.headers, content=body
        )

        async def _run() -> tuple[int, httpx.Headers, bytes]:
            response = await self._inner.handle_async_request(outbound)
            try:
                content = b"".join([chunk async for chunk in response.aiter_raw()])
            finally:
                await response.aclose()
            return response.status_code, response.headers, content

        status, headers, content = asyncio.run(_run())
        return httpx.Response(status, headers=headers, content=content, request=request)


class RecordingTransport(httpx.BaseTransport):
    """Wraps a transport and keeps every request for later inspection."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._inner.handle_request(request)


def _service_client(engine, **app_kwargs: Any) -> tuple[httpx.Client, RecordingTransport]:
    """A client whose counterparty is the real service.

    `TestClient` is an `httpx.Client` over a synchronous transport that drives
    the real ASGI app, so the adapter here talks to the actual endpoints,
    dependencies and database — not to a stand-in shaped like them.
    """
    app = create_app(
        engine,
        service_key=SERVICE_KEY,
        enrollment_secret=ENROLLMENT_SECRET,
        **app_kwargs,
    )
    recorder = RecordingTransport(SyncASGITransport(app))
    client = httpx.Client(
        base_url=PINNED, transport=recorder, follow_redirects=False, timeout=5.0
    )
    return client, recorder


def _adapter(tmp_path: Path, client: httpx.Client, **overrides: Any) -> RemoteHttpAdapter:
    return RemoteHttpAdapter(
        _settings(tmp_path, **overrides), client=client, sleep=lambda _: None
    )


def _enqueue(engine, *, feature_id: str = "demo") -> str:
    return queue.enqueue_job(
        engine,
        feature_id=feature_id,
        repository_id="synthetic",
        base_sha=BASE_SHA,
        branch_name=f"codex/feature-{feature_id}",
        toolchain_ref="toolchain-v1",
    )


def _bundle(feature_id: str = "demo", *, patch: str = "diff") -> CheckpointBundle:
    return CheckpointBundle(
        schema_version=CHECKPOINT_SCHEMA,
        feature_id=feature_id,
        repository_id="synthetic",
        base_sha=BASE_SHA,
        head_sha=BASE_SHA,
        changed_files=("a.txt",),
        acceptance_progress=("format",),
        test_results={"format": 0},
        toolchain_ref="toolchain-v1",
        toolchain_manifest_sha256="e" * 64,
        patch=patch,
    )


# --- endpoint pinning and the config union -----------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://dws.example.invalid",  # plaintext
        "https://user:pw@dws.example.invalid",  # credentials in the URL
        "https://dws.example.invalid?x=1",  # query
        "https://dws.example.invalid#f",  # fragment
        "https:///no-host",  # no host
        "dws.example.invalid",  # not absolute
    ],
)
def test_endpoint_must_be_a_pinnable_https_origin(endpoint: str) -> None:
    with pytest.raises(EndpointError):
        validate_endpoint(endpoint)


def test_endpoint_normalises_to_a_stable_pinned_form() -> None:
    assert validate_endpoint("https://dws.example.invalid/dal/") == (
        "https://dws.example.invalid/dal"
    )


def _config_body(tmp_path: Path, transport: dict[str, Any]) -> Path:
    path = tmp_path / "worker-config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "dal.worker-config/1.1",
                "worker_id": "w1",
                "transport": transport,
                "worktree_root": str(tmp_path / "wt"),
                "checkpoint_root": str(tmp_path / "cp"),
                "kill_switch_path": str(tmp_path / "disabled"),
                "lease_ttl_seconds": 60,
                "max_attempts": 3,
                "repos": {"synthetic": {"local_path": str(tmp_path / "repo")}},
            }
        )
    )
    return path


def test_schema_1_0_is_refused_outright(tmp_path: Path) -> None:
    """No implicit "database_path means local": that fallback is the defect."""
    path = tmp_path / "old.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "dal.worker-config/1.0",
                "worker_id": "w1",
                "database_path": str(tmp_path / "db"),
                "worktree_root": str(tmp_path / "wt"),
                "checkpoint_root": str(tmp_path / "cp"),
                "kill_switch_path": str(tmp_path / "disabled"),
                "lease_ttl_seconds": 60,
                "max_attempts": 3,
                "repos": {"synthetic": {"local_path": str(tmp_path / "repo")}},
            }
        )
    )
    with pytest.raises(ValueError, match="transport"):
        load_worker_config(path)


def test_remote_transport_cannot_name_a_database(tmp_path: Path) -> None:
    path = _config_body(
        tmp_path,
        {
            "mode": "remote",
            "endpoint": PINNED,
            "machine_id": "m1",
            "capabilities": ["coding"],
            "enrollment_secret_path": str(tmp_path / "s"),
            "token_cache_path": str(tmp_path / "t"),
            "database_path": str(tmp_path / "db"),
        },
    )
    with pytest.raises(ValueError, match="database_path"):
        load_worker_config(path)


def test_local_transport_cannot_name_an_endpoint(tmp_path: Path) -> None:
    path = _config_body(
        tmp_path, {"mode": "local", "database_path": str(tmp_path / "db"), "endpoint": PINNED}
    )
    with pytest.raises(ValueError, match="endpoint"):
        load_worker_config(path)


def test_remote_transport_requires_every_required_key(tmp_path: Path) -> None:
    path = _config_body(
        tmp_path,
        {"mode": "remote", "endpoint": PINNED, "machine_id": "m1", "capabilities": ["coding"]},
    )
    with pytest.raises(ValueError, match="missing remote transport keys"):
        load_worker_config(path)


def test_plaintext_endpoint_is_refused_at_config_load(tmp_path: Path) -> None:
    path = _config_body(
        tmp_path,
        {
            "mode": "remote",
            "endpoint": "http://dws.example.invalid",
            "machine_id": "m1",
            "capabilities": ["coding"],
            "enrollment_secret_path": str(tmp_path / "s"),
            "token_cache_path": str(tmp_path / "t"),
        },
    )
    with pytest.raises(ValueError, match="https"):
        load_worker_config(path)


def test_protected_paths_are_the_credentials_in_remote_mode(tmp_path: Path) -> None:
    path = _config_body(
        tmp_path,
        {
            "mode": "remote",
            "endpoint": PINNED,
            "machine_id": "m1",
            "capabilities": ["coding"],
            "enrollment_secret_path": str(tmp_path / "s"),
            "token_cache_path": str(tmp_path / "t"),
        },
    )
    config = load_worker_config(path)
    assert isinstance(config.transport, RemoteTransportConfig)
    protected = config.transport.protected_paths()
    assert (tmp_path / "s").resolve() in protected
    assert (tmp_path / "t").resolve() in protected


def test_local_protected_path_is_the_database(tmp_path: Path) -> None:
    config = load_worker_config(
        _config_body(tmp_path, {"mode": "local", "database_path": str(tmp_path / "db")})
    )
    assert isinstance(config.transport, LocalTransportConfig)
    assert config.transport.protected_paths() == ((tmp_path / "db").resolve(),)


# --- the digest binds the exact bytes ----------------------------------------


def test_body_digest_is_the_sha256_of_the_bytes_actually_sent(
    engine, tmp_path: Path
) -> None:
    client, recorder = _service_client(engine)
    _enqueue(engine)
    _adapter(tmp_path, client).claim()

    assert recorder.requests, "no request was made"
    for request in recorder.requests:
        sent = request.read()
        assert request.headers[BODY_DIGEST_HEADER] == hashlib.sha256(sent).hexdigest()


def test_a_body_altered_after_signing_is_refused_by_the_service(
    engine, tmp_path: Path
) -> None:
    """The fence is the point: a tampered `lease_epoch` must not be accepted."""

    class Tampering(httpx.BaseTransport):
        def __init__(self, inner: httpx.BaseTransport) -> None:
            self._inner = inner

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.read())
            if "lease_epoch" in body:
                body["lease_epoch"] = body["lease_epoch"] + 1
                request = httpx.Request(
                    request.method,
                    request.url,
                    headers=request.headers,
                    content=json.dumps(body).encode(),
                )
            return self._inner.handle_request(request)

    app_client, _ = _service_client(engine)
    tampered = httpx.Client(
        base_url=PINNED,
        transport=Tampering(app_client._transport),
        follow_redirects=False,
    )
    _enqueue(engine)
    lease = _adapter(tmp_path, app_client).claim()
    assert lease is not None

    adapter = _adapter(tmp_path, tampered)
    with pytest.raises(TransportError) as caught:
        adapter.heartbeat(lease)
    assert "body_digest" in caught.value.reason


# --- a redirect must never carry a credential --------------------------------


def test_a_redirect_is_refused_and_the_token_goes_nowhere_else(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/enroll"):
            return httpx.Response(200, json=_enroll_body())
        return httpx.Response(
            302, headers={"location": "https://attacker.example.invalid/jobs/claim"}
        )

    client = httpx.Client(
        base_url=PINNED, transport=httpx.MockTransport(handler), follow_redirects=False
    )
    with pytest.raises(TransportError) as caught:
        _adapter(tmp_path, client).claim()
    assert caught.value.reason == "unexpected_redirect"
    assert {request.url.host for request in seen} == {"dws.example.invalid"}


def _enroll_body(expires_in: int = 3600) -> dict[str, Any]:
    return {
        "schema_version": "dal.worker-transport/1.0",
        "worker_id": "worker-macmini-001",
        "capabilities": ["coding", "verification", "checkpoint"],
        "token": "test-token-value",
        "token_expires_at": int(time.time()) + expires_in,
    }


# --- token custody -----------------------------------------------------------


def test_first_use_enrolls_and_caches_the_token_owner_only(
    engine, tmp_path: Path
) -> None:
    client, _ = _service_client(engine)
    adapter = _adapter(tmp_path, client)
    assert adapter.claim() is None  # no job, but the enrollment happened

    cache = _settings(tmp_path).token_cache_path
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    worker_id, expires_at = read_token_expiry(cache)
    assert worker_id == "worker-macmini-001"
    assert expires_at > int(time.time())


def test_healthcheck_reads_expiry_without_reading_the_token(
    engine, tmp_path: Path
) -> None:
    client, _ = _service_client(engine)
    _adapter(tmp_path, client).claim()
    cache = _settings(tmp_path).token_cache_path

    # The accessor returns identity and expiry only; the secret stays in the file.
    assert read_token_expiry(cache)[0] == "worker-macmini-001"
    assert "token" in json.loads(cache.read_text())


def test_a_group_readable_token_cache_is_ignored_not_used(
    engine, tmp_path: Path
) -> None:
    client, recorder = _service_client(engine)
    adapter = _adapter(tmp_path, client)
    adapter.claim()
    cache = _settings(tmp_path).token_cache_path
    os.chmod(cache, 0o640)

    enrolls_before = sum(1 for r in recorder.requests if r.url.path.endswith("/enroll"))
    _adapter(tmp_path, client).claim()
    enrolls_after = sum(1 for r in recorder.requests if r.url.path.endswith("/enroll"))
    assert enrolls_after == enrolls_before + 1
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600


def test_an_expired_cached_token_is_replaced_before_use(
    engine, tmp_path: Path
) -> None:
    client, recorder = _service_client(engine)
    cache = tmp_path / "token" / "cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps(
            {
                "schema_version": "dal.worker-token-cache/1.0",
                "worker_id": "worker-macmini-001",
                "expires_at": int(time.time()) - 1,
                "token": "stale",
            }
        )
    )
    os.chmod(cache, 0o600)

    _adapter(tmp_path, client).claim()
    assert any(r.url.path.endswith("/enroll") for r in recorder.requests)
    assert read_token_expiry(cache)[1] > int(time.time())


def test_a_rejected_token_reenrolls_exactly_once_then_fails_closed(
    tmp_path: Path,
) -> None:
    """An unbounded re-auth loop would hammer the service with a bad credential."""
    enrolls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal enrolls
        if request.url.path.endswith("/enroll"):
            enrolls += 1
            return httpx.Response(200, json=_enroll_body())
        return httpx.Response(
            401,
            json={
                "schema_version": "dal.worker-transport/1.0",
                "code": "token_invalid",
                "detail": "token_invalid",
            },
        )

    client = httpx.Client(base_url=PINNED, transport=httpx.MockTransport(handler))
    with pytest.raises(TransportError) as caught:
        _adapter(tmp_path, client).claim()
    assert caught.value.reason == "unauthorized"
    assert enrolls == 2  # the initial enrollment plus exactly one re-auth


def test_an_enrollment_secret_that_is_not_owner_only_is_refused(
    engine, tmp_path: Path
) -> None:
    client, _ = _service_client(engine)
    secret = _secret_file(tmp_path, mode=0o644)
    adapter = _adapter(tmp_path, client, enrollment_secret_path=secret)
    with pytest.raises(TransportError) as caught:
        adapter.claim()
    assert caught.value.reason == "enrollment_secret_not_owner_only"


def test_a_wrong_enrollment_secret_cannot_enroll(engine, tmp_path: Path) -> None:
    client, _ = _service_client(engine)
    secret = _secret_file(tmp_path, value=b"not-the-secret")
    adapter = _adapter(tmp_path, client, enrollment_secret_path=secret)
    with pytest.raises(TransportError) as caught:
        adapter.claim()
    assert caught.value.reason == "enroll_refused:enrollment_secret_invalid"


# --- lease fencing, cancel, conflict, replay ---------------------------------


def test_claim_carries_feature_id_and_the_branch_agrees(engine, tmp_path: Path) -> None:
    client, _ = _service_client(engine)
    _enqueue(engine, feature_id="demo")
    lease = _adapter(tmp_path, client).claim()
    assert lease is not None
    assert lease.feature_id == "demo"
    assert lease.branch_name == "codex/feature-demo"


def test_no_pending_job_is_a_quiet_none(engine, tmp_path: Path) -> None:
    client, _ = _service_client(engine)
    assert _adapter(tmp_path, client).claim() is None


def test_a_stale_epoch_heartbeat_is_dead_not_cancelled(engine, tmp_path: Path) -> None:
    client, _ = _service_client(engine)
    _enqueue(engine)
    adapter = _adapter(tmp_path, client)
    lease = adapter.claim()
    assert lease is not None
    stale = JobLease(**{**lease.__dict__, "lease_epoch": lease.lease_epoch + 1})

    outcome = adapter.heartbeat(stale)
    assert outcome == type(outcome)(alive=False, cancel_requested=False)


def test_cancel_is_observed_on_the_next_heartbeat(engine, tmp_path: Path) -> None:
    client, _ = _service_client(engine)
    _enqueue(engine)
    adapter = _adapter(tmp_path, client)
    lease = adapter.claim()
    assert lease is not None
    assert adapter.heartbeat(lease).alive

    queue.cancel_job(engine, job_id=lease.job_id)

    outcome = adapter.heartbeat(lease)
    assert outcome.cancel_requested is True
    assert outcome.alive is False


def test_a_conflicting_checkpoint_is_refused_not_overwritten(
    engine, tmp_path: Path
) -> None:
    client, _ = _service_client(engine)
    _enqueue(engine)
    adapter = _adapter(tmp_path, client)
    lease = adapter.claim()
    assert lease is not None

    assert adapter.record_checkpoint(lease, _bundle(), sequence=1).recorded
    # Same sequence, different content: an integrity conflict, not progress.
    outcome = adapter.record_checkpoint(lease, _bundle(patch="other"), sequence=1)
    assert outcome.recorded is False
    assert outcome.conflict is True


def test_an_identical_checkpoint_replay_is_accepted(engine, tmp_path: Path) -> None:
    client, _ = _service_client(engine)
    _enqueue(engine)
    adapter = _adapter(tmp_path, client)
    lease = adapter.claim()
    assert lease is not None

    first = adapter.record_checkpoint(lease, _bundle(), sequence=1)
    second = adapter.record_checkpoint(lease, _bundle(), sequence=1)
    assert first.recorded and second.recorded


def test_a_checkpoint_on_a_stale_epoch_writes_nothing(engine, tmp_path: Path) -> None:
    client, _ = _service_client(engine)
    _enqueue(engine)
    adapter = _adapter(tmp_path, client)
    lease = adapter.claim()
    assert lease is not None
    stale = JobLease(**{**lease.__dict__, "lease_epoch": lease.lease_epoch + 5})

    outcome = adapter.record_checkpoint(stale, _bundle(), sequence=1)
    assert outcome.recorded is False
    assert outcome.stale is True


def test_an_identical_result_replays_to_the_same_receipt(
    engine, tmp_path: Path
) -> None:
    client, _ = _service_client(engine)
    _enqueue(engine)
    adapter = _adapter(tmp_path, client)
    lease = adapter.claim()
    assert lease is not None

    first = adapter.submit_result(
        lease, state="succeeded", result_sha256="c" * 64, last_error=None
    )
    second = adapter.submit_result(
        lease, state="succeeded", result_sha256="c" * 64, last_error=None
    )
    assert first.accepted and not first.replay
    assert second.accepted and second.replay
    assert second.receipt_id == first.receipt_id


def test_a_different_digest_for_the_same_job_is_a_conflict(
    engine, tmp_path: Path
) -> None:
    client, _ = _service_client(engine)
    _enqueue(engine)
    adapter = _adapter(tmp_path, client)
    lease = adapter.claim()
    assert lease is not None

    adapter.submit_result(
        lease, state="succeeded", result_sha256="c" * 64, last_error=None
    )
    outcome = adapter.submit_result(
        lease, state="succeeded", result_sha256="d" * 64, last_error=None
    )
    assert outcome.accepted is False
    assert outcome.conflict is True
    # The first receipt still stands.
    assert queue.get_job(engine, job_id=lease.job_id).result_sha256 == "c" * 64


def test_a_result_for_a_cancelled_job_is_reported_as_cancelled(
    engine, tmp_path: Path
) -> None:
    client, _ = _service_client(engine)
    _enqueue(engine)
    adapter = _adapter(tmp_path, client)
    lease = adapter.claim()
    assert lease is not None
    queue.cancel_job(engine, job_id=lease.job_id)

    outcome = adapter.submit_result(
        lease, state="succeeded", result_sha256="c" * 64, last_error=None
    )
    assert outcome.accepted is False
    assert outcome.cancelled is True


def test_a_result_without_a_digest_is_refused_before_it_is_sent(
    engine, tmp_path: Path
) -> None:
    client, recorder = _service_client(engine)
    _enqueue(engine)
    adapter = _adapter(tmp_path, client)
    lease = adapter.claim()
    assert lease is not None
    before = len(recorder.requests)

    with pytest.raises(TransportError) as caught:
        adapter.submit_result(
            lease, state="failed", result_sha256=None, last_error="x"
        )
    assert caught.value.reason == "result_requires_digest"
    assert len(recorder.requests) == before


def test_a_non_worker_terminal_state_is_refused(engine, tmp_path: Path) -> None:
    client, _ = _service_client(engine)
    _enqueue(engine)
    adapter = _adapter(tmp_path, client)
    lease = adapter.claim()
    assert lease is not None
    with pytest.raises(TransportError):
        adapter.submit_result(
            lease, state="cancelled", result_sha256="c" * 64, last_error=None
        )


# --- authority signals -------------------------------------------------------


def test_the_kill_switch_stops_the_worker_claiming(engine, tmp_path: Path) -> None:
    switch = tmp_path / "disabled"
    switch.write_text("stop\n")
    client, _ = _service_client(engine, kill_switch_path=switch)
    _enqueue(engine)

    with pytest.raises(TransportDisabledError) as caught:
        _adapter(tmp_path, client).claim()
    assert caught.value.reason == "kill_switch_active"


def test_a_revoked_worker_is_refused_at_the_auth_boundary(
    engine, tmp_path: Path
) -> None:
    from personal_agent_core.timeutil import utc_now
    from personal_agent_dal.storage.engine import session_factory
    from personal_agent_dal.storage.transport_models import WorkerEnrollment

    client, _ = _service_client(engine)
    adapter = _adapter(tmp_path, client)
    adapter.claim()

    sessions = session_factory(engine)
    with sessions() as session:
        row = session.get(WorkerEnrollment, "worker-macmini-001")
        row.revoked_at = utc_now()
        session.commit()

    with pytest.raises(TransportDisabledError) as caught:
        adapter.claim()
    assert caught.value.reason.startswith("forbidden:")


def test_reclaim_is_a_local_no_op_that_contacts_nobody(
    engine, tmp_path: Path
) -> None:
    """Expiring a lease is ECS authority; a remote worker must not reach for it."""
    client, recorder = _service_client(engine)
    adapter = _adapter(tmp_path, client)
    assert adapter.reclaim_expired() == ()
    assert recorder.requests == []


# --- bounded retry, stable identity ------------------------------------------


def test_a_persistent_429_is_retried_a_bounded_number_of_times(
    tmp_path: Path,
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path.endswith("/enroll"):
            return httpx.Response(200, json=_enroll_body())
        attempts += 1
        return httpx.Response(
            429,
            json={
                "schema_version": "dal.worker-transport/1.0",
                "code": "rate_limited",
                "detail": "rate_limited",
            },
        )

    client = httpx.Client(base_url=PINNED, transport=httpx.MockTransport(handler))
    with pytest.raises(TransportError) as caught:
        _adapter(tmp_path, client, retry_attempts=2).claim()
    assert caught.value.reason == "unavailable:429"
    assert attempts == 3  # the first try plus exactly two retries


def test_a_retry_replays_the_identical_request_not_a_new_one(
    tmp_path: Path,
) -> None:
    """A retried write must be the same fact, or a lost response becomes two."""
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/enroll"):
            return httpx.Response(200, json=_enroll_body())
        bodies.append(request.read())
        if len(bodies) < 3:
            return httpx.Response(
                503,
                json={
                    "schema_version": "dal.worker-transport/1.0",
                    "code": "unavailable",
                    "detail": "unavailable",
                },
            )
        return httpx.Response(204)

    client = httpx.Client(base_url=PINNED, transport=httpx.MockTransport(handler))
    assert _adapter(tmp_path, client, retry_attempts=3).claim() is None
    assert len(bodies) == 3
    assert len(set(bodies)) == 1


def test_a_kill_switch_503_is_not_retried_as_an_outage(tmp_path: Path) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path.endswith("/enroll"):
            return httpx.Response(200, json=_enroll_body())
        attempts += 1
        return httpx.Response(
            503,
            json={
                "schema_version": "dal.worker-transport/1.0",
                "code": "kill_switch_active",
                "detail": "kill_switch_active",
            },
        )

    client = httpx.Client(base_url=PINNED, transport=httpx.MockTransport(handler))
    with pytest.raises(TransportDisabledError):
        _adapter(tmp_path, client, retry_attempts=3).claim()
    assert attempts == 1


# --- responses are validated, never repaired ---------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda body: {**body, "surprise": 1}, id="extra_field"),
        pytest.param(
            lambda body: {k: v for k, v in body.items() if k != "feature_id"},
            id="missing_feature_id",
        ),
        pytest.param(
            lambda body: {**body, "schema_version": "dal.worker-transport/2.0"},
            id="wrong_schema_version",
        ),
        pytest.param(lambda body: {**body, "lease_epoch": -1}, id="negative_epoch"),
        pytest.param(lambda body: {**body, "deadline": "not-a-time"}, id="bad_deadline"),
    ],
)
def test_a_claim_response_off_contract_is_refused(tmp_path: Path, mutate) -> None:
    good = {
        "schema_version": "dal.worker-transport/1.0",
        "job_id": "job-1",
        "feature_id": "demo",
        "repository_id": "synthetic",
        "base_sha": BASE_SHA,
        "branch_name": "codex/feature-demo",
        "toolchain_ref": "toolchain-v1",
        "lease_epoch": 1,
        "attempt": 0,
        "deadline": "2026-08-26T12:00:00+00:00",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/enroll"):
            return httpx.Response(200, json=_enroll_body())
        return httpx.Response(200, json=mutate(good))

    client = httpx.Client(base_url=PINNED, transport=httpx.MockTransport(handler))
    with pytest.raises(TransportError):
        _adapter(tmp_path, client).claim()


def test_an_unparseable_response_is_not_guessed_at(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/enroll"):
            return httpx.Response(200, json=_enroll_body())
        return httpx.Response(200, content=b"<html>gateway</html>")

    client = httpx.Client(base_url=PINNED, transport=httpx.MockTransport(handler))
    with pytest.raises(TransportError) as caught:
        _adapter(tmp_path, client).claim()
    assert caught.value.reason == "unparseable_response"


# --- credentials stay out of logs and messages -------------------------------


def test_no_credential_reaches_a_log_or_an_error_message(
    engine, tmp_path: Path, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    client, _ = _service_client(engine)
    adapter = _adapter(tmp_path, client)
    adapter.claim()
    token = json.loads(_settings(tmp_path).token_cache_path.read_text())["token"]

    _enqueue(engine)
    lease = adapter.claim()
    assert lease is not None
    stale = JobLease(**{**lease.__dict__, "lease_epoch": 99})
    adapter.record_checkpoint(stale, _bundle(), sequence=1)
    with pytest.raises(TransportError) as caught:
        adapter.submit_result(lease, state="failed", result_sha256=None, last_error="x")

    haystack = caplog.text + str(caught.value) + caught.value.reason
    assert token not in haystack
    assert ENROLLMENT_SECRET.decode() not in haystack


# --- the composition the worker CLI actually builds --------------------------


def _remote_config_path(tmp_path: Path) -> Path:
    (tmp_path / "repo" / ".git").mkdir(parents=True, exist_ok=True)
    return _config_body(
        tmp_path,
        {
            "mode": "remote",
            "endpoint": PINNED,
            "machine_id": "macmini-001",
            "capabilities": ["coding"],
            "enrollment_secret_path": str(_secret_file(tmp_path)),
            "token_cache_path": str(tmp_path / "token" / "cache.json"),
        },
    )


def test_remote_mode_never_opens_a_database(tmp_path: Path, monkeypatch) -> None:
    """The whole point of DAL-R06: a remote worker cannot reach a database.

    The engine factory is made to explode, so if the remote branch could reach
    it at all — directly or through a fallback — this test would fail rather
    than quietly open the global Workflow SQLite on the Mac mini.
    """
    from personal_agent_dal.worker import cli

    def _explode(*args: Any, **kwargs: Any):
        raise AssertionError("a remote worker must not open a database")

    monkeypatch.setattr(cli, "create_database_engine", _explode)
    config = load_worker_config(_remote_config_path(tmp_path))

    with cli._open_transport(config) as transport:  # noqa: SLF001
        assert isinstance(transport, RemoteHttpAdapter)


def test_local_mode_does_open_the_database(tmp_path: Path, monkeypatch) -> None:
    """The counterpart, proving the guard above is not vacuous."""
    from personal_agent_dal.worker import cli

    opened: list[Path] = []

    class _Engine:
        def dispose(self) -> None:
            return None

    monkeypatch.setattr(
        cli, "create_database_engine", lambda path: (opened.append(path), _Engine())[1]
    )
    config = load_worker_config(
        _config_body(tmp_path, {"mode": "local", "database_path": str(tmp_path / "db")})
    )
    with cli._open_transport(config):  # noqa: SLF001
        pass
    assert opened == [(tmp_path / "db").resolve()]


def test_remote_healthcheck_reports_identity_without_reading_credentials(
    tmp_path: Path, capsys
) -> None:
    from personal_agent_dal.worker import cli

    config = load_worker_config(_remote_config_path(tmp_path))
    assert cli._healthcheck(config) == 0  # noqa: SLF001
    out = capsys.readouterr()
    assert "token cache absent" in out.out
    assert ENROLLMENT_SECRET.decode() not in out.out + out.err


def test_remote_healthcheck_fails_on_a_world_readable_secret(
    tmp_path: Path, capsys
) -> None:
    from personal_agent_dal.worker import cli

    config = load_worker_config(_remote_config_path(tmp_path))
    os.chmod(config.transport.enrollment_secret_path, 0o644)
    assert cli._healthcheck(config) == 1  # noqa: SLF001
    assert "owner-only" in capsys.readouterr().err
