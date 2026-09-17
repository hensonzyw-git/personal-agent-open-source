"""Narrow offline regressions for the final transport/config boundaries."""
import gzip
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func, text
from tests.integration.test_dal_resume_bridge import bridge_world
from tests.integration.test_dal_supervised_dispatch import composed
from tests.dal.test_p0_02_review_regressions import world
from tests.dal.test_p0_02_resume_authority import setup_resume
from personal_agent.api.dal_client import FixedDalTransport
from personal_agent.api.dal_resume import ProposalRequest
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.machine_models import Lease, ProviderAttempt, ResumeEpisode
from personal_agent_dal.service.app import create_app


@pytest.mark.parametrize('size', [65536, 65537, 131072])
@pytest.mark.parametrize('compressed', [False, True])
def test_response_stream_stops_at_decoded_limit(monkeypatch, size, compressed):
    class Stream(httpx.SyncByteStream):
        reads = 0
        closed = False
        def __iter__(self):
            payload = b'"' + b'x' * (size - 2) + b'"'
            if compressed:
                # Compressed wire size is small; the limit must use decoded size.
                chunks = [gzip.compress(payload)]
            else:
                chunks = [payload[i:i+4096] for i in range(0, len(payload), 4096)]
            for chunk in chunks:
                self.reads += 1
                yield chunk
            if size > 65536:
                self.reads += 1
                pytest.fail('read beyond the first oversized decoded chunk')
        def close(self): self.closed = True
    stream = Stream()
    def handler(request):
        assert str(request.url) == 'https://dal.example/internal/human-decisions'
        assert request.method == 'POST'
        return httpx.Response(200, stream=stream, headers={'content-encoding':'gzip'} if compressed else {})
    client = httpx.Client
    def factory(**kwargs):
        assert kwargs == dict(trust_env=False, follow_redirects=False, timeout=10)
        return client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(httpx, 'Client', factory)
    transport = FixedDalTransport(base_url='https://dal.example', key=None, kid='k', issuer='pa', audience='dal')
    if size > 65536:
        with pytest.raises(ValueError, match='^DAL_RESPONSE_TOO_LARGE$'):
            transport.deliver('synthetic')
    else:
        assert transport.deliver('synthetic') == 'x' * (size - 2)
    assert stream.reads == (1 if compressed else min((size+4095)//4096, 17))
    assert stream.closed


@pytest.mark.parametrize('bad', [None, [], 4, {'extra':True}, 'naive', 'malformed', 'offset', 'missing', 'binding', 'id', 'extra'])
def test_bad_proposal_response_is_closed(bridge_world, bad):
    _, bridge, auth, p, transport = bridge_world
    response = dict(p)
    if bad == 'naive': response['expires_at'] = '2099-01-01T00:00:00'
    elif bad == 'malformed': response['expires_at'] = 'invalid'
    elif bad == 'offset': response['expires_at'] = '2099-01-01T00:00:00+08:00'
    elif bad == 'missing': response.pop('expires_at')
    elif bad == 'binding': response['binding'] = []
    elif bad == 'id': response['proposal_id'] = []
    elif bad == 'extra': response['extra'] = True
    else: response = bad
    transport.propose = lambda _: response
    with pytest.raises(ValueError, match='^PROPOSAL_RESPONSE_INVALID$'):
        bridge.proposal(auth, ProposalRequest(request_id='bad-response', feature_id='f', selection_id=p['binding']['selection_id']))
    # Already persisted replay never consults the now-invalid remote response.
    assert bridge.proposal(auth, ProposalRequest(request_id='pa-proposal', feature_id='f', selection_id=p['binding']['selection_id'])) == p


def test_consumed_result_refuses_new_proposal(world):
    from personal_agent_dal.machine.resume_authority import propose
    attempt, p, _, _ = setup_resume(world)
    with session_factory(world)() as s, s.begin():
        s.get(ProviderAttempt, attempt.attempt_id).result_consumed_at = utc_now()
    with pytest.raises(ValueError, match='^ATTEMPT_INVALID$'):
        propose(world, feature_id='f', selection_id=p['binding']['selection_id'], request_id='after-consumption')


def test_config_off_existing_episode_blocks_policy_lease(composed, world, monkeypatch):
    transport, lease, *_ = composed
    with session_factory(world)() as s:
        assert s.scalar(select(ResumeEpisode).where(ResumeEpisode.job_id == lease.job_id))
        before = s.scalar(select(func.count()).select_from(Lease))
    transport._client = TestClient(create_app(world, service_key=b'synthetic-service', enrollment_secret=b'synthetic-enroll', resume_config=None))
    with pytest.raises(RuntimeError): transport.prelaunch_context(lease)
    with session_factory(world)() as s:
        assert s.scalar(select(func.count()).select_from(Lease)) == before
    from dataclasses import replace
    from personal_agent_dal.storage.worker_models import WorkerJob
    # The unrelated original job is an explicitly non-provider synthetic fixture.
    with session_factory(world)() as s, s.begin():
        s.get(WorkerJob, 'j').execution_mode = 'legacy_non_provider'
    assert transport.prelaunch_context(replace(lease, job_id='j', lease_epoch=3)) is None
    def forbidden(*args, **kwargs): pytest.fail('disabled claim consumed pending intent')
    monkeypatch.setattr('personal_agent_dal.machine.resume_dispatch.consume_pending', forbidden)
    transport.claim()


def test_current_dal_migrated_schema_matches_models(world):
    from alembic.migration import MigrationContext
    from alembic.autogenerate import compare_metadata
    from personal_agent_dal.storage.models import Base
    with world.connect() as connection:
        assert connection.scalar(text('SELECT version_num FROM alembic_version')) == '0021'
        tables = {'resume_lease_issuances', 'resume_revoke_receipts'}
        def include_object(obj, name, type_, reflected, compare_to):
            return (name if type_ == 'table' else obj.table.name) in tables
        assert compare_metadata(MigrationContext.configure(connection, opts={'include_object': include_object}), Base.metadata) == []
        from sqlalchemy import inspect
        checks = inspect(connection).get_check_constraints('resume_lease_issuances')
        assert [check['sqltext'] for check in checks] == ['job_lease_epoch >= 1']


def test_disabled_claim_leaves_queued_intent_unconsumed(world):
    from types import SimpleNamespace
    from tests.dal.test_resume_dispatch import replacement
    from personal_agent_dal.storage.transport_models import WorkerEnrollment
    from personal_agent_dal.storage.worker_models import WorkerJob
    from personal_agent_dal.worker.remote import RemoteHttpAdapter, CachedToken
    from personal_agent_dal.service.tokens import issue_token
    intent, _ = replacement(world)
    with session_factory(world)() as s, s.begin():
        s.add(WorkerEnrollment(worker_id='w', machine_id='synthetic-mini', capabilities='[]', created_at=utc_now()))
        jobs_before = s.scalar(select(func.count()).select_from(WorkerJob))
    app = create_app(world, service_key=b'synthetic-service', enrollment_secret=b'synthetic-enroll', resume_config=None)
    with TestClient(app) as client:
        transport = RemoteHttpAdapter(SimpleNamespace(worker_id='w', endpoint='https://testserver', retry_attempts=1), client=client)
        expires = int(utc_now().timestamp()) + 600
        transport._token = CachedToken('w', issue_token(worker_id='w', capabilities=[], key=b'synthetic-service', expires_at_epoch=expires), expires)
        transport.claim()
    with session_factory(world)() as s:
        assert s.get(ResumeEpisode, intent) is None
        assert s.scalar(select(func.count()).select_from(WorkerJob)) == jobs_before
