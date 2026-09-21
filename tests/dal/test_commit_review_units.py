"""Commit review failure cases; fixture and actual disposable Git, no provider."""
import copy
import pytest
from personal_agent_dal.timeline.stages import validate_plan
from personal_agent_dal.timeline.driver import validate_result
from personal_agent_dal.worker.supervisor import SupervisorRefusal
from tests.dal.test_workflow_git_executor import repository


def plan():
    return {'schema':'dal.commit-plan/1.0','nodes':[{'stage_id':'health-sync','revision':1,
        'goal':'Acquire and persist authorized data','acceptance':['sync'],
        'commit':{'subject':'Add authorized sync and storage','in_scope':['client','API','storage'],
                  'out_of_scope':['monthly review'],'modules':['ios','backend'],
                  'verification':['sync replay and deletion tests'],'boundary_reason':'One end-to-end behavior'}}],
        'edges':[],'acceptance':['sync']}


@pytest.mark.parametrize('field',['subject','in_scope','out_of_scope','modules','verification','boundary_reason'])
def test_new_commit_plan_requires_every_boundary_field(field):
    body=plan();del body['nodes'][0]['commit'][field]
    with pytest.raises(ValueError,match='PLAN_INVALID'):validate_plan(body)


def test_new_commit_plan_valid_and_legacy_is_readable():
    assert validate_plan(plan())==plan()
    old=plan();old.pop('schema');old['nodes'][0].pop('commit')
    assert validate_plan(old)==old


@pytest.mark.parametrize('mutation',[lambda x:x.update(schema='unknown'),
    lambda x:x['nodes'][0]['commit'].update(verification=[]),
    lambda x:x['nodes'][0]['commit'].update(modules=[' ']),
    lambda x:x['nodes'][0]['commit'].update(extra='unsafe')])
def test_commit_plan_is_closed_and_nonempty(mutation):
    body=plan();mutation(body)
    with pytest.raises(ValueError,match='PLAN_INVALID'):validate_plan(body)


def candidate(executor,inputs,text='synthetic acceptance\n'):
    inputs['workspace']=executor.prepare()['manifest'];base=inputs['workspace']['base_sha']
    (executor.work/'README.md').write_text(text)
    inputs['stage']={'stage_id':'one','revision':1,'state_version':4,'goal':plan()['nodes'][0],
                     'candidate':{'head_sha':base},'review':{'unit_id':'one:1','mode':'initial','previous':None}}
    inputs['stage']['candidate']=executor.candidate(stage_text='Synthetic')['candidate']


def test_real_git_full_packet_then_only_fix_diff(repository):
    executor,inputs,_=repository;candidate(executor,inputs)
    first=executor.review_packet();assert '+synthetic acceptance' in first['patch']
    old_tree=inputs['stage']['candidate']['tree_sha']
    (executor.work/'README.md').write_text('synthetic acceptance fixed\n')
    inputs['stage']['candidate']=executor.candidate(stage_text='Fix')['candidate']
    inputs['stage']['review']={'unit_id':'one:1','mode':'incremental',
        'previous':{'candidate':{'tree_sha':old_tree},'findings':['fix the race'],'receipt_digest':'a'*64}}
    second=executor.review_packet()
    assert second['base_tree']==old_tree and '-synthetic acceptance' in second['patch']
    assert '+synthetic acceptance fixed' in second['patch']


def test_packet_refuses_unstaged_change_before_provider(repository):
    executor,inputs,_=repository;candidate(executor,inputs)
    (executor.work/'README.md').write_text('tampered')
    with pytest.raises(SupervisorRefusal,match='CANDIDATE_CHANGED'):executor.review_packet()


def test_packet_refuses_oversize_without_truncation(repository):
    executor,inputs,_=repository;candidate(executor,inputs,'x'*270000)
    with pytest.raises(SupervisorRefusal,match='REVIEW_PACKET_TOO_LARGE'):executor.review_packet()


@pytest.mark.parametrize('value',[True,-1,'2',2**63])
def test_review_runtime_usage_rejects_untrusted_shapes(value):
    result={'kind':'code_review','text':'review','candidate':{k:'a'*40 for k in ('base_sha','head_sha','tree_sha')},
            'passed':True,'findings':[],'runtime_usage':{'input_tokens':None,'output_tokens':None,'provider_requests':value}}
    with pytest.raises(ValueError,match='RESULT_INVALID'):validate_result('code_review',result)

from tests.dal.test_timeline_requests import world,submit
from tests.dal.test_timeline_stages import plan as legacy_plan
from tests.dal.stage_fixtures import reviewed_design,evidence
from personal_agent_dal.timeline.stages import StageService,stage_identity
from personal_agent_dal.timeline.commit_reviews import review_context,review_summary
from personal_agent_dal.storage.timeline_models import DevelopmentStage as Stage,DevelopmentDriverStep as Step


def test_verification_failure_is_not_a_prior_review_and_noop_fix_is_refused(world):
    requests=world[2];wf=submit(requests)['request_id'];body=legacy_plan()
    StageService(requests).freeze(workflow_id=wf,revision=1,body=body,**reviewed_design(requests,wf,body))
    ident=stage_identity(wf,'one');cand={k:'b'*40 for k in ('base_sha','head_sha','tree_sha')}
    with requests.sessions() as s,s.begin():
        stage=s.get(Stage,(ident,1));stage.tree_sha=cand['tree_sha'];stage.review_fix_cycle=1
        assert review_context(requests,s,wf,stage)['mode']=='initial'
    _,receipt=evidence(requests,wf,'code_review',dict(candidate=cand,passed=False,findings=['race']),stage=(ident,1))
    with requests.sessions() as s,s.begin():
        stage=s.get(Stage,(ident,1))
        with pytest.raises(ValueError,match='REVIEW_CANDIDATE_UNCHANGED'):review_context(requests,s,wf,stage)
        assert review_context(requests,s,wf,stage,reject_unchanged=False)['previous']['findings']==['race']
        stage.tree_sha='c'*40
        context=review_context(requests,s,wf,stage)
        assert context['mode']=='incremental' and context['previous']['receipt_digest']==receipt
        summary=review_summary(requests,s,wf,stage)
        assert summary['provider_requests'] is None and summary['legacy_reviews']==1
        other=s.get(Stage,(stage_identity(wf,'two'),1))
        assert review_context(requests,s,wf,other)['mode']=='initial'


def test_tampered_review_result_cannot_seed_incremental_context(world):
    requests=world[2];wf=submit(requests)['request_id'];body=legacy_plan()
    StageService(requests).freeze(workflow_id=wf,revision=1,body=body,**reviewed_design(requests,wf,body))
    ident=stage_identity(wf,'one');cand={k:'b'*40 for k in ('base_sha','head_sha','tree_sha')}
    step,_=evidence(requests,wf,'code_review',dict(candidate=cand,passed=False,findings=['race']),stage=(ident,1))
    with requests.sessions() as s,s.begin():
        s.get(Step,step).result_digest='f'*64
    with requests.sessions() as s:
        with pytest.raises(ValueError,match='INPUT_INTEGRITY_FAILED'):review_context(requests,s,wf,s.get(Stage,(ident,1)))


def test_driver_deduplicates_initial_review_and_blocks_empty_fix_without_new_review(world):
    from tests.dal.test_workflow_local_delivery import delivery_ready,launch,accept
    from personal_agent_dal.storage.timeline_models import DevelopmentStageWriter as Writer
    from sqlalchemy import select,func
    r,driver,wf,item=delivery_ready(world,before_code=True)
    base=item['input']['stage']['candidate']['head_sha']
    cand=dict(base_sha=base,head_sha=base,tree_sha='d'*40)
    def verified(code_item):
        accept(driver,code_item,dict(kind='candidate',text='Synthetic code',candidate=cand))
        verify=launch(driver,wf)
        accept(driver,verify,dict(kind='verification',text='checked',candidate=cand,passed=True,
            commands=[dict(argv_digest='a'*64,output_digest='b'*64,exit_code=0)]))
    verified(item)
    first=driver.tick(wf);second=driver.tick(wf)
    assert first['step_id']==second['step_id']
    review=driver.dispatch(first['step_id'],admission={'test':'signed-by-helper'})
    assert review['input']['stage']['review']['mode']=='initial'
    accept(driver,review,dict(kind='code_review',text='needs fix',candidate=cand,passed=False,findings=['race']))
    fix=launch(driver,wf)
    assert fix['input']['stage']['review']['previous']['findings']==['race']
    verified(fix)
    assert driver.tick(wf)['status']=='blocked'
    with r.sessions() as s:
        assert s.get(Writer,wf) is None
        assert s.scalar(select(func.count()).select_from(Step).where(Step.workflow_id==wf,Step.phase=='code_review'))==1


def test_commit_uses_planner_subject_with_actual_git_readback(repository):
    executor,inputs,_=repository;candidate(executor,inputs)
    result=executor.commit()
    assert executor.run(['show','-s','--format=%s',result['commit_sha']])=='Add authorized sync and storage'


def test_new_prepared_design_cannot_downgrade_to_legacy_plan(world,monkeypatch):
    from tests.dal.test_workflow_local_delivery import delivery_ready
    monkeypatch.setattr('tests.dal.test_workflow_local_delivery.plan',legacy_plan)
    with pytest.raises(ValueError,match='COMMIT_PLAN_REQUIRED'):delivery_ready(world,before_code=True)


def test_model_cannot_supply_runtime_usage_and_unknown_is_preserved():
    import json
    from personal_agent_dal.worker.workflow import observed_review_result
    result={'kind':'code_review','text':'review','candidate':{k:'a'*40 for k in ('base_sha','head_sha','tree_sha')},'passed':True,'findings':[]}
    usage={'input_tokens':10,'output_tokens':20,'provider_requests':None}
    assert observed_review_result({'report':json.dumps(result),'usage':usage})['runtime_usage']['provider_requests'] is None
    result['runtime_usage']=dict(usage,provider_requests=0)
    with pytest.raises(SupervisorRefusal,match='REVIEW_USAGE_FORGED'):observed_review_result({'report':json.dumps(result),'usage':usage})


def test_binary_patch_is_refused_without_launching_model(repository):
    executor,inputs,_=repository;candidate(executor,inputs)
    (executor.work/'blob.bin').write_bytes(b'\x00\x01')
    executor.run(['add','--all'])
    inputs['stage']['candidate']['tree_sha']=executor.run(['write-tree'])
    with pytest.raises(SupervisorRefusal,match='REVIEW_PACKET_UNSUPPORTED'):executor.review_packet()


def test_retry_attempts_do_not_multiply_incremental_review_units(world):
    from personal_agent_dal.timeline.requests import digest
    requests=world[2];wf=submit(requests)['request_id'];body=legacy_plan()
    StageService(requests).freeze(workflow_id=wf,revision=1,body=body,**reviewed_design(requests,wf,body))
    ident=stage_identity(wf,'one')
    with requests.sessions() as s,s.begin():
        for n in range(2):
            key='incomplete-review-'+str(n)
            inputs={'stage':{'review':{'mode':'incremental'},'candidate':{'tree_sha':'b'*40}}}
            s.add(Step(step_id=key,workflow_id=wf,phase='code_review',input_digest=digest(inputs),
                sealed_input=requests._seal(Step,key,'sealed_input',inputs),snapshot_id=None,
                expected_version=10+n,gate_epoch=1,cycle=n+1,status='failed',attempt_id='attempt-'+str(n),stage_id=ident,stage_revision=1))
    with requests.sessions() as s:
        summary=review_summary(requests,s,wf,s.get(Stage,(ident,1)))
        assert summary['incremental_reviews']==1 and summary['execution_attempts']==2
        assert summary['incomplete_attempts']==2 and summary['provider_requests'] is None
