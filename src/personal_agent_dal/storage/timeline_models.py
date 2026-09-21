"""Durable, encrypted Timeline intake and complete query snapshots."""
from datetime import datetime
from typing import Any

from sqlalchemy import Index, text, CheckConstraint, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent_core.sqlite import UtcTimestamp
from personal_agent_core.sqlite import EncryptedEnvelope
from personal_agent_dal.storage.models import Base


class DevelopmentRequest(Base):
    __tablename__ = 'development_requests'
    request_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_message_ref: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    request_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    __table_args__ = (
        CheckConstraint('version >= 1', name='request_version'),
        CheckConstraint("status IN ('clarifying','ready','linked','cancelled')", name='request_status'),
    )


class DevelopmentRequestRevision(Base):
    __tablename__ = 'development_request_revisions'
    revision_id: Mapped[str] = mapped_column(Text, primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey('development_requests.request_id', ondelete='RESTRICT'), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    body_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    sealed_body: Mapped[dict[str, Any]] = mapped_column(EncryptedEnvelope, nullable=False)
    __table_args__ = (UniqueConstraint('request_id', 'revision', name='request_revision'),)


class DevelopmentCommand(Base):
    __tablename__ = 'development_commands'
    command_id: Mapped[str] = mapped_column(Text, primary_key=True)
    body_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    sealed_result: Mapped[dict[str, Any]] = mapped_column(EncryptedEnvelope, nullable=False)


class DevelopmentEventStream(Base):
    __tablename__ = 'development_event_streams'
    stream_id: Mapped[str] = mapped_column(Text, primary_key=True)
    singleton: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    next_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    tail_digest: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (CheckConstraint('singleton = 1 AND next_seq >= 1', name='stream_singleton'),)


class DevelopmentEvent(Base):
    __tablename__ = 'development_events'
    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    stream_id: Mapped[str] = mapped_column(ForeignKey('development_event_streams.stream_id'), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str] = mapped_column(ForeignKey('development_requests.request_id'), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body_digest: Mapped[str] = mapped_column(Text, nullable=False)
    prev_digest: Mapped[str] = mapped_column(Text, nullable=False)
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    sealed_body: Mapped[dict[str, Any]] = mapped_column(EncryptedEnvelope, nullable=False)
    __table_args__ = (UniqueConstraint('stream_id', 'seq', name='event_sequence'), CheckConstraint('seq >= 1', name='sequence_positive'))


class DevelopmentQuerySnapshot(Base):
    __tablename__ = 'development_query_snapshots'
    snapshot_id: Mapped[str] = mapped_column(Text, primary_key=True)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    view: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    sealed_body: Mapped[dict[str, Any]] = mapped_column(EncryptedEnvelope, nullable=False)


class DevelopmentEventConsumer(Base):
    __tablename__ = 'development_event_consumers'
    stream_id: Mapped[str] = mapped_column(ForeignKey('development_event_streams.stream_id'), primary_key=True)
    consumer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    ack_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    tail_digest: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (CheckConstraint("consumer_id = 'pa-timeline' AND ack_seq >= 0 AND version >= 1", name='consumer_cursor'),)


class DevelopmentWorkflow(Base):
    __tablename__ = 'development_workflows'
    workflow_id: Mapped[str] = mapped_column(Text, primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey('development_requests.request_id', ondelete='RESTRICT'), nullable=False, unique=True)
    feature_id: Mapped[str | None] = mapped_column(ForeignKey('features.feature_id', ondelete='RESTRICT'), unique=True)
    blocker_reason: Mapped[str|None]=mapped_column(Text)
    accepted_delivery_id: Mapped[str|None] = mapped_column(Text)
    acceptance_receipt_id: Mapped[str|None] = mapped_column(Text)
    completed_at: Mapped[datetime|None] = mapped_column(UtcTimestamp)
    contract_version: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint("contract_version = 'dal.timeline-workflow/1.0' AND version >= 1", name='workflow_contract'),
        CheckConstraint("status IN ('active','blocked','paused','completed','cancelled')", name='workflow_status'),
        CheckConstraint("(phase = 'accepted' AND status = 'completed') OR (phase != 'accepted' AND status != 'completed')", name='workflow_completion'),
    )


class DevelopmentArtifact(Base):
    __tablename__='development_artifacts'
    artifact_id: Mapped[str]=mapped_column(Text,primary_key=True)
    workflow_id: Mapped[str]=mapped_column(ForeignKey('development_workflows.workflow_id'),nullable=False)
    kind: Mapped[str]=mapped_column(Text,nullable=False)
    revision: Mapped[int]=mapped_column(Integer,nullable=False)
    body_sha256: Mapped[str]=mapped_column(Text,nullable=False)
    sealed_body: Mapped[dict[str,Any]]=mapped_column(EncryptedEnvelope,nullable=False)
    source_step_id: Mapped[str]=mapped_column(Text,nullable=False)
    source_receipt_digest: Mapped[str]=mapped_column(Text,nullable=False)
    supersedes_id: Mapped[str|None]=mapped_column(ForeignKey('development_artifacts.artifact_id'))
    __table_args__=(UniqueConstraint('workflow_id','kind','revision',name='artifact_revision'),)


class RoleConfigurationRevision(Base):
    __tablename__='role_configuration_revisions'
    revision_id: Mapped[str]=mapped_column(Text,primary_key=True)
    configuration_id: Mapped[str]=mapped_column(Text,nullable=False)
    revision: Mapped[int]=mapped_column(Integer,nullable=False)
    body: Mapped[str]=mapped_column(Text,nullable=False)
    digest: Mapped[str]=mapped_column(Text,nullable=False)
    __table_args__=(UniqueConstraint('configuration_id','revision',name='role_config_revision'),)


class RoleConfigurationBinding(Base):
    __tablename__='role_configuration_bindings'
    scope: Mapped[str]=mapped_column(Text,primary_key=True)
    scope_id: Mapped[str]=mapped_column(Text,primary_key=True)
    revision_id: Mapped[str]=mapped_column(ForeignKey('role_configuration_revisions.revision_id'),nullable=False)
    version: Mapped[int]=mapped_column(Integer,nullable=False)
    __table_args__=(CheckConstraint("scope IN ('system','project','task') AND version >= 1",name='role_binding_scope'),)


class DevelopmentRoleSnapshot(Base):
    __tablename__='development_role_snapshots'
    snapshot_id: Mapped[str]=mapped_column(Text,primary_key=True)
    digest: Mapped[str]=mapped_column(Text,nullable=False,unique=True)
    body: Mapped[str]=mapped_column(Text,nullable=False)


class DevelopmentDecisionRequest(Base):
    __tablename__='development_decision_requests'
    decision_id: Mapped[str]=mapped_column(Text,primary_key=True)
    workflow_id: Mapped[str]=mapped_column(ForeignKey('development_workflows.workflow_id'),nullable=False)
    kind: Mapped[str]=mapped_column(Text,nullable=False)
    version: Mapped[int]=mapped_column(Integer,nullable=False)
    binding_digest: Mapped[str]=mapped_column(Text,nullable=False)
    sealed_binding: Mapped[dict[str,Any]]=mapped_column(EncryptedEnvelope,nullable=False)
    status: Mapped[str]=mapped_column(Text,nullable=False)
    expires_at: Mapped[datetime]=mapped_column(UtcTimestamp,nullable=False)
    __table_args__=(CheckConstraint("kind IN ('project_selection','prd','delivery') AND status IN ('pending','consumed','superseded','rejected')",name='development_decision_state'),)


class DevelopmentGate(Base):
    __tablename__='development_execution_gates'
    workflow_id: Mapped[str]=mapped_column(ForeignKey('development_workflows.workflow_id'),primary_key=True)
    mode: Mapped[str]=mapped_column(Text,nullable=False)
    epoch: Mapped[int]=mapped_column(Integer,nullable=False)
    version: Mapped[int]=mapped_column(Integer,nullable=False)
    __table_args__=(CheckConstraint("mode IN ('open','paused','cancelled','delivered') AND epoch >= 1 AND version >= 1",name='development_gate_state'),)


class DevelopmentDriverStep(Base):
    __tablename__='development_driver_steps'
    step_id: Mapped[str]=mapped_column(Text,primary_key=True)
    workflow_id: Mapped[str]=mapped_column(ForeignKey('development_workflows.workflow_id'),nullable=False)
    phase: Mapped[str]=mapped_column(Text,nullable=False)
    input_digest: Mapped[str]=mapped_column(Text,nullable=False)
    sealed_input: Mapped[dict[str,Any]]=mapped_column(EncryptedEnvelope,nullable=False)
    snapshot_id: Mapped[str|None]=mapped_column(ForeignKey('development_role_snapshots.snapshot_id'))
    expected_version: Mapped[int]=mapped_column(Integer,nullable=False)
    gate_epoch: Mapped[int]=mapped_column(Integer,nullable=False)
    cycle: Mapped[int]=mapped_column(Integer,nullable=False)
    stage_id: Mapped[str|None]=mapped_column(Text)
    stage_revision: Mapped[int|None]=mapped_column(Integer)
    status: Mapped[str]=mapped_column(Text,nullable=False)
    attempt_id: Mapped[str|None]=mapped_column(Text,unique=True)
    result_digest: Mapped[str|None]=mapped_column(Text)
    sealed_result: Mapped[dict[str,Any]|None]=mapped_column(EncryptedEnvelope)
    __table_args__=(UniqueConstraint('workflow_id','phase','input_digest','cycle',name='driver_step_input'),CheckConstraint("status IN ('prepared','dispatch_started','result_unknown','completed','failed','retired')",name='driver_step_state'),)


class DevelopmentStagePlan(Base):
    __tablename__='development_stage_plans'
    plan_id: Mapped[str]=mapped_column(Text,primary_key=True)
    workflow_id: Mapped[str]=mapped_column(ForeignKey('development_workflows.workflow_id'),nullable=False)
    revision: Mapped[int]=mapped_column(Integer,nullable=False)
    design_digest: Mapped[str]=mapped_column(Text,nullable=False)
    review_digest: Mapped[str]=mapped_column(Text,nullable=False)
    dag_digest: Mapped[str]=mapped_column(Text,nullable=False)
    __table_args__=(UniqueConstraint('workflow_id','revision',name='stage_plan_revision'),)


class DevelopmentStage(Base):
    __tablename__='development_stages'
    stage_id: Mapped[str]=mapped_column(Text,primary_key=True)
    revision: Mapped[int]=mapped_column(Integer,primary_key=True)
    plan_id: Mapped[str]=mapped_column(ForeignKey('development_stage_plans.plan_id'),nullable=False)
    ordinal: Mapped[int]=mapped_column(Integer,nullable=False)
    state: Mapped[str]=mapped_column(Text,nullable=False)
    state_version: Mapped[int]=mapped_column(Integer,nullable=False)
    sealed_goal: Mapped[dict[str,Any]]=mapped_column(EncryptedEnvelope,nullable=False)
    dependency_digest: Mapped[str|None]=mapped_column(Text)
    base_sha: Mapped[str|None]=mapped_column(Text)
    head_sha: Mapped[str|None]=mapped_column(Text)
    tree_sha: Mapped[str|None]=mapped_column(Text)
    verification_digest: Mapped[str|None]=mapped_column(Text)
    review_digest: Mapped[str|None]=mapped_column(Text)
    commit_digest: Mapped[str|None]=mapped_column(Text)
    review_fix_cycle: Mapped[int]=mapped_column(Integer,nullable=False)
    __table_args__=(CheckConstraint("state IN ('pending','ready','coding','verifying','reviewing','fixing','commit_ready','committed','blocked','paused','cancelled','invalidated') AND revision >= 1 AND state_version >= 1",name='development_stage_state'),)


from sqlalchemy import ForeignKeyConstraint


class DevelopmentStageDependency(Base):
    __tablename__='development_stage_dependencies'
    edge_id: Mapped[str]=mapped_column(Text,primary_key=True)
    plan_id: Mapped[str]=mapped_column(ForeignKey('development_stage_plans.plan_id'),nullable=False)
    upstream_id: Mapped[str]=mapped_column(Text,nullable=False)
    upstream_revision: Mapped[int]=mapped_column(Integer,nullable=False)
    downstream_id: Mapped[str]=mapped_column(Text,nullable=False)
    downstream_revision: Mapped[int]=mapped_column(Integer,nullable=False)
    __table_args__=(ForeignKeyConstraint(['upstream_id','upstream_revision'],['development_stages.stage_id','development_stages.revision']),ForeignKeyConstraint(['downstream_id','downstream_revision'],['development_stages.stage_id','development_stages.revision']),UniqueConstraint('plan_id','upstream_id','downstream_id',name='stage_dependency_edge'),)


class DevelopmentDependencySatisfaction(Base):
    __tablename__='development_stage_dependency_satisfactions'
    satisfaction_id: Mapped[str]=mapped_column(Text,primary_key=True)
    edge_id: Mapped[str]=mapped_column(ForeignKey('development_stage_dependencies.edge_id'),nullable=False,unique=True)
    commit_digest: Mapped[str]=mapped_column(Text,nullable=False)
    commit_sha: Mapped[str]=mapped_column(Text,nullable=False)
    tree_sha: Mapped[str]=mapped_column(Text,nullable=False)
    upstream_state_version: Mapped[int]=mapped_column(Integer,nullable=False)
    observed_at: Mapped[datetime]=mapped_column(UtcTimestamp,nullable=False)


class DevelopmentStageWriter(Base):
    __tablename__='development_stage_writers'
    workflow_id: Mapped[str]=mapped_column(ForeignKey('development_workflows.workflow_id'),primary_key=True)
    stage_id: Mapped[str]=mapped_column(Text,nullable=False)
    stage_revision: Mapped[int]=mapped_column(Integer,nullable=False)
    epoch: Mapped[int]=mapped_column(Integer,nullable=False)
    __table_args__=(ForeignKeyConstraint(['stage_id','stage_revision'],['development_stages.stage_id','development_stages.revision']),)


class DevelopmentDecisionReceipt(Base):
    __tablename__='development_decision_receipts'
    command_id: Mapped[str]=mapped_column(Text,primary_key=True)
    source_message_ref: Mapped[str]=mapped_column(Text,nullable=False,unique=True)
    decision_id: Mapped[str]=mapped_column(ForeignKey('development_decision_requests.decision_id'),nullable=False)
    body_digest: Mapped[str]=mapped_column(Text,nullable=False)
    sealed_result: Mapped[dict[str,Any]]=mapped_column(EncryptedEnvelope,nullable=False)


class DevelopmentProjectAuthorization(Base):
    __tablename__='development_project_authorizations'
    grant_id: Mapped[str]=mapped_column(Text,primary_key=True)
    version: Mapped[int]=mapped_column(Integer,nullable=False)
    project_id: Mapped[str]=mapped_column(Text,nullable=False)
    subject: Mapped[str]=mapped_column(Text,nullable=False)
    sealed_grant: Mapped[dict[str,Any]]=mapped_column(EncryptedEnvelope,nullable=False)
    digest: Mapped[str]=mapped_column(Text,nullable=False)
    expires_at: Mapped[datetime]=mapped_column(UtcTimestamp,nullable=False)
    revoked: Mapped[int]=mapped_column(Integer,nullable=False)


class DevelopmentProjectBinding(Base):
    __tablename__='development_project_bindings'
    workflow_id: Mapped[str]=mapped_column(ForeignKey('development_workflows.workflow_id'),primary_key=True)
    project_id: Mapped[str]=mapped_column(Text,nullable=False)
    grant_id: Mapped[str]=mapped_column(ForeignKey('development_project_authorizations.grant_id'),nullable=False)
    grant_version: Mapped[int]=mapped_column(Integer,nullable=False)
    route_artifact_id: Mapped[str]=mapped_column(ForeignKey('development_artifacts.artifact_id'),nullable=False)
    candidate_digest: Mapped[str]=mapped_column(Text,nullable=False)
    sealed_binding: Mapped[dict[str,Any]]=mapped_column(EncryptedEnvelope,nullable=False)


class DevelopmentExecution(Base):
    """One irreversible execution identity per immutable driver step."""
    __tablename__ = 'development_executions'
    execution_id: Mapped[str] = mapped_column(Text, primary_key=True)
    step_id: Mapped[str] = mapped_column(ForeignKey('development_driver_steps.step_id'), nullable=False, unique=True)
    worker_id: Mapped[str] = mapped_column(Text, nullable=False)
    boot_id: Mapped[str] = mapped_column(Text, nullable=False)
    supervisor_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    lease_until: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    admission_digest: Mapped[str] = mapped_column(Text, nullable=False)
    binding_digest: Mapped[str] = mapped_column(Text, nullable=False)
    sealed_binding: Mapped[dict[str,Any]] = mapped_column(EncryptedEnvelope, nullable=False)
    grant_id: Mapped[str|None]=mapped_column(Text)
    reserved_seconds: Mapped[int]=mapped_column(Integer,nullable=False,default=0)
    charged_seconds: Mapped[int|None]=mapped_column(Integer)
    started_at: Mapped[datetime|None]=mapped_column(UtcTimestamp)
    stop_receipt: Mapped[dict[str,Any]|None] = mapped_column(EncryptedEnvelope)
    receipt_digest: Mapped[str|None] = mapped_column(Text)
    sealed_receipt: Mapped[dict[str,Any]|None] = mapped_column(EncryptedEnvelope)
    __table_args__ = (CheckConstraint('supervisor_epoch >= 1', name='execution_supervisor_epoch'),)


class DevelopmentWorkspace(Base):
    __tablename__='development_workspaces'
    workflow_id: Mapped[str]=mapped_column(ForeignKey('development_workflows.workflow_id'),primary_key=True)
    reservation_id: Mapped[str]=mapped_column(Text,nullable=False,unique=True)
    generation: Mapped[int]=mapped_column(Integer,nullable=False)
    directory_digest: Mapped[str]=mapped_column(Text,nullable=False)
    base_sha: Mapped[str]=mapped_column(Text,nullable=False)
    toolchain_digest: Mapped[str]=mapped_column(Text,nullable=False)
    receipt_digest: Mapped[str]=mapped_column(Text,nullable=False)
    sealed_manifest: Mapped[dict[str,Any]]=mapped_column(EncryptedEnvelope,nullable=False)


class DevelopmentDelivery(Base):
    __tablename__='development_deliveries'
    delivery_id: Mapped[str]=mapped_column(Text,primary_key=True)
    workflow_id: Mapped[str]=mapped_column(ForeignKey('development_workflows.workflow_id'),nullable=False)
    artifact_id: Mapped[str]=mapped_column(ForeignKey('development_artifacts.artifact_id'),nullable=False,unique=True)
    manifest_digest: Mapped[str]=mapped_column(Text,nullable=False)
    sealed_manifest: Mapped[dict[str,Any]]=mapped_column(EncryptedEnvelope,nullable=False)
    source_step_id: Mapped[str]=mapped_column(ForeignKey('development_driver_steps.step_id'),nullable=False,unique=True)
    gate_version: Mapped[int]=mapped_column(Integer,nullable=False)
    gate_epoch: Mapped[int]=mapped_column(Integer,nullable=False)


class DevelopmentAcceptanceProbe(Base):
    __tablename__='development_acceptance_probes'
    command_id: Mapped[str]=mapped_column(Text,primary_key=True)
    source_message_ref: Mapped[str]=mapped_column(Text,nullable=False,unique=True)
    decision_id: Mapped[str]=mapped_column(ForeignKey('development_decision_requests.decision_id'),nullable=False)
    workflow_id: Mapped[str]=mapped_column(ForeignKey('development_workflows.workflow_id'),nullable=False)
    fingerprint: Mapped[str]=mapped_column(Text,nullable=False)
    nonce: Mapped[str]=mapped_column(Text,nullable=False,unique=True)
    step_id: Mapped[str|None]=mapped_column(ForeignKey('development_driver_steps.step_id'),unique=True)
    sealed_command: Mapped[dict[str,Any]]=mapped_column(EncryptedEnvelope,nullable=False)
    result_digest: Mapped[str|None]=mapped_column(Text)
    observed_at: Mapped[datetime|None]=mapped_column(UtcTimestamp)
    sealed_result: Mapped[dict[str,Any]|None]=mapped_column(EncryptedEnvelope)


class DevelopmentStageMembership(Base):
    """A frozen plan can retain an unaffected stage without copying its proof."""
    __tablename__='development_stage_memberships'
    plan_id: Mapped[str]=mapped_column(ForeignKey('development_stage_plans.plan_id'),primary_key=True)
    stage_id: Mapped[str]=mapped_column(Text,primary_key=True)
    stage_revision: Mapped[int]=mapped_column(Integer,nullable=False)
    ordinal: Mapped[int]=mapped_column(Integer,nullable=False)
    __table_args__=(ForeignKeyConstraint(['stage_id','stage_revision'],['development_stages.stage_id','development_stages.revision']),)


class DevelopmentRemoteEffect(Base):
    __tablename__='development_remote_effects'
    step_id: Mapped[str]=mapped_column(ForeignKey('development_driver_steps.step_id'),primary_key=True)
    operation: Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest: Mapped[str]=mapped_column(Text,nullable=False)
    status: Mapped[str]=mapped_column(Text,nullable=False)
    sealed_result: Mapped[dict[str,Any]|None]=mapped_column(EncryptedEnvelope)
    __table_args__=(CheckConstraint("status IN ('started','completed','unknown')",name='remote_effect_state'),)


class DevelopmentAuthorizationRequest(Base):
    __tablename__='development_authorization_requests'
    generation: Mapped[int]=mapped_column(Integer,nullable=False,default=0,server_default='0')
    current_proposal_id: Mapped[str|None]=mapped_column(Text)
    workflow_id: Mapped[str]=mapped_column(ForeignKey('development_workflows.workflow_id'),primary_key=True)
    request_version: Mapped[int]=mapped_column(Integer,nullable=False)
    status: Mapped[str]=mapped_column(Text,nullable=False)
    expires_at: Mapped[datetime]=mapped_column(UtcTimestamp,nullable=False)
    grant_id: Mapped[str|None]=mapped_column(ForeignKey('development_project_authorizations.grant_id'))
    sealed_scope: Mapped[dict[str,Any]]=mapped_column(EncryptedEnvelope,nullable=False)
    __table_args__=(CheckConstraint("status IN ('pending','granted','expired','rejected')",name='authorization_request_state'),)


# Metadata-created databases must enforce the same ownership fence as migration
# 0022. Inspect actual tables because historical fixtures create partial schemas.
from sqlalchemy import event, inspect


@event.listens_for(Base.metadata, 'after_create')
def _install_timeline_ownership_guards(metadata, connection, **kwargs):
    if connection.dialect.name != 'sqlite':
        return
    tables = set(inspect(connection).get_table_names())
    if 'development_workflows' not in tables:
        return
    legacy = ('approval_action_receipts', 'approvals', 'capabilities',
              'commit_capabilities', 'decisions', 'execution_control_receipts',
              'execution_gates', 'impact_reports', 'leases', 'recovery_cases',
              'workflow_actions', 'workflow_selections')
    for table in (*legacy, 'features'):
        if table not in tables:
            continue
        operations = ('UPDATE', 'DELETE') if table == 'features' else ('INSERT', 'UPDATE')
        reference = 'OLD' if table == 'features' else 'NEW'
        for operation in operations:
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS timeline_owner_{table}_{operation.lower()} "
                f"BEFORE {operation} ON {table} WHEN EXISTS(SELECT 1 FROM "
                f"development_workflows WHERE feature_id={reference}.feature_id) "
                "BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")


class DevelopmentProjectTemplate(Base):
    __tablename__='development_project_templates'
    template_id: Mapped[str]=mapped_column(Text,primary_key=True)
    project_id: Mapped[str]=mapped_column(Text,nullable=False)
    revision: Mapped[int]=mapped_column(Integer,nullable=False)
    active: Mapped[int]=mapped_column(Integer,nullable=False)
    digest: Mapped[str]=mapped_column(Text,nullable=False)
    sealed_template: Mapped[dict[str,Any]]=mapped_column(EncryptedEnvelope,nullable=False)
    observed_at: Mapped[datetime]=mapped_column(UtcTimestamp,nullable=False)
    expires_at: Mapped[datetime]=mapped_column(UtcTimestamp,nullable=False)
    evidence_digest: Mapped[str]=mapped_column(Text,nullable=False)
    actor: Mapped[str]=mapped_column(Text,nullable=False)
    __table_args__=(Index('uq_active_project_template','project_id',unique=True,sqlite_where=text('active = 1')),
        UniqueConstraint('project_id','revision',name='project_template_revision'),
        CheckConstraint('revision >= 1 AND active IN (0,1)',name='project_template_state'),)


class DevelopmentAuthorizationProposal(Base):
    __tablename__='development_authorization_proposals'
    proposal_id: Mapped[str]=mapped_column(Text,primary_key=True)
    workflow_id: Mapped[str]=mapped_column(ForeignKey('development_workflows.workflow_id'),nullable=False)
    template_id: Mapped[str]=mapped_column(ForeignKey('development_project_templates.template_id'),nullable=False)
    generation: Mapped[int]=mapped_column(Integer,nullable=False)
    status: Mapped[str]=mapped_column(Text,nullable=False)
    binding_digest: Mapped[str]=mapped_column(Text,nullable=False)
    sealed_proposal: Mapped[dict[str,Any]]=mapped_column(EncryptedEnvelope,nullable=False)
    expires_at: Mapped[datetime]=mapped_column(UtcTimestamp,nullable=False)
    __table_args__=(UniqueConstraint('workflow_id','generation',name='authorization_generation'),
        CheckConstraint("generation >= 1 AND status IN ('pending','granted','superseded','expired')",name='authorization_proposal_state'),)


class DevelopmentAuthorizationPolicy(Base):
    __tablename__='development_authorization_policies'
    grant_id: Mapped[str]=mapped_column(ForeignKey('development_project_authorizations.grant_id'),primary_key=True)
    workflow_id: Mapped[str]=mapped_column(ForeignKey('development_workflows.workflow_id'),nullable=False,unique=True)
    template_id: Mapped[str]=mapped_column(ForeignKey('development_project_templates.template_id'),nullable=False)
    template_digest: Mapped[str]=mapped_column(Text,nullable=False)
    proposal_id: Mapped[str]=mapped_column(ForeignKey('development_authorization_proposals.proposal_id'),nullable=False)
