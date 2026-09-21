"""Pinned deterministic repository actions for workflow-owned reservations.

No provider decides commands, paths, commit metadata, tests, or write grants.
A process interruption is reconciled by the caller, never by retrying this class.
"""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from personal_agent_dal.worker.supervisor import verify_executable,SupervisorRefusal,_absolute
from personal_agent_dal.timeline.requests import digest
from personal_agent_dal.worker.toolchain import _sandboxed_argv, _child_environment
from personal_agent_dal.worker.workflow_process import run_owned,validate_executor


class RepositoryExecutor:
    def __init__(self,supervisor,reservation,config,inputs,*,inventory,attempt,heartbeat):
        self.supervisor,self.reservation,self.config,self.inputs=supervisor,reservation,config,inputs
        self.inventory,self.attempt,self.heartbeat=inventory,attempt,heartbeat
        validate_executor(config,supervisor)
        from personal_agent_dal.worker.project_policy import validate_project_policy
        validate_project_policy(config,inputs)
        self.git=str(verify_executable(config['git_pin']))
        self.work=Path(reservation['workspace'])
        self.environment={'PATH':str(Path(self.git).parent)+':/usr/bin:/bin','HOME':reservation['temp'],
            'TMPDIR':reservation['temp'],'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null',
            'GIT_TERMINAL_PROMPT':'0','GIT_NO_REPLACE_OBJECTS':'1',
            'GIT_AUTHOR_NAME':'DAL','GIT_AUTHOR_EMAIL':'dal@localhost',
            'GIT_COMMITTER_NAME':'DAL','GIT_COMMITTER_EMAIL':'dal@localhost',
            'GIT_AUTHOR_DATE':str(inputs['prepared_at'])+' +0000','GIT_COMMITTER_DATE':str(inputs['prepared_at'])+' +0000'}

    def run(self,args,*,cwd=None,data=None,raw=False):
        self.supervisor.validate(self.reservation['reservation_id'])
        gitdir=Path(self.reservation['git'])/'repository'
        from personal_agent_dal.worker.project_policy import validate_project_policy
        validate_project_policy(self.config,self.inputs)
        target=Path(cwd) if cwd is not None else self.work
        prefix=[]
        if target==self.work and gitdir.is_dir():
            pointer=self.work/'.git'
            if pointer.is_symlink() or not pointer.is_file() or pointer.read_text().strip()!='gitdir: '+str(gitdir):
                raise SupervisorRefusal('GIT_DIRECTORY_SUBSTITUTED')
            if (gitdir/'objects/info/alternates').exists():raise SupervisorRefusal('SHARED_GIT_OBJECTS')
            prefix=['--git-dir='+str(gitdir),'--work-tree='+str(self.work)]
        output,code=run_owned([self.git,*prefix,'-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
            '-c','protocol.allow=never','-c','diff.external=',*args],cwd=cwd or self.work,environment=self.environment,
            data=data or b'',timeout=45,inventory=self.inventory,attempt=self.attempt,heartbeat=self.heartbeat,
            revalidate=lambda:validate_executor(self.config,self.supervisor))
        if code or len(output)>2*1024*1024:raise SupervisorRefusal('WORKFLOW_GIT_FAILED')
        return output if raw else output.decode('utf-8','strict').strip()

    def grant(self,actions):
        authorization=self.inputs.get('authorization')
        if not authorization or not set(actions)<=set(authorization['actions']):raise SupervisorRefusal('PROJECT_AUTHORIZATION_REQUIRED')
        if authorization['project_id']!=self.inputs['project']['project_id']:raise SupervisorRefusal('PROJECT_BINDING_MISMATCH')
        return authorization

    def registration(self):
        grant=self.grant({'read'})
        if grant['registration_policy']=='github_issue':
            self.grant({'read','remote_issue'})
            receipt=self.publish({'operation':'issue'})
            return dict(kind='registration',text='登记已完成并回读。',project_id=grant['project_id'],grant_digest=self.inputs['project']['grant_digest'],policy='github_issue',tracker_receipt=digest(receipt))
        # The encrypted workflow inventory is the durable equivalent tracker;
        # the receipt names only immutable refs, never the request plaintext.
        receipt=dict(workflow_id=self.inputs['owner']['workflow_id'],project_id=grant['project_id'],
            request_revision=self.inputs['request_revision'],grant_digest=self.inputs['project']['grant_digest'])
        return dict(kind='registration',text='项目已登记到本地开发追踪。',project_id=grant['project_id'],
            grant_digest=self.inputs['project']['grant_digest'],policy='local_tracker',tracker_receipt=digest(receipt))

    def prepare(self):
        grant=self.grant({'read'})
        registered=self.config['projects'].get(grant['project_id'])
        if registered is None or registered['root']!=grant['root'] or registered['kind']!=grant['kind']:
            raise SupervisorRefusal('PROJECT_NOT_REGISTERED_ON_WORKER')
        root=_absolute(grant['root'])
        if grant['kind']=='existing':
            if any(self.work.iterdir()) or any(Path(self.reservation['git']).iterdir()):raise SupervisorRefusal('WORKSPACE_PREPARATION_UNKNOWN')
            policy=self.inputs.get('project_policy')
            base=policy['base_sha'] if policy else self.run(['rev-parse','HEAD'],cwd=root)
            if policy and self.run(['rev-parse','--verify',base+'^{commit}'],cwd=root)!=base:raise SupervisorRefusal('PROJECT_BASE_CHANGED')
            self.run(['-c','protocol.file.allow=always','clone','--no-local','--no-checkout','--separate-git-dir',
                str(Path(self.reservation['git'])/'repository'),'--',str(root),str(self.work)])
            self.run(['remote','remove','origin'])
            self.run(['checkout','--detach',base])
        else:
            self.grant({'read','create','local_init'})
            if root!=self.supervisor.root:raise SupervisorRefusal('LOCAL_ROOT_NOT_AUTHORIZED')
            if any(self.work.iterdir()) or any(Path(self.reservation['git']).iterdir()):raise SupervisorRefusal('WORKSPACE_PREPARATION_UNKNOWN')
            self.run(['init','--separate-git-dir',str(Path(self.reservation['git'])/'repository'),str(self.work)])
            tree=self.run(['mktree'],data=b'')
            policy=self.inputs.get('project_policy')
            original_dates={k:self.environment[k] for k in ('GIT_AUTHOR_DATE','GIT_COMMITTER_DATE')}
            if policy:self.environment.update({k:'946684800 +0000' for k in original_dates})
            try:base=self.run(['commit-tree',tree],data=b'DAL local project bootstrap\n')
            finally:self.environment.update(original_dates)
            if policy and base!=policy['base_sha']:raise SupervisorRefusal('PROJECT_BASE_CHANGED')
            self.run(['update-ref','HEAD',base,'0'*40])
        branch='refs/heads/codex/dal-'+self.inputs['owner']['workflow_id']
        self.run(['update-ref',branch,base,'0'*40])
        self.run(['symbolic-ref','HEAD',branch])
        manifest=dict(kind=grant['kind'],project_id=grant['project_id'],reservation_id=self.reservation['reservation_id'],
            generation=self.reservation['generation'],directory_digest=digest(self.reservation['identities']),base_sha=base,
            toolchain_digest=digest(registered['verification_commands']),branch=branch)
        return dict(kind='workspace',text='隔离工作区已准备并回读。',project_id=grant['project_id'],
            grant_digest=self.inputs['project']['grant_digest'],manifest=manifest)

    def scan_candidate(self):
        """Inspect the exact staged blobs, before verification or publication."""
        import re
        # Definite credential shapes only; generic variable names in source are
        # not credentials. Sensitive files fail regardless of their contents.
        secret=re.compile(r'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]+|AKIA[A-Z0-9]{16})\b')
        paths=self.run(['diff','--cached','--name-only','--diff-filter=ACMR','-z','HEAD','--']).split('\0')
        total=0
        for name in filter(None,paths):
            path=Path(name)
            if (path.name=='.env' or path.name.startswith('.env.') and path.name not in ('.env.example','.env.template')
                or path.suffix.lower() in ('.p12','.pfx','.key','.sqlite','.sqlite3','.db')
                or path.name in ('id_rsa','id_ed25519','id_ecdsa')):
                raise SupervisorRefusal('CANDIDATE_SENSITIVE_FILE')
            size=int(self.run(['cat-file','-s',':'+name]));total+=size
            if size>1024*1024 or total>8*1024*1024:raise SupervisorRefusal('CANDIDATE_SCAN_LIMIT')
            try:content=self.run(['cat-file','blob',':'+name])
            except UnicodeError:raise SupervisorRefusal('CANDIDATE_BINARY_REQUIRES_REVIEW') from None
            if secret.search(content):raise SupervisorRefusal('CANDIDATE_SECRET')

    def candidate(self,*,stage_text):
        self.grant({'read','write'})
        head=self.run(['rev-parse','HEAD'])
        expected=self.inputs['stage']['candidate']['head_sha']
        if expected and head!=expected:raise SupervisorRefusal('CANDIDATE_HEAD_CHANGED')
        self.run(['add','--all','--','.'])
        self.scan_candidate()
        tree=self.run(['write-tree'])
        return dict(kind='candidate',text=stage_text,candidate=dict(base_sha=head,head_sha=head,tree_sha=tree))

    def observe_candidate(self):
        candidate=self.inputs['stage']['candidate']
        if self.run(['rev-parse','HEAD'])!=candidate['head_sha'] or self.run(['write-tree'])!=candidate['tree_sha']:
            raise SupervisorRefusal('CANDIDATE_CHANGED')
        # Index and worktree must match; otherwise write-tree alone misses edits.
        if self.run(['diff','--no-ext-diff','--name-only']) or self.run(['ls-files','--others','--exclude-standard']):
            raise SupervisorRefusal('CANDIDATE_CHANGED')
        return candidate

    def verify(self,*,heartbeat):
        self.grant({'read'})
        candidate=self.observe_candidate()
        project=self.config['projects'][self.inputs['project']['project_id']]
        commands=project['verification_commands']
        if digest(commands)!=self.inputs['workspace']['toolchain_digest'] or not commands:raise SupervisorRefusal('TOOLCHAIN_CHANGED')
        scratch=Path(self.reservation['temp'])/('verify-'+str(self.inputs['stage']['state_version']))
        if scratch.exists():raise SupervisorRefusal('VERIFICATION_RECONCILIATION_REQUIRED')
        self.supervisor.validate(self.reservation['reservation_id'])
        shutil.copytree(self.work,scratch,ignore=shutil.ignore_patterns('.git'),symlinks=False)
        results=[]
        for spec in commands:
            if not heartbeat():raise SupervisorRefusal('AUTHORITY_LOST')
            executable=str(verify_executable(spec['pin']))
            argv=(executable,*spec['arguments'])
            sandbox=str(verify_executable(self.config['sandbox_pin']))
            wrapped=_sandboxed_argv(argv,scratch,Path(self.reservation['temp']),(),(self.work,Path(self.reservation['git'])))
            if wrapped[0]!=sandbox:raise SupervisorRefusal('SANDBOX_PIN_MISMATCH')
            output,code=run_owned(wrapped,cwd=scratch,environment=_child_environment(Path(self.reservation['temp'])),
                timeout=spec['timeout_seconds'],inventory=self.inventory,attempt=self.attempt,heartbeat=heartbeat,
                revalidate=lambda:validate_executor(self.config,self.supervisor))
            output=output.decode('utf-8','replace')
            results.append(dict(argv_digest=digest(list(argv)),exit_code=code,output_digest=hashlib.sha256(output.encode()).hexdigest()))
        self.observe_candidate()
        return dict(kind='verification',text='固定验证命令已执行。',candidate=candidate,passed=all(r['exit_code']==0 for r in results),commands=results)

    def commit(self):
        self.grant({'read','write'})
        self.scan_candidate()
        candidate=self.observe_candidate()
        message=('DAL stage '+self.inputs['stage']['stage_id']+'/'+str(self.inputs['stage']['revision'])+'\n').encode()
        sha=self.run(['commit-tree',candidate['tree_sha'],'-p',candidate['head_sha']],data=message)
        self.run(['update-ref',self.inputs['workspace']['branch'],sha,candidate['head_sha']])
        if self.run(['rev-parse','HEAD'])!=sha or self.run(['rev-parse','HEAD^{tree}'])!=candidate['tree_sha']:
            raise SupervisorRefusal('COMMIT_READBACK_MISMATCH')
        return dict(kind='commit',text='阶段提交已完成并回读。',candidate=candidate,committed=True,commit_sha=sha,parent_sha=candidate['head_sha'])

    def delivery(self):
        self.grant({'read'})
        stage_manifest=self.inputs['delivery']
        head=self.run(['rev-parse','HEAD']);tree=self.run(['rev-parse','HEAD^{tree}'])
        if self.run(['status','--porcelain=v1']) or self.run(['ls-files','--others']):
            raise SupervisorRefusal('DELIVERY_WORKTREE_DIRTY')
        commits=[]
        expected=stage_manifest.get('commit_history') or [dict(sha=stage['head_sha'],parent=stage['base_sha'],tree=stage['tree_sha']) for stage in stage_manifest['stages']]
        for commit in expected:
            actual=self.run(['show','-s','--format=%H %P %T',commit['sha']]).split()
            if actual!=[commit['sha'],commit['parent'],commit['tree']]:raise SupervisorRefusal('STAGE_COMMIT_READBACK_MISMATCH')
            commits.append(dict(sha=actual[0],parent=actual[1],tree=actual[2]))
        ordered=self.run(['rev-list','--reverse',stage_manifest['workspace']['base_sha']+'..HEAD']).splitlines()
        if ordered!=[c['sha'] for c in commits] or head!=commits[-1]['sha'] or tree!=commits[-1]['tree']:
            raise SupervisorRefusal('DELIVERY_COMMIT_MANIFEST_MISMATCH')
        manifest=dict(kind='local',stage_manifest=stage_manifest,head_sha=head,tree_sha=tree,commits=commits,clean=True,untracked_digest=digest([]))
        if stage_manifest['workspace']['kind']=='existing':
            if self.inputs['phase'] in ('delivery_probe','delivery_prepare'):
                original=self.inputs['probe']['manifest'] if self.inputs['phase']=='delivery_probe' else self.inputs['published_manifest']
                if any(original.get(key)!=value for key,value in manifest.items() if key!='kind'):
                    raise SupervisorRefusal('DELIVERY_BINDING_MISMATCH')
                remote=self.publish({'operation':'probe','manifest':original})
                manifest.update(remote)
            else:
                if self.inputs['phase']!='delivery_publication':raise SupervisorRefusal('PUBLICATION_PHASE_REQUIRED')
                self.grant({'read','push','pr'})
                import base64
                shas=self.run(['rev-list','--objects','--no-object-names',stage_manifest['workspace']['base_sha']+'..HEAD']).splitlines()
                if len(shas)>512:raise SupervisorRefusal('GIT_BUNDLE_LIMIT')
                objects=[];size=0
                for sha in shas:
                    kind=self.run(['cat-file','-t',sha]);data=self.run(['cat-file',kind,sha],raw=True);size+=len(data)
                    if size>1024*1024:raise SupervisorRefusal('GIT_BUNDLE_LIMIT')
                    objects.append(dict(sha=sha,kind=kind,data=base64.b64encode(data).decode()))
                from personal_agent_dal.github.workflow_objects import decode_bundle
                decode_bundle(objects,commits)
                remote=self.publish({'operation':'publish','manifest':manifest,'objects':objects})
                manifest.update(remote)
        text=('PR 交付已发布并回读，阶段与验证证据绑定当前提交；尚未合并或部署。' if manifest['kind']=='pr'
            else '本地交付已准备，阶段与验证证据绑定当前提交；尚未推送、合并或部署。')
        return dict(kind='delivery',text=text,manifest=manifest)

    def probe(self):
        import time
        expected=self.inputs['probe']
        observed=self.delivery()['manifest']
        return dict(kind='delivery_probe',text='当前交付版本已重新回读。',nonce=expected['nonce'],
            manifest_digest=expected['manifest_digest'],matches=(digest(observed)==expected['manifest_digest']),observed_at=int(time.time()))
