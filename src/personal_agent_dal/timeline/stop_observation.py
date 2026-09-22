"""Recover a deterministic commit by its content identity, never by replay."""
from personal_agent_dal.storage.timeline_models import DevelopmentDriverStep as Step,DevelopmentWorkflow as Workflow,DevelopmentGate as Gate,DevelopmentExecution
from personal_agent_dal.timeline.requests import digest
from personal_agent_dal.github.workflow_objects import object_sha


def apply(driver,session,step,execution,observation,assertion):
    if step.status not in ('dispatch_started','result_unknown'):return
    inputs=driver.r._open(Step,step.step_id,'sealed_input',step.sealed_input)
    wf=session.get(Workflow,step.workflow_id);gate=session.get(Gate,step.workflow_id)
    unchanged=(wf.status=='active' and wf.phase==step.phase and wf.version==step.expected_version
        and gate is not None and gate.mode=='open' and gate.epoch==step.gate_epoch)
    if step.phase=='stage_commit' and unchanged:
        candidate=inputs['stage']['candidate'];stage=inputs['stage']
        from personal_agent_dal.timeline.commit_contract import commit_message
        raw=(f"tree {candidate['tree_sha']}\nparent {candidate['head_sha']}\n"
            f"author DAL <dal@localhost> {inputs['prepared_at']} +0000\n"
            f"committer DAL <dal@localhost> {inputs['prepared_at']} +0000\n\n"
            ).encode()+commit_message(stage)
        expected=object_sha('commit',raw)
        if observation['head_sha']==expected and observation['tree_sha']==candidate['tree_sha']:
            # Grants/kill switch still apply; a late observation is not renewed
            # execution permission. The stop proof is explicitly retained.
            driver._current(session,step)
            result=dict(kind='commit',text='阶段提交已完成并回读。',candidate=candidate,
                committed=True,commit_sha=expected,parent_sha=candidate['head_sha'])
            receipt=dict(binding=driver.authority.binding(execution),result_digest=digest(result),
                assertion=assertion,reconciliation_observation=observation)
            execution.receipt_digest=digest(receipt)
            execution.sealed_receipt=driver.r._seal(DevelopmentExecution,execution.execution_id,'sealed_receipt',receipt)
            step.status='completed';step.result_digest=digest(result)
            step.sealed_result=driver.r._seal(Step,step.step_id,'sealed_result',result);session.flush()
            from personal_agent_dal.timeline.stage_driver import accept_stage
            accept_stage(driver,session,wf,step,result,execution)
            return
    source_write=step.phase in ('coding','fix','stage_commit','workspace_prepare')
    baseline=inputs.get('stage',{}).get('candidate',{}).get('head_sha')
    # A readback of the unchanged Git head proves no commit landed. A human
    # resume can inspect/continue the preserved worktree; no automatic replay.
    unchanged_head=baseline is not None and observation['head_sha']==baseline and observation['tree_sha'] is not None
    step.status='failed' if not source_write or unchanged_head else 'result_unknown'
    if wf.status=='active':driver._block(session,wf,'EXECUTION_STOPPED_REVIEW_REQUIRED')
