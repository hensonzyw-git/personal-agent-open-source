"""No network: adversarial server shapes at the production publication boundary."""
from types import SimpleNamespace
import httpx
import pytest
from personal_agent_dal.timeline.publication import PublicationService


def service(handler):
    adapter=SimpleNamespace(_settings=SimpleNamespace(api_base='https://api.github.com',repository='synthetic/project'),
        _headers=lambda:{'Accept':'application/vnd.github+json'},_client=httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=False,trust_env=False))
    return PublicationService(None,adapter)


def manifest():
    return dict(kind='pr',repository_id='synthetic/project',host='github.com',owner='synthetic',repository='project',
        pr_number=7,base_branch='main',base_sha='a'*40,head_sha='b'*40,
        stage_manifest={'workspace':{'branch':'refs/heads/codex/dal-synthetic'}})


def pr():
    return dict(state='open',merged=False,base=dict(sha='a'*40,ref='main',repo={'full_name':'synthetic/project'}),
        head=dict(sha='b'*40,ref='codex/dal-synthetic',repo={'full_name':'synthetic/project'}))


@pytest.mark.parametrize('mutation',[lambda p:p.update(merged=True),lambda p:p.update(state='closed'),
    lambda p:p['head'].update(sha='c'*40),lambda p:p['base'].update(sha='c'*40),
    lambda p:p['head']['repo'].update(full_name='attacker/fork'),lambda p:p['base'].update(ref='other')])
def test_fresh_pr_probe_refuses_every_binding_drift(mutation):
    payload=pr();mutation(payload)
    client=service(lambda req:httpx.Response(200,json=payload))
    with pytest.raises(ValueError,match='GITHUB_PR_DRIFT'):client.probe(manifest())


def test_probe_is_a_fresh_pinned_read_and_redirects_are_not_followed():
    calls=[]
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200,json=pr())
    client=service(handler)
    assert client.probe(manifest())['pr_number']==7
    assert calls==['https://api.github.com/repos/synthetic/project/pulls/7']
    client=service(lambda req:httpx.Response(302,headers={'location':'https://attacker.invalid'}))
    with pytest.raises(ValueError,match='UNCONFIRMED'):client.probe(manifest())


from tests.dal.test_timeline_requests import world


def registration_ready(world):
    from datetime import timedelta
    from tests.dal.test_timeline_driver import configured
    from tests.dal.test_workflow_local_delivery import accept,launch,decision
    from personal_agent_dal.timeline.operator import Authorization,register_authorization
    from personal_agent_dal.timeline.decisions import DecisionService
    r,driver,wf,item=configured(world)
    accept(driver,item,dict(kind='clarification',text='Synthetic',ready=True,questions=[],acceptance=['a']))
    register_authorization(r,Authorization(grant_id='remote-grant',request_id=wf,project_id='project',subject='device:synthetic',
        approval_evidence_ref='approval',root='/synthetic',kind='existing',display_name='Synthetic',actions=['read','write','remote_issue'],
        budget_seconds=600,expires_at=r.now()+timedelta(hours=1),registration_policy='github_issue',remote_repository='synthetic/project'),actor='operator')
    item=launch(driver,wf)
    accept(driver,item,dict(kind='project_route',text='Synthetic',candidates=item['input']['project_catalog']))
    DecisionService(r).process(**decision(r,wf,'project_selection','select','选第一个项目'))
    return driver,launch(driver,wf)


def test_issue_timeout_is_durable_unknown_and_never_reposted(world):
    driver,item=registration_ready(world);calls=[]
    def handler(request):
        calls.append(request.method)
        raise httpx.ReadTimeout('synthetic response loss',request=request)
    client=service(handler);client.driver=driver
    payload={'operation':'issue'}
    proof=driver.proof(item['step_id'],'dal.workflow-publication/1.0',payload)
    for _ in range(2):
        with pytest.raises(ValueError,match='GITHUB_RECONCILIATION_REQUIRED'):
            client.perform(step_id=item['step_id'],worker_id='synthetic',assertion=proof,payload=payload)
    assert calls==['POST']


def test_registration_requires_remote_evidence_and_reads_back_actual_issue(world):
    from tests.dal.test_workflow_local_delivery import accept
    from personal_agent_dal.timeline.requests import digest
    driver,item=registration_ready(world);writes=[]
    def handler(request):
        if request.method=='POST':
            import json
            writes.append(json.loads(request.content))
            return httpx.Response(201,json={'number':7})
        return httpx.Response(200,json={'number':7,'state':'open','body':writes[0]['body']})
    client=service(handler);client.driver=driver
    result=dict(kind='registration',text='Synthetic',project_id='project',grant_digest=item['input']['project']['grant_digest'],policy='github_issue',tracker_receipt='a'*64)
    with pytest.raises(ValueError,match='REMOTE_EVIDENCE_REQUIRED'):accept(driver,item,result)
    payload={'operation':'issue'}
    observed=client.perform(step_id=item['step_id'],worker_id='synthetic',assertion=driver.proof(item['step_id'],'dal.workflow-publication/1.0',payload),payload=payload)
    result['tracker_receipt']=digest(observed)
    assert accept(driver,item,result)['status']=='completed'
    assert len(writes)==1


def test_background_publication_is_scheduled_once_and_polls_durable_result(world):
    driver,item=registration_ready(world)
    calls=[];scheduled=[]
    def handler(request):
        calls.append(request.method)
        return httpx.Response(201 if request.method=='POST' else 200,json={
            'number':7,'state':'open','body':'DAL workflow reference: '+item['input']['owner']['workflow_id']})
    client=service(handler);client.driver=driver
    payload={'operation':'issue'}
    def poll():
        return client.perform(step_id=item['step_id'],worker_id='synthetic',
            assertion=driver.proof(item['step_id'],'dal.workflow-publication/1.0',payload),
            payload=payload,schedule=scheduled.append)
    assert poll()=={'status':'pending'}
    assert poll()=={'status':'pending'}
    assert calls==[] and len(scheduled)==1
    scheduled[0]()
    assert poll()=={'repository_id':'synthetic/project','issue_number':7}
    assert calls==['POST','GET'] and len(scheduled)==1


def test_existing_delivery_publishes_before_fence_and_frozen_prepare_is_read_only(world,monkeypatch):
    from tests.dal.test_workflow_local_delivery import delivery_ready,launch,accept
    from personal_agent_dal.storage.timeline_models import DevelopmentRemoteEffect as Effect,DevelopmentGate as Gate
    from personal_agent_dal.timeline.requests import digest
    r,driver,wf,commits=delivery_ready(world,existing=True,before_delivery=True)
    item=launch(driver,wf)
    assert item['input']['phase']=='delivery_publication'
    with r.sessions() as session:assert session.get(Gate,wf).mode=='open'
    remote=dict(kind='pr',repository_id='synthetic/project',host='github.com',owner='synthetic',repository='project',
        pr_number=7,base_branch='main',base_sha='1'*40)
    manifest=dict(remote,stage_manifest=item['input']['delivery'],head_sha='3'*40,tree_sha='6'*40,
        commits=commits,clean=True,untracked_digest=digest([]))
    # Synthetic remote receipt isolates this state/fence test from GitHub.
    with r.sessions() as session,session.begin():
        session.add(Effect(step_id=item['step_id'],operation='publish',payload_digest='a'*64,status='completed',
            sealed_result=r._seal(Effect,item['step_id'],'sealed_result',remote)))
    accept(driver,item,dict(kind='delivery',text='Synthetic published PR',manifest=manifest))
    item=launch(driver,wf)
    assert item['input']['phase']=='delivery_prepare'
    assert item['input']['published_manifest']==manifest
    with r.sessions() as session:assert session.get(Gate,wf).mode=='paused'
    client=service(lambda req:pytest.fail('frozen preparation performed a remote write'));client.driver=driver
    monkeypatch.setattr('personal_agent_dal.timeline.publication.decode_bundle',lambda *args:[])
    payload=dict(operation='publish',manifest=manifest,objects=[])
    with pytest.raises(ValueError,match='GITHUB_AUTHORITY_INVALID'):
        client.perform(step_id=item['step_id'],worker_id='synthetic',payload=payload,
            assertion=driver.proof(item['step_id'],'dal.workflow-publication/1.0',payload))
    client.probe=lambda observed:remote
    payload=dict(operation='probe',manifest=manifest)
    assert client.perform(step_id=item['step_id'],worker_id='synthetic',payload=payload,
        assertion=driver.proof(item['step_id'],'dal.workflow-publication/1.0',payload))==remote
    assert accept(driver,item,dict(kind='delivery',text='Synthetic fresh PR readback',manifest=manifest))['status']=='completed'


def test_lost_issue_response_is_reconciled_by_read_only_exact_reference(world):
    from personal_agent_dal.timeline.requests import digest
    driver,item=registration_ready(world);calls=[]
    def lost(request):
        calls.append(request.method)
        raise httpx.ReadTimeout('synthetic response loss',request=request)
    client=service(lost);client.driver=driver;payload={'operation':'issue'}
    with pytest.raises(ValueError,match='RECONCILIATION_REQUIRED'):
        client.perform(step_id=item['step_id'],worker_id='synthetic',payload=payload,
            assertion=driver.proof(item['step_id'],'dal.workflow-publication/1.0',payload))
    def readback(request):
        calls.append(request.method)
        return httpx.Response(200,json=[dict(number=7,state='open',body='DAL workflow reference: '+item['input']['owner']['workflow_id'])])
    client.adapter._client=httpx.Client(transport=httpx.MockTransport(readback))
    result=client.reconcile(step_id=item['step_id'],payload_digest=digest(payload),actor='synthetic-operator')
    assert result==dict(repository_id='synthetic/project',issue_number=7)
    assert client.reconcile(step_id=item['step_id'],payload_digest=digest(payload),actor='synthetic-operator')==result
    assert calls==['POST','GET']
