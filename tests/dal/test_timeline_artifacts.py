import hashlib
import pytest
from sqlalchemy import select
from tests.dal.test_timeline_requests import world,submit
from personal_agent_dal.timeline.artifacts import ArtifactService
from personal_agent_dal.storage.timeline_models import DevelopmentDriverStep
from personal_agent_dal.timeline.requests import digest


def source(service,workflow,text):
    result=dict(kind='prd',text=text)
    with service.sessions() as s,s.begin():
        s.add(DevelopmentDriverStep(step_id='step',workflow_id=workflow,phase='prd_authoring',input_digest='a'*64,
            sealed_input=service._seal(DevelopmentDriverStep,'step','sealed_input',{}),expected_version=1,gate_epoch=1,cycle=1,
            status='completed',attempt_id='attempt',result_digest=digest(result),sealed_result=service._seal(DevelopmentDriverStep,'step','sealed_result',result)))
    return digest(result)


def test_artifact_uses_exact_completed_result_and_full_utf8_pages(world):
    s=world[2];r=submit(s);text='合成文档。'*20000;sha=source(s,r['request_id'],text)
    artifacts=ArtifactService(s);id=artifacts.record(step_id='step',expected_result_digest=sha)
    parts=[];offset=0
    while True:
        page=artifacts.read(id,offset=offset)
        parts.append(page['text']);offset=page['next_offset']
        if offset is None:break
    assert ''.join(parts)==text
    assert page['body_sha256']==hashlib.sha256(text.encode()).hexdigest()
    assert artifacts.record(step_id='step',expected_result_digest=sha)==id


def test_secret_rejected_before_artifact_is_stored(world):
    s=world[2];r=submit(s);sha=source(s,r['request_id'],'api_key=synthetic-secret')
    with pytest.raises(ValueError,match='ARTIFACT_SECRET'):ArtifactService(s).record(step_id='step',expected_result_digest=sha)
