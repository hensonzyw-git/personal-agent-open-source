"""Bind workflow attempts to immutable authenticated worker authority."""
from alembic import op
import sqlalchemy as sa
revision='0022'
down_revision='0021'
branch_labels=None
depends_on=None

def upgrade():
    op.execute('\nCREATE TABLE development_executions (\n\texecution_id TEXT NOT NULL, \n\tstep_id TEXT NOT NULL, \n\tworker_id TEXT NOT NULL, \n\tboot_id TEXT NOT NULL, \n\tsupervisor_epoch INTEGER NOT NULL, \n\tlease_id TEXT NOT NULL, \n\tlease_until TEXT NOT NULL, \n\tadmission_digest TEXT NOT NULL, \n\tbinding_digest TEXT NOT NULL, \n\tsealed_binding TEXT NOT NULL, \n\tstop_receipt TEXT, \n\treceipt_digest TEXT, \n\tsealed_receipt TEXT, \n\tCONSTRAINT pk_development_executions PRIMARY KEY (execution_id), \n\tCONSTRAINT ck_development_executions_execution_supervisor_epoch CHECK (supervisor_epoch >= 1), \n\tCONSTRAINT uq_development_executions_step_id UNIQUE (step_id), \n\tCONSTRAINT fk_development_executions_step_id FOREIGN KEY(step_id) REFERENCES development_driver_steps (step_id), \n\tCONSTRAINT uq_development_executions_lease_id UNIQUE (lease_id)\n)\n\n')

    op.add_column('development_executions',sa.Column('grant_id',sa.Text()))
    op.add_column('development_executions',sa.Column('reserved_seconds',sa.Integer(),nullable=False,server_default='0'))
    op.add_column('development_executions',sa.Column('charged_seconds',sa.Integer()))
    op.add_column('development_executions',sa.Column('started_at',sa.Text()))

    op.execute('\nCREATE TABLE development_workspaces (\n\tworkflow_id TEXT NOT NULL, \n\treservation_id TEXT NOT NULL, \n\tgeneration INTEGER NOT NULL, \n\tdirectory_digest TEXT NOT NULL, \n\tbase_sha TEXT NOT NULL, \n\ttoolchain_digest TEXT NOT NULL, \n\treceipt_digest TEXT NOT NULL, \n\tsealed_manifest TEXT NOT NULL, \n\tCONSTRAINT pk_development_workspaces PRIMARY KEY (workflow_id), \n\tCONSTRAINT fk_development_workspaces_workflow_id FOREIGN KEY(workflow_id) REFERENCES development_workflows (workflow_id), \n\tCONSTRAINT uq_development_workspaces_reservation_id UNIQUE (reservation_id)\n)\n\n')

    op.execute('\nCREATE TABLE development_deliveries (\n\tdelivery_id TEXT NOT NULL, \n\tworkflow_id TEXT NOT NULL, \n\tartifact_id TEXT NOT NULL, \n\tmanifest_digest TEXT NOT NULL, \n\tsealed_manifest TEXT NOT NULL, \n\tsource_step_id TEXT NOT NULL, \n\tgate_version INTEGER NOT NULL, \n\tgate_epoch INTEGER NOT NULL, \n\tCONSTRAINT pk_development_deliveries PRIMARY KEY (delivery_id), \n\tCONSTRAINT fk_development_deliveries_workflow_id FOREIGN KEY(workflow_id) REFERENCES development_workflows (workflow_id), \n\tCONSTRAINT uq_development_deliveries_artifact_id UNIQUE (artifact_id), \n\tCONSTRAINT fk_development_deliveries_artifact_id FOREIGN KEY(artifact_id) REFERENCES development_artifacts (artifact_id), \n\tCONSTRAINT uq_development_deliveries_source_step_id UNIQUE (source_step_id), \n\tCONSTRAINT fk_development_deliveries_source_step_id FOREIGN KEY(source_step_id) REFERENCES development_driver_steps (step_id)\n)\n\n')
    op.execute('\nCREATE TABLE development_acceptance_probes (\n\tcommand_id TEXT NOT NULL, \n\tsource_message_ref TEXT NOT NULL, \n\tdecision_id TEXT NOT NULL, \n\tworkflow_id TEXT NOT NULL, \n\tfingerprint TEXT NOT NULL, \n\tnonce TEXT NOT NULL, \n\tstep_id TEXT, \n\tsealed_command TEXT NOT NULL, \n\tresult_digest TEXT, \n\tobserved_at TEXT, \n\tsealed_result TEXT, \n\tCONSTRAINT pk_development_acceptance_probes PRIMARY KEY (command_id), \n\tCONSTRAINT uq_development_acceptance_probes_source_message_ref UNIQUE (source_message_ref), \n\tCONSTRAINT fk_development_acceptance_probes_decision_id FOREIGN KEY(decision_id) REFERENCES development_decision_requests (decision_id), \n\tCONSTRAINT fk_development_acceptance_probes_workflow_id FOREIGN KEY(workflow_id) REFERENCES development_workflows (workflow_id), \n\tCONSTRAINT uq_development_acceptance_probes_nonce UNIQUE (nonce), \n\tCONSTRAINT uq_development_acceptance_probes_step_id UNIQUE (step_id), \n\tCONSTRAINT fk_development_acceptance_probes_step_id FOREIGN KEY(step_id) REFERENCES development_driver_steps (step_id)\n)\n\n')
    op.add_column("development_workflows",sa.Column('accepted_delivery_id',sa.Text(),nullable=True))
    op.add_column("development_workflows",sa.Column('acceptance_receipt_id',sa.Text(),nullable=True))
    op.add_column("development_workflows",sa.Column('completed_at',sa.Text(),nullable=True))

    op.add_column('development_workflows',sa.Column('blocker_reason',sa.Text(),nullable=True))
    op.create_table('development_authorization_requests',
        sa.Column('workflow_id',sa.Text(),sa.ForeignKey('development_workflows.workflow_id'),primary_key=True),
        sa.Column('request_version',sa.Integer(),nullable=False),sa.Column('status',sa.Text(),nullable=False),
        sa.Column('expires_at',sa.Text(),nullable=False),
        sa.Column('grant_id',sa.Text(),sa.ForeignKey('development_project_authorizations.grant_id')),
        sa.Column('sealed_scope',sa.Text(),nullable=False),
        sa.CheckConstraint("status IN ('pending','granted','expired','rejected')",name='authorization_request_state'))

    op.create_table('development_remote_effects',
        sa.Column('step_id',sa.Text(),sa.ForeignKey('development_driver_steps.step_id'),primary_key=True),
        sa.Column('operation',sa.Text(),nullable=False),sa.Column('payload_digest',sa.Text(),nullable=False),
        sa.Column('status',sa.Text(),nullable=False),sa.Column('sealed_result',sa.Text()),
        sa.CheckConstraint("status IN ('started','completed','unknown')",name='remote_effect_state'))

    op.create_table('development_stage_memberships',
        sa.Column('plan_id',sa.Text(),sa.ForeignKey('development_stage_plans.plan_id'),primary_key=True),
        sa.Column('stage_id',sa.Text(),primary_key=True),
        sa.Column('stage_revision',sa.Integer(),nullable=False),
        sa.Column('ordinal',sa.Integer(),nullable=False),
        sa.ForeignKeyConstraint(['stage_id','stage_revision'],['development_stages.stage_id','development_stages.revision']))
    op.execute('INSERT INTO development_stage_memberships SELECT plan_id,stage_id,revision,ordinal FROM development_stages')

    op.execute("CREATE TRIGGER timeline_owner_approval_action_receipts_insert BEFORE INSERT ON approval_action_receipts WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_approval_action_receipts_update BEFORE UPDATE ON approval_action_receipts WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_approvals_insert BEFORE INSERT ON approvals WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_approvals_update BEFORE UPDATE ON approvals WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_capabilities_insert BEFORE INSERT ON capabilities WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_capabilities_update BEFORE UPDATE ON capabilities WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_commit_capabilities_insert BEFORE INSERT ON commit_capabilities WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_commit_capabilities_update BEFORE UPDATE ON commit_capabilities WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_decisions_insert BEFORE INSERT ON decisions WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_decisions_update BEFORE UPDATE ON decisions WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_execution_control_receipts_insert BEFORE INSERT ON execution_control_receipts WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_execution_control_receipts_update BEFORE UPDATE ON execution_control_receipts WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_execution_gates_insert BEFORE INSERT ON execution_gates WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_execution_gates_update BEFORE UPDATE ON execution_gates WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_impact_reports_insert BEFORE INSERT ON impact_reports WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_impact_reports_update BEFORE UPDATE ON impact_reports WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_leases_insert BEFORE INSERT ON leases WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_leases_update BEFORE UPDATE ON leases WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_recovery_cases_insert BEFORE INSERT ON recovery_cases WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_recovery_cases_update BEFORE UPDATE ON recovery_cases WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_workflow_actions_insert BEFORE INSERT ON workflow_actions WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_workflow_actions_update BEFORE UPDATE ON workflow_actions WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_workflow_selections_insert BEFORE INSERT ON workflow_selections WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_workflow_selections_update BEFORE UPDATE ON workflow_selections WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=NEW.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_features_update BEFORE UPDATE ON features WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=OLD.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")
    op.execute("CREATE TRIGGER timeline_owner_features_delete BEFORE DELETE ON features WHEN EXISTS(SELECT 1 FROM development_workflows WHERE feature_id=OLD.feature_id) BEGIN SELECT RAISE(ABORT, 'WORKFLOW_CONTRACT_MISMATCH'); END")

def downgrade():
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_approval_action_receipts_insert")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_approval_action_receipts_update")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_approvals_insert")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_approvals_update")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_capabilities_insert")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_capabilities_update")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_commit_capabilities_insert")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_commit_capabilities_update")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_decisions_insert")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_decisions_update")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_execution_control_receipts_insert")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_execution_control_receipts_update")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_execution_gates_insert")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_execution_gates_update")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_impact_reports_insert")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_impact_reports_update")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_leases_insert")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_leases_update")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_recovery_cases_insert")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_recovery_cases_update")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_workflow_actions_insert")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_workflow_actions_update")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_workflow_selections_insert")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_workflow_selections_update")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_features_update")
    op.execute("DROP TRIGGER IF EXISTS timeline_owner_features_delete")

    if op.get_bind().execute(sa.text('SELECT 1 FROM development_executions LIMIT 1')).first():
        raise RuntimeError('Execution authority must be preserved')
    if op.get_bind().execute(sa.text('SELECT 1 FROM development_workspaces LIMIT 1')).first():
        raise RuntimeError('Workspace evidence must be preserved')
    for name in ('development_authorization_requests','development_remote_effects','development_acceptance_probes','development_deliveries'):
        if op.get_bind().execute(sa.text('SELECT 1 FROM '+name+' LIMIT 1')).first():
            raise RuntimeError('Delivery authority must be preserved')
        op.drop_table(name)
    with op.batch_alter_table('development_workflows') as batch:
        for column in ('blocker_reason','accepted_delivery_id','acceptance_receipt_id','completed_at'):batch.drop_column(column)
    op.drop_table('development_stage_memberships')
    op.drop_table('development_workspaces')
    op.drop_table('development_executions')
