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
