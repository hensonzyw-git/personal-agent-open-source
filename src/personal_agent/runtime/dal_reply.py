"""Isolated ADK candidate interpretation; only deterministic Host can authorize."""
import json
from uuid import uuid4
from google.genai import types
from personal_agent.runtime.adk_runtime import AdkRuntime,ModelAttemptInput
from personal_agent.runtime.run_tools import RunToolSpec,ToolResult
from personal_agent.runtime.run_catalog import obj
from personal_agent.runtime.response_witness import ResponseViolation
from personal_agent.runtime.run_store import RunStateError
from personal_agent.api.dal_contexts import pending, select_reply
from personal_agent_dal.timeline.decisions import parse_decision,parse_project_choice


class CandidateHost:
    def __init__(self,owner,context):self.owner,self.context,self.calls=owner,context,0
    async def prepare_model(self,*,tools):
        if self.calls:raise ResponseViolation('decision_one_attempt_only')
        self.calls+=1
        prepared=await self.owner.prepare_model(tools=tools)
        request=prepared.request.model_copy(deep=True)
        request.config.system_instruction='只解释当前用户对于绑定待审项的决定，返回一个 decision_candidate 调用。不能执行任何动作。存在条件、引用、否定歧义或多个意图时用 clarify。'
        request.contents=[types.Content(role='user',parts=[types.Part(text=json.dumps(dict(text=self.owner.payload.text,kind=self.context['kind']),ensure_ascii=False))])]
        return ModelAttemptInput(prepared.binding,request)
    def model_for(self,prepared):return self.owner.model_for(prepared)
    async def accept_batch(self,binding,calls):
        if len(calls)!=1:raise ResponseViolation('decision_one_candidate_required')
        self.owner.repo.check(self.owner.lease,now_ms=self.owner.now());return self.owner.lease
    async def check_active(self,authority):await self.owner.check_active(authority)
    async def execute(self,call,authority):return ToolResult(call.args,stop=True)
    async def failed_run(self,code):pass
    async def record_model_usage(self,binding,response):
        if hasattr(self.owner,'record_model_usage'):await self.owner.record_model_usage(binding,response)


async def handle_reply(owner):
    bridge=owner.deps.dal_timeline
    if bridge is None or 'dal.read' not in owner.auth.scopes:return False
    contexts=pending(bridge,owner.auth)
    explicit=owner.payload.dal_reply_context
    invalid_context=False
    if explicit is not None:
        try:contexts=select_reply(bridge,owner.auth,explicit,contexts)
        except ValueError:contexts=[];invalid_context=True
    if not contexts and explicit is None:return False
    text=owner.payload.text
    parsed=parse_decision(text)
    choice_like=text.strip().startswith(('选','选择'))
    if parsed is None and not choice_like and explicit is None:return False
    if invalid_context:
        response='这个回复对象已失效或过期，请重新打开待审消息后回复。';selected=None
    elif owner.auth.client_wire_version<6:
        response='请更新 App 后再在 Timeline 审批或选择项目。';selected=None
    elif len(contexts)!=1:
        response='当前有多个待处理开发事项，请打开准确的任务文档并指定回复对象。';selected=None
    else:
        selected=contexts[0]
        if selected['kind']=='project_selection':
            valid=parse_project_choice(text,selected['binding']['candidates']) is not None
        else:
            spec=RunToolSpec('decision_candidate','decision.candidate','control','只返回决定候选，不执行审批。',
                obj({'decision':{'enum':['approve','request_changes','reject','clarify']},'feedback':{'type':'string','maxLength':32768}}))
            try:proposal=await AdkRuntime(host=CandidateHost(owner,selected),specs=[spec],max_reads=0).run()
            except (ResponseViolation, ValueError, KeyError, TypeError):proposal=None
            valid=parsed is not None and proposal is not None and proposal['decision']==parsed['decision'] and proposal['feedback']==parsed['feedback']
        if not valid:response='这条回复还不能唯一确定决定，请针对当前文档直接回复“通过”“拒绝”或“修改：具体意见”。';selected=None
        else:response='已记录你的选择，等待 DAL 校验并接纳。'
    # Bind this user turn before atomically recording its command and reply.
    if owner.repo.snapshot(owner.operation_id)['task_id'] is None:
        id='task_'+uuid4().hex
        owner.repo.bind(owner.operation_id,task_id=None,new_task_id=id,now_ms=owner.now(),lease=owner.lease,
            sealed_goal=owner.repo.seal('agent_tasks','sealed_goal',id,text),sealed_constraints=owner.repo.seal('agent_tasks','sealed_constraints',id,[]))
    answer=dict(version=2,kind='conversation' if selected else 'clarification',task_status='completed' if selected else 'waiting',coverage='complete' if selected else 'partial',text=response,evidence=[])
    def writer(session,result):
        if selected:bridge.queue_decision(owner.auth,command_id=owner.operation_id,source_message_ref=owner.anchor.event_id,context=selected,text=text,_session=session)
        if owner.event_writer:owner.event_writer(session,result)
    owner.repo.finish(owner.lease,answer,now_ms=owner.now(),event_writer=writer)
    return True
