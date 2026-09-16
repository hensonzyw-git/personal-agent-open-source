"""Sealed v2 task sources, results, read evidence, and conservative recovery."""
import json
from uuid import uuid4
from sqlalchemy import select,update,insert,func
from personal_agent.runtime.task_control import TaskControlStore, _now
from personal_agent.runtime.run_store import RunStateError, TASK_LIMITS, USAGE_COLUMNS
from personal_agent.storage.models import Operation, ConversationEvent
from personal_agent.api.operation_store import transition_operation
from personal_agent.api.operation_state import StaleOperationVersionError
from personal_agent.runtime.answers import canonical


def close_candidate(session, run, *, now_ms):
    source=run['candidate_operation_id']
    if source is None: return
    operations=Operation.__table__
    old=session.execute(select(operations).where(operations.c.operation_id==source)).mappings().one()
    if old['state']!='waiting_for_clarification' or old['state_version']!=run['expected_source_version']:
        raise RunStateError('stale_clarification_source')
    transition_operation(session,operation_id=source,current_state=old['state'],current_version=old['state_version'],
        target_state='cancelled_pre_submit',now=_now(now_ms))
    runs=TaskControlStore(None).runs
    session.execute(update(runs).where(runs.c.operation_id==source).values(superseded_by_operation_id=run['operation_id']))


class RunRepository(TaskControlStore):
    def __init__(self, sessions, keyring):
        super().__init__(sessions)
        self.keyring=keyring
        self.outcomes=self.runs.metadata.tables['agent_run_outcomes']

    def seal(self, table, column, identity, value):
        return self.keyring.encrypt(canonical(value).encode(),table=table,column=column,row_id=identity)

    def open(self, table, column, identity, envelope):
        return json.loads(self.keyring.decrypt(envelope,table=table,column=column,row_id=identity))

    def pending(self, timeline, *, offset=0, limit=3):
        if type(offset) is not int or offset<0 or offset>1000: raise RunStateError('invalid_candidate_cursor')
        with self.sessions() as s:
            rows=s.execute(select(self.tasks).where(self.tasks.c.timeline_id==timeline,
                self.tasks.c.status.in_(['active','waiting','paused'])).order_by(select(func.max(self.runs.c.started_ms)).where(self.runs.c.task_id==self.tasks.c.task_id).scalar_subquery().desc(),self.tasks.c.task_id).offset(offset).limit(limit+1)).mappings().all()
            result=[]
            for t in rows[:limit]:
                goal=self.open('agent_tasks','sealed_goal',t['task_id'],t['sealed_goal'])
                constraints=self.open('agent_tasks','sealed_constraints',t['task_id'],t['sealed_constraints'])
                source=s.execute(select(self.runs.c.operation_id,Operation.state_version).join(Operation,Operation.operation_id==self.runs.c.operation_id).where(self.runs.c.task_id==t['task_id'],Operation.state=='waiting_for_clarification',self.runs.c.superseded_by_operation_id.is_(None)).order_by(self.runs.c.started_ms.desc()).limit(1)).first()
                last=None
                if source:
                    outcome=s.execute(select(self.outcomes).where(self.outcomes.c.operation_id==source.operation_id)).mappings().one_or_none()
                    if outcome:last=self.open('agent_run_outcomes','sealed_answer',source.operation_id,outcome['sealed_answer']).get('text')
                result.append({'last_question':last,'source_operation_id':source.operation_id if source else None,'source_version':source.state_version if source else None,'task_ref':t['task_id'],'revision':t['revision'],'status':t['status'],'goal':goal,
                    'constraints':constraints,'write_pending':t['write_slot'] is not None,
                    'metadata':self.open('agent_tasks','source_refs',t['task_id'],t['source_refs']) if t['source_refs'] else None})
            return result, offset+limit if len(rows)>limit else None

    def validate_sources(self, session, timeline, sources):
        if not isinstance(sources,list) or not sources or len(sources)>32 or any(not isinstance(x,str) for x in sources):
            raise RunStateError('invalid_constraint_sources')
        found=session.execute(select(ConversationEvent.event_id).where(ConversationEvent.event_id.in_(sources),
            ConversationEvent.conversation_id==timeline,ConversationEvent.event_type=='user_message')).scalars().all()
        if set(found)!=set(sources): raise RunStateError('unknown_constraint_source')

    def validate_metadata(self, metadata, *, current_source, timeline, old=None):
        from jsonschema import Draft202012Validator
        from personal_agent.runtime.run_catalog import TASK_VALIDATION
        if not Draft202012Validator(TASK_VALIDATION).is_valid(metadata):
            raise RunStateError('invalid_task_metadata')
        goal=metadata.get('goal'); constraints=metadata.get('constraints',[])
        if not isinstance(goal,str) or not goal.strip() or len(goal)>2000 or not isinstance(constraints,list) or len(constraints)>32:
            raise RunStateError('invalid_task_metadata')
        with self.sessions() as s:
            self.validate_sources(s,timeline,metadata.get('source_refs'))
            if metadata.get('query_requirement'):
                self.validate_sources(s, timeline, metadata['query_requirement']['source_refs'])
            for comparison in metadata.get('comparisons',[]):self.validate_sources(s,timeline,comparison['source_refs'])
            for c in constraints:
                if not isinstance(c,dict) or set(c)!={'key','value','source_refs'} or not isinstance(c['key'],str): raise RunStateError('invalid_constraint')
                self.validate_sources(s,timeline,c['source_refs'])
            if len({c['key'] for c in constraints})!=len(constraints): raise RunStateError('duplicate_constraint')
        if old:
            new={c['key']:c for c in constraints}
            for c in old:
                if c['key'] not in new or new[c['key']]['value']!=c['value']:
                    if current_source not in metadata['source_refs']: raise RunStateError('constraint_change_needs_current_source')
        return metadata

    def validate_write_sources(self, metadata, sources, *, current_source, timeline, task_id):
        refs = set(sources) | set(metadata['source_refs'])
        for constraint in metadata['constraints']:
            refs.update(constraint['source_refs'])
        with self.sessions() as s:
            self.validate_sources(s, timeline, list(refs))
            if task_id is None:
                if refs != {current_source}:
                    raise RunStateError('new_write_requires_current_source')
                return
            old = refs - {current_source}
            owned = s.execute(select(ConversationEvent.event_id).join(
                self.runs, self.runs.c.operation_id == ConversationEvent.operation_id).where(
                ConversationEvent.event_id.in_(old), self.runs.c.task_id == task_id)).scalars().all()
            if old != set(owned):
                raise RunStateError('write_source_task_mismatch')

    def record_not_executed(self, lease, calls, *, now_ms):
        def work(s):
            self.check_in_transaction(s, lease, now_ms=now_ms)
            for call in calls:
                step = self._next_step(s, lease.operation_id)
                s.execute(insert(self.steps).values(operation_id=lease.operation_id,
                    step_no=step, call_no=0, attempt_no=1, call_id=call.call_id,
                    attempt_nonce='not_executed:'+str(step)+':'+call.call_id,
                    args_hash=call.args_hash, kind='read', status='not_executed',
                    started_ms=now_ms, ended_ms=now_ms))
        self._write(work)

    def finish(self, lease, answer, *, now_ms, event_writer=None, close_source=True, metadata=None):
        def work(s):
            run=self.check_in_transaction(s,lease,now_ms=now_ms)
            if run['task_id'] is None: raise RunStateError('unbound_finish')
            existing=s.execute(select(self.outcomes).where(self.outcomes.c.operation_id==lease.operation_id)).first()
            if existing: raise RunStateError('outcome_already_committed')
            op=self._one(s,Operation.__table__,Operation.operation_id,lease.operation_id)
            failure_code = answer.get('failure', {}).get('code')
            target = 'failed_safe' if failure_code else ('waiting_for_clarification' if answer['kind']=='clarification' else 'succeeded')
            if op['state']=='accepted':
                v=transition_operation(s,operation_id=op['operation_id'],current_state='accepted',current_version=op['state_version'],target_state='interpreting',now=_now(now_ms))
                op.update(state='interpreting',state_version=v)
            transition_operation(s,operation_id=op['operation_id'],current_state=op['state'],current_version=op['state_version'],target_state=target,now=_now(now_ms),failure_reason=failure_code)
            if close_source:close_candidate(s,run,now_ms=now_ms)
            s.execute(insert(self.outcomes).values(operation_id=lease.operation_id,version=2,
                outcome_kind=answer['kind'],task_status=answer['task_status'],failure_code=failure_code,sealed_answer=self.seal('agent_run_outcomes','sealed_answer',lease.operation_id,answer)))
            s.execute(update(self.runs).where(self.runs.c.operation_id==lease.operation_id).values(state='partial' if failure_code else ('parked' if target.startswith('waiting') else 'completed')))
            s.execute(update(self.tasks).where(self.tasks.c.task_id==run['task_id']).values(status=answer['task_status'],active_operation_id=None))
            if metadata is not None and close_source:
                s.execute(update(self.tasks).where(self.tasks.c.task_id==run['task_id']).values(
                    sealed_constraints=self.seal('agent_tasks','sealed_constraints',run['task_id'],metadata['constraints']),
                    source_refs=self.seal('agent_tasks','source_refs',run['task_id'],metadata)))
            if event_writer: event_writer(s,answer)
        self._write(work)

    def cancel_pre_submit(self, session, operation_id, *, now_ms):
        """Called in the authenticated DELETE transaction after safe cancellation."""
        run=session.execute(select(self.runs).where(self.runs.c.operation_id==operation_id)).mappings().one_or_none()
        if run is None:return
        if run['superseded_by_operation_id'] is not None:return
        op=self._one(session,Operation.__table__,Operation.operation_id,operation_id)
        if op['state']!='cancelled_pre_submit':return
        session.execute(update(self.runs).where(self.runs.c.operation_id==operation_id).values(state='cancelled',fence=run['fence']+1))
        session.execute(update(self.steps).where(self.steps.c.operation_id==operation_id,self.steps.c.status.in_(['proposed','reserved'])).values(status='cancelled',ended_ms=now_ms))
        if run['task_id']:
            task=self._one(session,self.tasks,self.tasks.c.task_id,run['task_id'])
            # A stale clarification card cannot revoke a different active run.
            if task['active_operation_id'] in (None,operation_id):
                session.execute(update(self.tasks).where(self.tasks.c.task_id==run['task_id']).values(status='cancelled',revision=task['revision']+1,active_operation_id=None,write_slot=None))
        else:
            # The actual attempt remains charged to each held candidate.
            for reservation in session.execute(select(self.reservations).where(self.reservations.c.operation_id==operation_id,self.reservations.c.state=='held')).mappings():
                task=self._one(session,self.tasks,self.tasks.c.task_id,reservation['task_id'])
                session.execute(update(self.tasks).where(self.tasks.c.task_id==task['task_id']).values(**{USAGE_COLUMNS[k]:task[USAGE_COLUMNS[k]]+reservation[k] for k in TASK_LIMITS}))
                session.execute(update(self.reservations).where(self.reservations.c.operation_id==operation_id,self.reservations.c.task_id==task['task_id'],self.reservations.c.reservation_no==reservation['reservation_no']).values(state='orphan_charge',**{f'charged_{k}':reservation[k] for k in TASK_LIMITS}))

    def completed_evidence(self, operation_id):
        with self.sessions() as s:
            rows=s.execute(select(self.steps).where(self.steps.c.operation_id==operation_id,self.steps.c.status=='completed',self.steps.c.sealed_evidence.is_not(None))).mappings().all()
            return [self.open('agent_run_steps','sealed_evidence',f'{operation_id}:{r["step_no"]}:0',r['sealed_evidence']) for r in rows if r['kind']!='model']

    def outcome(self, operation_id):
        with self.sessions() as s:
            row=s.execute(select(self.outcomes).where(self.outcomes.c.operation_id==operation_id)).mappings().one_or_none()
            return None if row is None else self.open('agent_run_outcomes','sealed_answer',operation_id,row['sealed_answer'])

    def evidence_step(self, lease, call_id, payload, *, now_ms):
        def work(s):
            self.check_in_transaction(s,lease,now_ms=now_ms)
            r=s.execute(select(self.steps).where(self.steps.c.operation_id==lease.operation_id,self.steps.c.call_id==call_id)).mappings().one()
            identity=f'{lease.operation_id}:{r["step_no"]}:0'
            s.execute(update(self.steps).where(self.steps.c.operation_id==lease.operation_id,self.steps.c.step_no==r['step_no']).values(
                status='completed',ended_ms=now_ms,sealed_evidence=self.seal('agent_run_steps','sealed_evidence',identity,payload)))
        self._write(work)

    def recover(self, operation_id, *, now_ms):
        """Expired reads become resumable tasks; frozen writes never reselect."""
        run=self.snapshot(operation_id)
        if run['state'] in {'completed','partial','cancelled','parked'}:return
        if now_ms<run['deadline_ms']:return
        if run['task_id'] is None:
            return self.recover_prebind(operation_id,now_ms=now_ms)
        def work(s):
            run=self._one(s,self.runs,self.runs.c.operation_id,operation_id)
            if run['state'] in {'completed','failed','cancelled','parked','partial'}: return
            if run['lease_until_ms'] is not None and now_ms<run['lease_until_ms']: return
            task=self._one(s,self.tasks,self.tasks.c.task_id,run['task_id'])
            if task['write_slot'] is not None:
                # Legacy business recovery owns authoritative write results.
                s.execute(update(self.runs).where(self.runs.c.operation_id==operation_id).values(fence=run['fence']+1,state='handoff'))
                return
            elapsed=max(run['active_ms'],min(now_ms,run['deadline_ms'])-run['started_ms'])
            s.execute(update(self.runs).where(self.runs.c.operation_id==operation_id).values(state='failed',fence=run['fence']+1,active_ms=elapsed))
            if task['active_operation_id']==operation_id:
                s.execute(update(self.tasks).where(self.tasks.c.task_id==task['task_id']).values(active_ms=task['active_ms']+elapsed-run['active_ms'],active_operation_id=None,status='waiting'))
        self._write(work)

    def cached_read(self, operation_id, request):
        with self.sessions() as s:
            rows=s.execute(select(self.steps).where(self.steps.c.operation_id==operation_id,self.steps.c.kind=='read',self.steps.c.status=='completed',self.steps.c.sealed_args.is_not(None),self.steps.c.sealed_evidence.is_not(None))).mappings().all()
            for row in rows:
                args=self.open('agent_run_steps','sealed_args',row['call_id'],row['sealed_args'])
                if args.get('tool')==request['tool'] and args.get('arguments')==request['arguments']:
                    evidence=self.open('agent_run_steps','sealed_evidence',f'{operation_id}:{row["step_no"]}:0',row['sealed_evidence'])
                    if 'error' not in evidence:return evidence
        return None

    def reserve_read(self, lease, call_id, request, *, now_ms):
        def work(s):
            prior=s.execute(select(self.steps.c.call_id).where(self.steps.c.operation_id==lease.operation_id,self.steps.c.call_id==call_id)).first()
            if prior:raise RunStateError('read_call_id_reused')
            self.reserve_bound(lease.operation_id,now_ms=now_ms,read_add=1,attempt_key=call_id,lease=lease,_session=s)
            s.execute(update(self.steps).where(self.steps.c.operation_id==lease.operation_id,self.steps.c.call_id==call_id).values(sealed_args=self.seal('agent_run_steps','sealed_args',call_id,request)))
        self._write(work)

    def reserve_web(self, lease, attempt_key, *, now_ms, daily_limit=100, sealed_args=None):
        if type(daily_limit) is not int or not 1<=daily_limit<=100: raise RunStateError('invalid_search_limit')
        days=self.runs.metadata.tables['search_budget_days']
        day=_now(now_ms).date().isoformat()
        def work(s):
            prior=s.execute(select(self.steps).where(self.steps.c.operation_id==lease.operation_id,self.steps.c.call_id==attempt_key)).first()
            if prior: raise RunStateError('web_attempt_replay')
            row=s.execute(select(days).where(days.c.provider=='anysearch',days.c.utc_day==day)).mappings().one_or_none()
            if row is None:s.execute(insert(days).values(provider='anysearch',utc_day=day,reserved_count=1,limit_snapshot=daily_limit))
            else:
                if row['reserved_count']>=min(daily_limit,row['limit_snapshot']):raise RunStateError('search_daily_budget')
                s.execute(update(days).where(days.c.provider=='anysearch',days.c.utc_day==day).values(reserved_count=row['reserved_count']+1))
            self.reserve_bound(lease.operation_id,now_ms=now_ms,read_add=1,web_add=1,attempt_key=attempt_key,lease=lease,_session=s)
            s.execute(update(self.steps).where(self.steps.c.operation_id==lease.operation_id,self.steps.c.call_id==attempt_key).values(sealed_args=sealed_args))
        self._write(work)

    def amend_and_bind(self, lease, *, task_id, expected_revision, control_id, metadata, now_ms):
        goal=self.seal('agent_tasks','sealed_goal',task_id,metadata['goal'])
        constraints=self.seal('agent_tasks','sealed_constraints',task_id,metadata['constraints'])
        def work(s):
            result=self.mutate_task(lease,task_id=task_id,expected_revision=expected_revision,action='amend',control_id=control_id,
                sealed_change=self.seal('agent_run_steps','sealed_args',control_id,metadata),now_ms=now_ms,
                sealed_goal=goal,sealed_constraints=constraints,_session=s)
            if result!='applied':return result
            t=self._one(s,self.tasks,self.tasks.c.task_id,task_id)
            r=self.reservations
            s.execute(update(r).where(r.c.operation_id==lease.operation_id,r.c.task_id==task_id,r.c.state=='held').values(task_revision=t['revision']))
            bound=self.bind(lease.operation_id,task_id=task_id,now_ms=now_ms,lease=lease,sealed_goal=goal,sealed_constraints=constraints,_session=s)
            if not bound.accepted:raise RunStateError('amend_binding_conflict')
            from personal_agent.runtime.task_contracts import freeze_comparisons
            frozen = freeze_comparisons(metadata, None, task_id=task_id, revision=t['revision'])
            run = self._one(s, self.runs, self.runs.c.operation_id, lease.operation_id)
            snapshot = self.open('agent_runs', 'sealed_input_snapshot', lease.operation_id, run['sealed_input_snapshot'])
            snapshot['task_metadata'] = frozen
            s.execute(update(self.runs).where(self.runs.c.operation_id==lease.operation_id).values(
                candidate_operation_id=None,expected_source_version=None,
                sealed_input_snapshot=self.seal('agent_runs','sealed_input_snapshot',lease.operation_id,snapshot)))
            s.execute(update(self.tasks).where(self.tasks.c.task_id==task_id).values(source_refs=self.seal('agent_tasks','source_refs',task_id,frozen)))
            return 'applied'
        try:
            result = self._write(work)
        except (RunStateError, StaleOperationVersionError) as exc:
            # The amendment transaction must roll back the old proposal, but
            # cannot refund the already reserved model attempt on that task.
            if not isinstance(exc, StaleOperationVersionError) and str(exc) not in {'stale_or_foreign_task','terminal_task','amend_binding_conflict'}:
                raise
            self.bind(lease.operation_id,task_id=task_id,now_ms=now_ms,lease=lease,
                      sealed_goal=goal,sealed_constraints=constraints,_reject=True)
            raise RunStateError('task_binding_conflict') from None
        if result == 'too_late':
            with self.sessions() as s:
                held = s.execute(select(self.reservations.c.task_id).where(
                    self.reservations.c.operation_id==lease.operation_id,
                    self.reservations.c.task_id==task_id,self.reservations.c.state=='held')).first()
            if held:
                self.bind(lease.operation_id,task_id=task_id,now_ms=now_ms,lease=lease,
                          sealed_goal=goal,sealed_constraints=constraints,_reject=True)
                raise RunStateError('task_amend_too_late')
        return result

    def settle_expired_or_business(self, operation_id, *, now_ms, failure_code=None):
        """Rebuild factual output after authoritative v1 recovery/phone reports."""
        from personal_agent.api import events
        def work(s):
            run=self._one(s,self.runs,self.runs.c.operation_id,operation_id)
            op=self._one(s,Operation.__table__,Operation.operation_id,operation_id)
            if run['state'] in {'completed','partial','cancelled'}:return
            # A structured duplicate decision transfers the same business task
            # to a legacy recovery-only operation. Its outcome owns the slot.
            if run['superseded_by_operation_id'] is not None and op['duplicate_check_id']:
                op = self._one(s, Operation.__table__, Operation.operation_id, run['superseded_by_operation_id'])
                if op['state'] not in {'succeeded','failed_safe','cancelled_pre_submit','needs_manual_review'}:return
            if op['state'] in {'accepted','interpreting','dispatching'}:
                if now_ms<run['deadline_ms'] and run['state']!='failed':return
                # A dispatching frozen plan is recovery-only, not proof of zero execution.
                if op['plan_key'] is not None:return
                if run['task_id']:
                    task=self._one(s,self.tasks,self.tasks.c.task_id,run['task_id'])
                    if task['active_operation_id']==operation_id and task['write_slot'] is not None:return
                transition_operation(s,operation_id=operation_id,current_state=op['state'],current_version=op['state_version'],target_state='failed_safe',now=_now(now_ms),failure_reason='run_budget_exhausted')
                op['state']='failed_safe'
            if op['state'] not in {'succeeded','failed_safe','cancelled_pre_submit','needs_manual_review'}:return
            if op['plan_key']:
                siblings=s.execute(select(Operation.state).where(Operation.plan_key==op['plan_key'])).scalars().all()
                if any(st not in {'succeeded','failed_safe','cancelled_pre_submit'} for st in siblings):return
            completed=op['state'] in {'succeeded','failed_safe','cancelled_pre_submit'}
            answer={'version':2,'kind':'action' if op['tool'] else 'limitation','task_status':'completed' if completed else 'waiting',
                'text':'操作已完成。' if op['state']=='succeeded' else '操作未完成，已保留可核验的状态。',
                'evidence':[{'kind':'action_card','state':op['state'],'record_id':op['safe_result'] if op['state']=='succeeded' else None}]}
            if not op['tool']:
                answer['coverage']='partial'
                answer['task_status']='waiting'
                for evidence in s.execute(select(self.steps).where(self.steps.c.operation_id==operation_id,self.steps.c.kind=='read',self.steps.c.status=='completed',self.steps.c.sealed_evidence.is_not(None))).mappings():
                    payload=self.open('agent_run_steps','sealed_evidence',f'{operation_id}:{evidence["step_no"]}:0',evidence['sealed_evidence'])
                    if payload.get('kind')=='query_card':answer['evidence'].append(payload)
                    answer['evidence'].extend(payload.get('sources',[]))
            if failure_code is not None:
                answer['failure'] = {'code':failure_code,'retryable':False,'stage':'runtime'}
            existing=s.execute(select(self.outcomes).where(self.outcomes.c.operation_id==operation_id)).mappings().one_or_none()
            if existing and self.open('agent_run_outcomes','sealed_answer',operation_id,existing['sealed_answer'])==answer:return
            sealed=self.seal('agent_run_outcomes','sealed_answer',operation_id,answer)
            if existing:s.execute(update(self.outcomes).where(self.outcomes.c.operation_id==operation_id).values(sealed_answer=sealed,task_status=answer['task_status']))
            else:s.execute(insert(self.outcomes).values(operation_id=operation_id,version=2,outcome_kind=answer['kind'],task_status=answer['task_status'],sealed_answer=sealed))
            s.execute(update(self.runs).where(self.runs.c.operation_id==operation_id).values(state='completed' if completed else 'parked',fence=run['fence']+1))
            if run['task_id']:
                t=self._one(s,self.tasks,self.tasks.c.task_id,run['task_id'])
                if t['active_operation_id']==operation_id or (t['active_operation_id'] is None and
                    run['state']=='parked' and t['revision']==run['expected_task_revision'] and t['status'] in {'active','waiting','paused'}):
                    elapsed=run['active_ms'] if run['state']=='parked' or t['active_operation_id'] is None else max(run['active_ms'],min(now_ms,run['deadline_ms'])-run['started_ms'])
                    s.execute(update(self.tasks).where(self.tasks.c.task_id==run['task_id']).values(status=answer['task_status'],
                        active_operation_id=operation_id if not completed and t['write_slot'] is not None else None,
                        active_ms=t['active_ms']+elapsed-run['active_ms'],write_slot=None if completed else t['write_slot']))
            anchor=s.execute(select(ConversationEvent.__table__).where(ConversationEvent.operation_id==operation_id,ConversationEvent.event_type=='user_message')).mappings().first()
            if anchor:
                from personal_agent.api.app import _operation_event_content
                operation = s.get(Operation, op['operation_id']); s.refresh(operation)
                content = _operation_event_content(self.keyring, operation)
                content['result_envelope'] = answer
                events.append_event(s,self.keyring,conversation_id=anchor['conversation_id'],session_id=anchor['session_id'],turn_id=anchor['turn_id'],
                    event_type=events.OPERATION_RESULT,operation_id=operation_id,content=content,now=_now(now_ms))
        self._write(work)

    def sweep(self, *, now_ms):
        cursor = None
        while True:
            with self.sessions() as s:
                from sqlalchemy import or_, and_, exists
                op = Operation.__table__
                terminal = ['succeeded','failed_safe','cancelled_pre_submit']
                settled = exists(select(op.c.operation_id).where(
                    op.c.operation_id == self.runs.c.operation_id,
                    op.c.state.in_(terminal)))
                handed_back = exists(select(op.c.operation_id).where(
                    op.c.operation_id == self.runs.c.operation_id,
                    op.c.state.in_(terminal + ['needs_manual_review'])))
                child_done = exists(select(op.c.operation_id).where(
                    op.c.operation_id == self.runs.c.superseded_by_operation_id,
                    op.c.state.in_(terminal)))
                child_handed_back = exists(select(op.c.operation_id).where(
                    op.c.operation_id == self.runs.c.superseded_by_operation_id,
                    op.c.state.in_(terminal + ['needs_manual_review'])))
                query = select(self.runs.c.started_ms, self.runs.c.operation_id).where(
                    self.runs.c.started_ms <= now_ms,
                    or_(self.runs.c.state.in_(['accepted','thinking','reading','finalizing','failed']),
                        and_(self.runs.c.state == 'parked', or_(settled, child_done)),
                        and_(self.runs.c.state == 'handoff', or_(handed_back, child_handed_back))))
                if cursor is not None:
                    from sqlalchemy import tuple_
                    query = query.where(tuple_(self.runs.c.started_ms, self.runs.c.operation_id) > cursor)
                rows = s.execute(query.order_by(self.runs.c.started_ms, self.runs.c.operation_id).limit(100)).all()
            if not rows: return
            for _, op in rows:
                self.recover(op,now_ms=now_ms)
                self.settle_expired_or_business(op,now_ms=now_ms)
            cursor = tuple(rows[-1])
