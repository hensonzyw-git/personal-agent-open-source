"""Whole-utterance recovery with an explicit task ID and fresh version binding."""
import re
import httpx
from uuid import uuid4


def parse_recovery(text):
    if not isinstance(text,str) or len(text.encode())>32768:return None
    match=re.fullmatch(r'\s*(补充需求|继续开发|暂停开发|取消开发|刷新待审)\s+([A-Za-z0-9][A-Za-z0-9_.-]{0,127})(?:[：:]\s*(\S[\s\S]*))?\s*',text)
    if not match:return None
    action={'补充需求':'clarification','继续开发':'resume','暂停开发':'pause','取消开发':'cancel','刷新待审':'refresh'}[match[1]]
    if (action=='clarification')!=(match[3] is not None):return None
    return dict(workflow_id=match[2],action=action,text=match[3] or match[1])


async def handle_recovery(owner):
    parsed=parse_recovery(owner.payload.text)
    bridge=owner.deps.dal_timeline
    if parsed is None or bridge is None:return False
    selected=None
    if 'dal.request' not in owner.auth.scopes or 'dal.read' not in owner.auth.scopes or owner.auth.client_wire_version<6:
        response='当前设备不能处理这项开发操作，请检查权限和 App 版本。'
    else:
        try:
            detail=bridge.query(owner.auth,operation='request_detail',body={'request_id':parsed['workflow_id']})
            selected=dict(parsed,expected_version=detail['version'])
            response='操作已记录，等待 DAL 校验当前任务版本；未确认的执行结果会先对账。'
        except (ValueError,OSError,httpx.RequestError):response='暂时无法核对这个开发任务，请确认任务 ID 后重试。'
    if owner.repo.snapshot(owner.operation_id)['task_id'] is None:
        ident='task_'+uuid4().hex
        owner.repo.bind(owner.operation_id,task_id=None,new_task_id=ident,now_ms=owner.now(),lease=owner.lease,
            sealed_goal=owner.repo.seal('agent_tasks','sealed_goal',ident,owner.payload.text),
            sealed_constraints=owner.repo.seal('agent_tasks','sealed_constraints',ident,[]))
    answer=dict(version=2,kind='conversation' if selected else 'clarification',task_status='completed' if selected else 'waiting',
        coverage='complete' if selected else 'partial',text=response,evidence=[])
    def writer(session,result):
        if selected:bridge.queue_recovery(owner.auth,command_id=owner.operation_id,source_message_ref=owner.anchor.event_id,payload=selected,_session=session)
        if owner.event_writer:owner.event_writer(session,result)
    owner.repo.finish(owner.lease,answer,now_ms=owner.now(),event_writer=writer)
    return True


async def answer_clarification(owner, args):
    """Resolve from a complete live set; the model supplies neither ID nor text."""
    import asyncio
    from personal_agent.runtime.run_store import RunStateError
    from personal_agent.runtime.run_tools import ToolResult
    if args.get('write_source_refs',args['task']['source_refs']) != [owner.anchor.event_id]:
        raise RunStateError('dal_current_source_required')
    bridge=owner.deps.dal_timeline
    selected=None
    try:
        progress=await asyncio.to_thread(bridge.progress,owner.auth)
        items=progress['items']
        if not progress['complete']:
            response='开发任务列表尚未完整读取，本次未修改任何需求，请稍后重试。'
        elif len(items)!=1:
            response='请明确要补充的开发任务，本次未修改或新建需求。'
            if items:
                response+='\n'+ '\n'.join(f"• {item['summary']}" for item in items)
                response+='\n请打开手机的“开发”列表，点选对应任务，再点“补充需求”。'
        else:
            item=items[0]
            detail=await asyncio.to_thread(bridge.query,owner.auth,operation='request_detail',body={'request_id':item['task_id']})
            if detail['phase']!='clarify' or detail['status'] not in ('active','blocked'):
                response='该任务已不在需求澄清阶段，本次未修改需求。请针对当前待审文档提出修改意见。'
            else:
                selected=dict(workflow_id=item['task_id'],expected_version=detail['version'],action='clarification',text=owner.payload.text)
                response='需求补充已记录，等待 DAL 校验当前任务版本并接纳；没有新建开发任务。'
    except (ValueError,OSError,httpx.RequestError):
        response='暂时无法核对开发任务，本次未修改或新建需求，请稍后重试。'
    answer=dict(version=2,kind='conversation' if selected else 'clarification',
        task_status='completed' if selected else 'waiting',coverage='complete' if selected else 'partial',text=response,evidence=[])
    def writer(session,result):
        bridge._identity(session,owner.auth,'dal.read')
        bridge._identity(session,owner.auth,'dal.request')
        if selected:bridge.queue_recovery(owner.auth,command_id=owner.operation_id,source_message_ref=owner.anchor.event_id,payload=selected,_session=session)
        if owner.event_writer:owner.event_writer(session,result)
    owner.repo.finish(owner.lease,answer,now_ms=owner.now(),event_writer=writer,metadata=owner.pending_metadata)
    return ToolResult(answer,stop=True)
