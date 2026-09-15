"""Version choice in the chat anchor; persisted versions win over switches."""
from sqlalchemy import select,insert,inspect
from personal_agent_core.crypto import CryptoError
from personal_agent.storage.models import Base,Operation
from personal_agent.runtime.run_repository import RunRepository
from personal_agent.runtime.run_store import RunStateError
from personal_agent_core.errors import AppError,ErrorCode


def unavailable(code):
    return AppError({'client_upgrade_required':ErrorCode.CLIENT_UPGRADE_REQUIRED,'runtime_unavailable':ErrorCode.RUNTIME_UNAVAILABLE}.get(code,ErrorCode.INVALID_ARGUMENT),internal_detail=code)


def row(session,operation_id):
    t=Base.metadata.tables['agent_runs']
    return session.execute(select(t).where(t.c.operation_id==operation_id)).mappings().one_or_none()


def choose(session,deps,auth,source_id,timeline):
    if source_id:
        from personal_agent.api.app import _owned_operation
        from personal_agent.api.request_payload import open_chat_request
        source=_owned_operation(session,source_id,device_id=auth.device_id)
        payload=open_chat_request(deps.keyring,request_id=source.request_id,envelope=source.api_request.encrypted_request_payload)
        if source.state!='waiting_for_clarification' or payload.conversation_id!=timeline:
            raise unavailable('invalid_clarification_candidate')
        original=row(session,source_id)
        if original:
            if auth.client_wire_version<4:raise unavailable('client_upgrade_required')
            if not deps.v2_execution_enabled:raise unavailable('runtime_unavailable')
            return 2,source.state_version
        return 1,None
    return (2,None) if auth.client_wire_version>=4 and auth.device_id in deps.v2_device_ids and deps.v2_execution_enabled else (1,None)


def anchor(session,deps,operation,payload,source_version):
    r=RunRepository(None,deps.keyring); now=round(deps.now().timestamp()*1000)
    session.execute(insert(r.runs).values(operation_id=operation.operation_id,timeline_id=payload.conversation_id,
        started_ms=now,deadline_ms=now+60000,sealed_input_snapshot=r.seal('agent_runs','sealed_input_snapshot',operation.operation_id,
        {'text':payload.text,'clarification_of':payload.clarification_of}),candidate_operation_id=payload.clarification_of,
        expected_source_version=source_version))


def unavailable_result():
    return {'version': 2, 'kind': 'limitation', 'task_status': 'partial',
            'text': '结果暂时无法读取，请稍后重试。', 'evidence': []}


def valid_result(answer):
    """Validate persisted projection shape before it reaches API consumers.

    This is not model-output admission; evidence was checked when sealed.
    Unknown optional extension fields remain compatible with newer writers.
    """
    if not isinstance(answer, dict): return False
    if type(answer.get('version')) is not int or answer['version'] != 2: return False
    if not isinstance(answer.get('kind'), str) or answer['kind'] not in {'conversation','query','analysis','action','clarification','limitation'}: return False
    if not isinstance(answer.get('task_status'), str) or answer['task_status'] not in {'completed','waiting','partial','cancelled'}: return False
    if not isinstance(answer.get('text'), str): return False
    if not isinstance(answer.get('evidence'), list): return False
    if any(not isinstance(e, dict) or not isinstance(e.get('kind'), str) for e in answer['evidence']): return False
    for field in ('coverage', 'commentary'):
        if answer.get(field) is not None and not isinstance(answer[field], str): return False
    nodes = answer.get('analysis_nodes')
    if nodes is not None:
        if not isinstance(nodes, list) or len(nodes) > 16: return False
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get('kind'), str) or not isinstance(node.get('text'), str): return False
    return True


def project(session,keyring,operation,client_wire_version):
    run = row(session,operation.operation_id)
    if run is None:
        if operation.plan_key:
            parent=session.execute(select(Operation.operation_id).where(Operation.idempotency_key==operation.plan_key)).scalar_one_or_none()
            if parent and row(session,parent) is not None and client_wire_version<4:raise unavailable('client_upgrade_required')
        return None
    if client_wire_version<4:raise unavailable('client_upgrade_required')
    r=RunRepository(None,keyring)
    try:
        out=session.execute(select(r.outcomes).where(r.outcomes.c.operation_id==operation.operation_id)).mappings().one_or_none()
        if out is None:return None
        answer=r.open('agent_run_outcomes','sealed_answer',operation.operation_id,out['sealed_answer'])
    except (CryptoError, ValueError, TypeError, UnicodeError, RecursionError):
        return unavailable_result()
    if not valid_result(answer): return unavailable_result()
    # Live operation truth wins over a pre-receipt action envelope.
    if answer['kind']=='action':
        if operation.duplicate_check_id and run['superseded_by_operation_id']:
            operation = session.execute(select(Operation).where(
                Operation.operation_id == run['superseded_by_operation_id'])).scalar_one()
        answer['task_status']='completed' if operation.state in {'succeeded','failed_safe','cancelled_pre_submit'} else 'waiting'
        answer['text']='操作已完成。' if operation.state=='succeeded' else '操作尚未完成。'
        if operation.plan_key:
            states=session.execute(select(Operation.state).where(Operation.plan_key==operation.plan_key)).scalars().all()
            if any(state not in {'succeeded','failed_safe','cancelled_pre_submit'} for state in states):
                answer['task_status']='waiting';answer['text']='该批日程仍有项目等待可信回执。'
        answer['evidence']=[{'kind':'action_card','state':operation.state,'record_id':operation.safe_result if operation.state=='succeeded' else None}]
    return compatible_answer(answer, client_wire_version)


def process(deps,auth,operation_id):
    import asyncio
    from personal_agent.api.app import (_owned_operation,_anchor_event,_context_factory,_operation_response,_ProcessedChat)
    from personal_agent.api.request_payload import open_chat_request,seal_chat_request,with_clarification_question
    from personal_agent.api import events
    from personal_agent.runtime.host import DurableRunHost
    from personal_agent.runtime.adk_runtime import AdkRuntime
    from personal_agent.runtime.response_witness import ResponseViolation
    with deps.session_factory() as s:
        op=_owned_operation(s,operation_id,device_id=auth.device_id)
        if auth.client_wire_version<4:raise unavailable('client_upgrade_required')
        if not deps.v2_execution_enabled:raise unavailable('runtime_unavailable')
        payload=open_chat_request(deps.keyring,request_id=op.request_id,envelope=op.api_request.encrypted_request_payload)
        anchor_event=_anchor_event(s,operation_id)
        context=_context_factory(deps,auth,s,payload=payload,anchor=anchor_event,operation_id=operation_id)
        def build():
            envelope=context();s.commit();return envelope
        def write_event(db,answer):
            operation=db.get(Operation,operation_id)
            db.refresh(operation)
            if answer['kind']=='clarification':
                operation.api_request.encrypted_request_payload=seal_chat_request(deps.keyring,request_id=operation.request_id,
                    payload=with_clarification_question(payload,answer['text']))
            from personal_agent.api.app import _operation_event_content
            content = _operation_event_content(deps.keyring, operation)
            content['result_envelope'] = answer
            events.append_event(db,deps.keyring,conversation_id=payload.conversation_id,session_id=anchor_event.session_id,
                turn_id=anchor_event.turn_id,event_type=events.OPERATION_RESULT,content=content,operation_id=operation_id,now=deps.now())
        s.commit()
        repository=RunRepository(deps.session_factory,deps.keyring)
        try:
            host=DurableRunHost(deps=deps,auth=auth,operation_id=operation_id,payload=payload,anchor=anchor_event,
                build_context=build,event_writer=write_event,model_factory=deps.v2_model_factory)
        except RunStateError:
            repository.recover(operation_id,now_ms=round(deps.now().timestamp()*1000))
            repository.settle_expired_or_business(operation_id,now_ms=round(deps.now().timestamp()*1000))
            s.expire_all();op=_owned_operation(s,operation_id,device_id=auth.device_id)
            return _ProcessedChat(_operation_response(deps.keyring,op,client_wire_version=auth.client_wire_version))
        for attempt in range(2):
            try:
                asyncio.run(AdkRuntime(host=host,specs=host.specs,max_reads=3-host.repo.snapshot(operation_id)['read_used']).run())
                break
            except ResponseViolation as exc:
                if str(exc)=='finish_required' and attempt==0 and host.format_error is not None:continue
                break # Host records bounded failure or preserves write recovery.
        s.expire_all();op=_owned_operation(s,operation_id,device_id=auth.device_id)
        return _ProcessedChat(_operation_response(deps.keyring,op,client_wire_version=auth.client_wire_version))


def recovery_needed(deps):
    if not deps.v2_execution_enabled: return False
    with deps.session_factory() as s:
        if not inspect(s.get_bind()).has_table('agent_runs'): return False
        if deps.v2_device_ids: return True
        runs = Base.metadata.tables['agent_runs']
        # Removing admission grants must not abandon previously accepted work.
        return s.execute(select(runs.c.operation_id).where(
            runs.c.state.not_in(['completed','partial','cancelled'])).limit(1)).first() is not None


def resumable(deps):
    """Startup/periodic read-only discovery; acquiring the lease is the CAS."""
    from personal_agent.storage.models import Device
    from personal_agent.api.app import AuthContext
    if not deps.v2_execution_enabled:return []
    now=round(deps.now().timestamp()*1000)
    r=RunRepository(deps.session_factory,deps.keyring)
    r.sweep(now_ms=now)
    with deps.session_factory() as s:
        rows=s.execute(select(r.runs).where(r.runs.c.state.in_(['accepted','thinking','reading','finalizing']),
            r.runs.c.deadline_ms>now,(r.runs.c.lease_until_ms.is_(None)) | (r.runs.c.lease_until_ms<=now)).limit(100)).mappings().all()
        result=[]
        for run in rows:
            if run['task_id']:
                task=r._one(s,r.tasks,r.tasks.c.task_id,run['task_id'])
                if task['write_slot'] is not None:continue
            op=s.get(Operation,run['operation_id'])
            if op.cancel_requested or op.state not in {'accepted','interpreting'}:continue
            device=s.get(Device,op.api_request.device_id)
            if device is None or device.status!='active':continue
            import json
            scopes=json.loads(device.scopes) if isinstance(device.scopes,str) else device.scopes
            result.append((run['operation_id'],AuthContext(device.device_id,tuple(scopes),device.allowed_tools_version,4)))
        return result


def compatible_answer(answer, client_wire_version):
    """Read-only v4 history downgrade, including partial failed-run evidence."""
    if client_wire_version >= 5 or not isinstance(answer, dict):
        return answer
    import copy
    from personal_agent.api.finance_query_projection import decode_finance_query_projection, summarise_query_projection
    result = copy.deepcopy(answer)
    cards = [e for e in result.get('evidence', []) if e.get('query_result', {}).get('coverage') is not None]
    if not cards:
        return result
    try:
        texts = [summarise_query_projection(decode_finance_query_projection(e['query_result'])) for e in cards]
    except (ValueError, TypeError, KeyError):
        return unavailable_result()
    result['evidence'] = [e for e in result.get('evidence', []) if e not in cards]
    text = result.get('text', '') + '\n' + '\n'.join(texts)
    if len(text) > 8000:
        result.update(kind='limitation', coverage='partial', task_status='partial', text='请升级 App 查看完整旅行场次汇总。')
    else:
        result['text'] = text
    return result


def compatible_event(content, client_wire_version):
    if not isinstance(content, dict) or 'result_envelope' not in content:
        return content
    return {**content, 'result_envelope': compatible_answer(content['result_envelope'], client_wire_version)}
