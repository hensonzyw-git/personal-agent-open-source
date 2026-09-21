"""ECS-only, durable one-shot GitHub effects for workflow-owned deliveries.

A started/unknown write is never repeated by this entrypoint. Object hashes,
remote branch readback and the exact open PR are required before completion.
"""
from urllib.parse import quote
import httpx
from sqlalchemy import select
from personal_agent_dal.storage.timeline_models import DevelopmentRemoteEffect as Effect,DevelopmentDriverStep as Step
from personal_agent_dal.timeline.requests import digest
from personal_agent_dal.github.adapter import AdapterRefusal
from personal_agent_dal.github.workflow_objects import decode_bundle


class PublicationService:
    def __init__(self,driver,adapter):self.driver,self.adapter=driver,adapter

    def call(self,method,path,*,data=None,status=200):
        headers=self.adapter._headers()
        if isinstance(headers,AdapterRefusal):raise ValueError('GITHUB_CREDENTIAL_UNAVAILABLE')
        url=self.adapter._settings.api_base+'/repos/'+self.adapter._settings.repository+path
        response=self.adapter._client.request(method,url,headers=headers,**({'json':data} if data is not None else {}))
        if response.status_code!=status:raise ValueError('GITHUB_EFFECT_UNCONFIRMED')
        return response.json()

    def perform(self,*,step_id,worker_id,assertion,payload,schedule=None):
        if self.adapter is None:raise ValueError('GITHUB_ADAPTER_UNAVAILABLE')
        operation=payload.get('operation')
        if operation not in ('issue','publish','probe'):raise ValueError('GITHUB_OPERATION_INVALID')
        objects=None
        if operation=='publish':objects=decode_bundle(payload.get('objects'),payload.get('manifest',{}).get('commits',[]))
        def check(s, *, signed=True):
            step=s.get(Step,step_id)
            if step is None or step.status not in ('dispatch_started','result_unknown'):raise ValueError('STALE_BINDING')
            self.driver._current(s,step)
            if signed:
                execution=self.driver.authority.verify(s,step,assertion,domain='dal.workflow-publication/1.0',payload=payload)
            else:
                # The request signature was checked before durable admission.
                # Long uploads recheck live authority without extending that JWT.
                from personal_agent_dal.storage.timeline_models import DevelopmentExecution, DevelopmentRoleSnapshot
                execution=s.scalar(select(DevelopmentExecution).where(DevelopmentExecution.step_id==step_id))
                _, admission=self.driver.authority.admission(worker_id,s.get(DevelopmentRoleSnapshot,step.snapshot_id))
                if execution is None or execution.lease_until<=self.driver.r.now() or digest(admission)!=execution.admission_digest:
                    raise ValueError('RUNTIME_ADMISSION_REQUIRED')
            if execution.worker_id!=worker_id:raise ValueError('WORKER_NOT_AUTHORIZED')
            inputs=self.driver.r._open(Step,step_id,'sealed_input',step.sealed_input)
            expected={'issue':'project_registration','publish':'delivery_publication','probe':'delivery_probe'}[operation]
            grant=inputs.get('authorization',{})
            required={'read','remote_issue'} if operation=='issue' else {'read','push','pr'} if operation=='publish' else {'read'}
            if (step.phase not in ('delivery_probe','delivery_prepare') if operation=='probe' else step.phase!=expected) or not required<=set(grant.get('actions',[])) or grant.get('remote_repository')!=self.adapter._settings.repository:
                raise ValueError('GITHUB_AUTHORITY_INVALID')
            if operation=='issue':
                if set(payload)!={'operation'} or grant.get('registration_policy')!='github_issue':raise ValueError('GITHUB_AUTHORITY_INVALID')
            elif operation=='probe':
                if set(payload)!={'operation','manifest'} or payload['manifest']!=(inputs['probe']['manifest'] if step.phase=='delivery_probe' else inputs['published_manifest']):raise ValueError('GITHUB_BINDING_INVALID')
            else:
                from personal_agent_dal.timeline.delivery import manifest_inputs
                from personal_agent_dal.storage.timeline_models import DevelopmentWorkflow
                expected_manifest=manifest_inputs(self.driver.r,s,s.get(DevelopmentWorkflow,step.workflow_id))
                manifest=payload['manifest']
                if (set(payload)!={'operation','manifest','objects'} or manifest.get('stage_manifest')!=expected_manifest
                    or expected_manifest!=inputs['delivery'] or manifest.get('commits')!=expected_manifest['commit_history']
                    or manifest.get('head_sha')!=manifest['commits'][-1]['sha'] or manifest.get('tree_sha')!=manifest['commits'][-1]['tree']
                    or manifest.get('clean') is not True or manifest.get('untracked_digest')!=digest([])):
                    raise ValueError('GITHUB_BINDING_INVALID')
            return inputs
        inputs=self.driver._write(check)
        # Read-only probes are intentionally fresh on every call.
        if operation=='probe':
            result=self.probe(payload['manifest'])
            def observed(s):
                check(s)
                old=s.get(Effect,step_id)
                if old and old.payload_digest!=digest(payload):raise ValueError('IDEMPOTENCY_CONFLICT')
                record=dict(remote=result,observed_at=int(self.driver.r.now().timestamp()))
                if old is None:
                    old=Effect(step_id=step_id,operation='probe',payload_digest=digest(payload),status='completed');s.add(old)
                old.sealed_result=self.driver.r._seal(Effect,step_id,'sealed_result',record)
            self.driver._write(observed)
            return result
        def begin(s):
            check(s)
            old=s.get(Effect,step_id)
            if old:
                if old.payload_digest!=digest(payload) or old.operation!=operation:raise ValueError('IDEMPOTENCY_CONFLICT')
                if old.status=='started' and schedule is not None:return {'status':'pending'}
                if old.status!='completed':raise ValueError('GITHUB_RECONCILIATION_REQUIRED')
                return self.driver.r._open(Effect,step_id,'sealed_result',old.sealed_result)
            if operation=='issue':
                current=s.get(Step,step_id)
                previous=s.execute(select(Effect,Step).join(Step,Step.step_id==Effect.step_id).where(
                    Step.workflow_id==current.workflow_id,Effect.operation=='issue',Effect.status=='completed')).all()
                for effect, source in previous:
                    original=self.driver.r._open(Step,source.step_id,'sealed_input',source.sealed_input)
                    if original.get('project',{}).get('project_id')!=inputs['project']['project_id']:
                        continue
                    result=self.driver.r._open(Effect,effect.step_id,'sealed_result',effect.sealed_result)
                    if result.get('repository_id')!=self.adapter._settings.repository:
                        continue
                    # A later resumed step adopts the same durable external ID;
                    # losing a Worker response never authorizes a second Issue.
                    s.add(Effect(step_id=step_id,operation=operation,payload_digest=digest(payload),status='completed',
                        sealed_result=self.driver.r._seal(Effect,step_id,'sealed_result',result)))
                    return result
            s.add(Effect(step_id=step_id,operation=operation,payload_digest=digest(payload),status='started'))
            return None
        existing=self.driver._write(begin)
        if existing is not None:return existing
        def guard():self.driver._write(lambda session:check(session,signed=False))
        def execute():
            try:
                guard()
                if operation=='issue':
                    body='DAL workflow reference: '+inputs['owner']['workflow_id']
                    observed=self.call('POST','/issues',data={'title':'DAL '+inputs['owner']['workflow_id'],'body':body},status=201)
                    number=observed.get('number')
                    if type(number) is not int or number<1:raise ValueError('GITHUB_READBACK_INVALID')
                    guard();observed=self.call('GET','/issues/'+str(number))
                    if observed.get('body')!=body or observed.get('state')!='open' or observed.get('number')!=number:raise ValueError('GITHUB_READBACK_INVALID')
                    result=dict(repository_id=self.adapter._settings.repository,issue_number=number)
                else:
                    result=self.publish(inputs,payload['manifest'],objects,guard)
            except (ValueError,httpx.HTTPError,KeyError,TypeError):
                def failed(s):s.get(Effect,step_id).status='unknown'
                self.driver._write(failed)
                raise ValueError('GITHUB_RECONCILIATION_REQUIRED') from None
            def finish(s):
                row=s.get(Effect,step_id)
                row.status='completed';row.sealed_result=self.driver.r._seal(Effect,step_id,'sealed_result',result)
            self.driver._write(finish)
            return result
        if schedule is not None:
            def background():
                try:execute()
                except ValueError:pass  # durable unknown is returned by subsequent polls
            schedule(background)
            return {'status':'pending'}
        return execute()

    def reconcile(self,*,step_id,payload_digest,actor):
        """Operator readback can adopt a proven effect, never repeat a write."""
        from personal_agent_dal.storage.timeline_models import DevelopmentExecution,DevelopmentWorkflow
        if not actor or self.adapter is None:raise ValueError('GITHUB_RECONCILIATION_REQUIRED')
        with self.driver.r.sessions() as session:
            effect=session.get(Effect,step_id);step=session.get(Step,step_id)
            if effect is None or step is None or effect.payload_digest!=payload_digest:
                raise ValueError('STALE_BINDING')
            if effect.status=='completed':return self.driver.r._open(Effect,step_id,'sealed_result',effect.sealed_result)
            execution=session.scalar(select(DevelopmentExecution).where(DevelopmentExecution.step_id==step_id))
            if effect.status=='started' and (execution is None or execution.lease_until>self.driver.r.now()):
                raise ValueError('GITHUB_EFFECT_IN_FLIGHT')
            inputs=self.driver.r._open(Step,step_id,'sealed_input',step.sealed_input)
            operation=effect.operation;workflow_id=step.workflow_id
        repository=self.adapter._settings.repository
        if inputs['authorization'].get('remote_repository')!=repository:raise ValueError('GITHUB_BINDING_INVALID')
        if operation=='issue':
            body='DAL workflow reference: '+workflow_id
            matches=[]
            # Bounded complete pagination; a truncated search is never absence.
            for page in range(1,11):
                rows=self.call('GET',f'/issues?state=all&per_page=100&page={page}')
                if not isinstance(rows,list):raise ValueError('GITHUB_READBACK_INVALID')
                matches.extend(row for row in rows if isinstance(row,dict) and 'pull_request' not in row and row.get('body')==body)
                if len(rows)<100:break
            else:raise ValueError('GITHUB_RECONCILIATION_REQUIRED')
            if len(matches)!=1 or matches[0].get('state')!='open' or type(matches[0].get('number')) is not int:
                raise ValueError('GITHUB_RECONCILIATION_REQUIRED')
            result=dict(repository_id=repository,issue_number=matches[0]['number'])
        elif operation=='publish':
            owner,name=repository.split('/')
            branch=inputs['workspace']['branch'].removeprefix('refs/heads/')
            rows=self.call('GET','/pulls?state=all&per_page=100&head='+quote(owner+':'+branch,safe=''))
            if not isinstance(rows,list) or len(rows)!=1:raise ValueError('GITHUB_RECONCILIATION_REQUIRED')
            pr=rows[0];stages=inputs['delivery'];last=stages['commit_history'][-1]
            result=dict(kind='pr',repository_id=repository,host='github.com',owner=owner,repository=name,
                pr_number=pr.get('number'),base_branch=pr.get('base',{}).get('ref'),base_sha=stages['workspace']['base_sha'])
            self.probe(dict(result,stage_manifest=stages,head_sha=last['sha']))
        else:raise ValueError('GITHUB_RECONCILIATION_REQUIRED')
        def finish(session):
            current=session.get(Effect,step_id)
            if current.payload_digest!=payload_digest:raise ValueError('STALE_BINDING')
            if current.status=='completed':
                if self.driver.r._open(Effect,step_id,'sealed_result',current.sealed_result)!=result:
                    raise ValueError('GITHUB_READBACK_CONFLICT')
                return result
            current.status='completed';current.sealed_result=self.driver.r._seal(Effect,step_id,'sealed_result',result)
            wf=session.get(DevelopmentWorkflow,workflow_id)
            self.driver._event(session,wf,'workflow.remote_reconciled','远端效果已只读回查确认；继续执行仍需校验当前权限。',
                step_id=step_id,actor=actor,operation=operation,receipt_digest=digest(result))
            return result
        return self.driver._write(finish)

    def publish(self,inputs,manifest,objects,guard):
        repository=self.adapter._settings.repository
        info=self.call('GET','')
        base=inputs['project_policy']['base_branch'] if inputs.get('project_policy') else info.get('default_branch')
        if not isinstance(base,str) or not base:raise ValueError('GITHUB_BASE_INVALID')
        base_sha=inputs['delivery']['workspace']['base_sha']
        current=self.call('GET','/git/ref/heads/'+quote(base,safe=''))
        if current.get('object',{}).get('sha')!=base_sha:raise ValueError('GITHUB_BASE_DRIFT')
        for sha,kind,payload in objects:
            guard()
            observed=self.call('POST','/git/'+{'blob':'blobs','tree':'trees','commit':'commits'}[kind],data=payload,status=201)
            if observed.get('sha')!=sha:raise ValueError('GITHUB_OBJECT_READBACK_INVALID')
        branch=inputs['workspace']['branch'].removeprefix('refs/heads/')
        if branch!='codex/dal-'+inputs['owner']['workflow_id']:raise ValueError('GITHUB_BRANCH_INVALID')
        guard()
        pushed=self.adapter.push_feature_branch(branch=branch,head_sha=manifest['head_sha'],idempotency_key=digest(manifest))
        if pushed.refusal or pushed.unknown or pushed.head_sha!=manifest['head_sha']:raise ValueError('GITHUB_PUSH_UNCONFIRMED')
        guard()
        pr=self.adapter.create_pull_request(branch=branch,base_branch=base,title='DAL '+inputs['owner']['workflow_id'],
            body='Workflow delivery '+digest(manifest),idempotency_key=digest(manifest))
        if pr.refusal or pr.unknown or pr.head_sha!=manifest['head_sha'] or not pr.pull_request_number:raise ValueError('GITHUB_PR_UNCONFIRMED')
        owner,name=repository.split('/')
        result=dict(kind='pr',repository_id=repository,host='github.com',owner=owner,repository=name,
            pr_number=pr.pull_request_number,base_branch=base,base_sha=base_sha)
        self.probe(dict(manifest,**result))
        return result

    def probe(self,manifest):
        repository=self.adapter._settings.repository
        if manifest.get('repository_id')!=repository or type(manifest.get('pr_number')) is not int:raise ValueError('GITHUB_PR_INVALID')
        observed=self.call('GET','/pulls/'+str(manifest['pr_number']))
        base,head=observed.get('base',{}),observed.get('head',{})
        if (observed.get('state')!='open' or observed.get('merged') is not False
            or head.get('sha')!=manifest['head_sha'] or base.get('sha')!=manifest['base_sha']
            or base.get('ref')!=manifest['base_branch'] or head.get('ref')!=manifest['stage_manifest']['workspace']['branch'].removeprefix('refs/heads/')
            or base.get('repo',{}).get('full_name')!=repository or head.get('repo',{}).get('full_name')!=repository):
            raise ValueError('GITHUB_PR_DRIFT')
        return {key:manifest[key] for key in ('kind','repository_id','host','owner','repository','pr_number','base_branch','base_sha')}
