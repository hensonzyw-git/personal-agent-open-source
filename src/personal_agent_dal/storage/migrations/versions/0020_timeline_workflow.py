"""Immutable Timeline configuration, artifacts, decisions and Stage authority."""
from alembic import op
import sqlalchemy as sa
revision="0020"
down_revision="0019"
branch_labels=None
depends_on=None


def upgrade():
    op.execute('\nCREATE TABLE development_artifacts (\n\tartifact_id TEXT NOT NULL, \n\tworkflow_id TEXT NOT NULL, \n\tkind TEXT NOT NULL, \n\trevision INTEGER NOT NULL, \n\tbody_sha256 TEXT NOT NULL, \n\tsealed_body TEXT NOT NULL, \n\tsource_step_id TEXT NOT NULL, \n\tsource_receipt_digest TEXT NOT NULL, \n\tsupersedes_id TEXT, \n\tCONSTRAINT pk_development_artifacts PRIMARY KEY (artifact_id), \n\tCONSTRAINT artifact_revision UNIQUE (workflow_id, kind, revision), \n\tCONSTRAINT fk_development_artifacts_workflow_id FOREIGN KEY(workflow_id) REFERENCES development_workflows (workflow_id), \n\tCONSTRAINT fk_development_artifacts_supersedes_id FOREIGN KEY(supersedes_id) REFERENCES development_artifacts (artifact_id)\n)\n\n')
    op.execute('\nCREATE TABLE role_configuration_revisions (\n\trevision_id TEXT NOT NULL, \n\tconfiguration_id TEXT NOT NULL, \n\trevision INTEGER NOT NULL, \n\tbody TEXT NOT NULL, \n\tdigest TEXT NOT NULL, \n\tCONSTRAINT pk_role_configuration_revisions PRIMARY KEY (revision_id), \n\tCONSTRAINT role_config_revision UNIQUE (configuration_id, revision)\n)\n\n')
    op.execute("\nCREATE TABLE role_configuration_bindings (\n\tscope TEXT NOT NULL, \n\tscope_id TEXT NOT NULL, \n\trevision_id TEXT NOT NULL, \n\tversion INTEGER NOT NULL, \n\tCONSTRAINT pk_role_configuration_bindings PRIMARY KEY (scope, scope_id), \n\tCONSTRAINT ck_role_configuration_bindings_role_binding_scope CHECK (scope IN ('system','project','task') AND version >= 1), \n\tCONSTRAINT fk_role_configuration_bindings_revision_id FOREIGN KEY(revision_id) REFERENCES role_configuration_revisions (revision_id)\n)\n\n")
    op.execute('\nCREATE TABLE development_role_snapshots (\n\tsnapshot_id TEXT NOT NULL, \n\tdigest TEXT NOT NULL, \n\tbody TEXT NOT NULL, \n\tCONSTRAINT pk_development_role_snapshots PRIMARY KEY (snapshot_id), \n\tCONSTRAINT uq_development_role_snapshots_digest UNIQUE (digest)\n)\n\n')
    op.execute("\nCREATE TABLE development_decision_requests (\n\tdecision_id TEXT NOT NULL, \n\tworkflow_id TEXT NOT NULL, \n\tkind TEXT NOT NULL, \n\tversion INTEGER NOT NULL, \n\tbinding_digest TEXT NOT NULL, \n\tsealed_binding TEXT NOT NULL, \n\tstatus TEXT NOT NULL, \n\texpires_at TEXT NOT NULL, \n\tCONSTRAINT pk_development_decision_requests PRIMARY KEY (decision_id), \n\tCONSTRAINT ck_development_decision_requests_development_decision_state CHECK (kind IN ('project_selection','prd','delivery') AND status IN ('pending','consumed','superseded','rejected')), \n\tCONSTRAINT fk_development_decision_requests_workflow_id FOREIGN KEY(workflow_id) REFERENCES development_workflows (workflow_id)\n)\n\n")
    op.execute("\nCREATE TABLE development_execution_gates (\n\tworkflow_id TEXT NOT NULL, \n\tmode TEXT NOT NULL, \n\tepoch INTEGER NOT NULL, \n\tversion INTEGER NOT NULL, \n\tCONSTRAINT pk_development_execution_gates PRIMARY KEY (workflow_id), \n\tCONSTRAINT ck_development_execution_gates_development_gate_state CHECK (mode IN ('open','paused','cancelled','delivered') AND epoch >= 1 AND version >= 1), \n\tCONSTRAINT fk_development_execution_gates_workflow_id FOREIGN KEY(workflow_id) REFERENCES development_workflows (workflow_id)\n)\n\n")
    op.execute("\nCREATE TABLE development_driver_steps (\n\tstep_id TEXT NOT NULL, \n\tworkflow_id TEXT NOT NULL, \n\tphase TEXT NOT NULL, \n\tinput_digest TEXT NOT NULL, \n\tsealed_input TEXT NOT NULL, \n\tsnapshot_id TEXT, \n\texpected_version INTEGER NOT NULL, \n\tgate_epoch INTEGER NOT NULL, \n\tcycle INTEGER NOT NULL, \n\tstage_id TEXT, \n\tstage_revision INTEGER, \n\tstatus TEXT NOT NULL, \n\tattempt_id TEXT, \n\tresult_digest TEXT, \n\tsealed_result TEXT, \n\tCONSTRAINT pk_development_driver_steps PRIMARY KEY (step_id), \n\tCONSTRAINT driver_step_input UNIQUE (workflow_id, phase, input_digest, cycle), \n\tCONSTRAINT ck_development_driver_steps_driver_step_state CHECK (status IN ('prepared','dispatch_started','result_unknown','completed','failed','retired')), \n\tCONSTRAINT fk_development_driver_steps_workflow_id FOREIGN KEY(workflow_id) REFERENCES development_workflows (workflow_id), \n\tCONSTRAINT fk_development_driver_steps_snapshot_id FOREIGN KEY(snapshot_id) REFERENCES development_role_snapshots (snapshot_id), \n\tCONSTRAINT uq_development_driver_steps_attempt_id UNIQUE (attempt_id)\n)\n\n")
    op.execute('\nCREATE TABLE development_stage_plans (\n\tplan_id TEXT NOT NULL, \n\tworkflow_id TEXT NOT NULL, \n\trevision INTEGER NOT NULL, \n\tdesign_digest TEXT NOT NULL, \n\treview_digest TEXT NOT NULL, \n\tdag_digest TEXT NOT NULL, \n\tCONSTRAINT pk_development_stage_plans PRIMARY KEY (plan_id), \n\tCONSTRAINT stage_plan_revision UNIQUE (workflow_id, revision), \n\tCONSTRAINT fk_development_stage_plans_workflow_id FOREIGN KEY(workflow_id) REFERENCES development_workflows (workflow_id)\n)\n\n')
    op.execute("\nCREATE TABLE development_stages (\n\tstage_id TEXT NOT NULL, \n\trevision INTEGER NOT NULL, \n\tplan_id TEXT NOT NULL, \n\tordinal INTEGER NOT NULL, \n\tstate TEXT NOT NULL, \n\tstate_version INTEGER NOT NULL, \n\tsealed_goal TEXT NOT NULL, \n\tdependency_digest TEXT, \n\tbase_sha TEXT, \n\thead_sha TEXT, \n\ttree_sha TEXT, \n\tverification_digest TEXT, \n\treview_digest TEXT, \n\tcommit_digest TEXT, \n\treview_fix_cycle INTEGER NOT NULL, \n\tCONSTRAINT pk_development_stages PRIMARY KEY (stage_id, revision), \n\tCONSTRAINT ck_development_stages_development_stage_state CHECK (state IN ('pending','ready','coding','verifying','reviewing','fixing','commit_ready','committed','blocked','paused','cancelled','invalidated') AND revision >= 1 AND state_version >= 1), \n\tCONSTRAINT fk_development_stages_plan_id FOREIGN KEY(plan_id) REFERENCES development_stage_plans (plan_id)\n)\n\n")
    op.execute('\nCREATE TABLE development_stage_dependencies (\n\tedge_id TEXT NOT NULL, \n\tplan_id TEXT NOT NULL, \n\tupstream_id TEXT NOT NULL, \n\tupstream_revision INTEGER NOT NULL, \n\tdownstream_id TEXT NOT NULL, \n\tdownstream_revision INTEGER NOT NULL, \n\tCONSTRAINT pk_development_stage_dependencies PRIMARY KEY (edge_id), \n\tCONSTRAINT fk_development_stage_dependencies_upstream_id_upstream_revision FOREIGN KEY(upstream_id, upstream_revision) REFERENCES development_stages (stage_id, revision), \n\tCONSTRAINT fk_development_stage_dependencies_downstream_id_downstream_revision FOREIGN KEY(downstream_id, downstream_revision) REFERENCES development_stages (stage_id, revision), \n\tCONSTRAINT stage_dependency_edge UNIQUE (plan_id, upstream_id, downstream_id), \n\tCONSTRAINT fk_development_stage_dependencies_plan_id FOREIGN KEY(plan_id) REFERENCES development_stage_plans (plan_id)\n)\n\n')
    op.execute('\nCREATE TABLE development_stage_dependency_satisfactions (\n\tsatisfaction_id TEXT NOT NULL, \n\tedge_id TEXT NOT NULL, \n\tcommit_digest TEXT NOT NULL, \n\tcommit_sha TEXT NOT NULL, \n\ttree_sha TEXT NOT NULL, \n\tupstream_state_version INTEGER NOT NULL, \n\tobserved_at TEXT NOT NULL, \n\tCONSTRAINT pk_development_stage_dependency_satisfactions PRIMARY KEY (satisfaction_id), \n\tCONSTRAINT uq_development_stage_dependency_satisfactions_edge_id UNIQUE (edge_id), \n\tCONSTRAINT fk_development_stage_dependency_satisfactions_edge_id FOREIGN KEY(edge_id) REFERENCES development_stage_dependencies (edge_id)\n)\n\n')
    op.execute('\nCREATE TABLE development_stage_writers (\n\tworkflow_id TEXT NOT NULL, \n\tstage_id TEXT NOT NULL, \n\tstage_revision INTEGER NOT NULL, \n\tepoch INTEGER NOT NULL, \n\tCONSTRAINT pk_development_stage_writers PRIMARY KEY (workflow_id), \n\tCONSTRAINT fk_development_stage_writers_stage_id_stage_revision FOREIGN KEY(stage_id, stage_revision) REFERENCES development_stages (stage_id, revision), \n\tCONSTRAINT fk_development_stage_writers_workflow_id FOREIGN KEY(workflow_id) REFERENCES development_workflows (workflow_id)\n)\n\n')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM development_stage_writers LIMIT 1')).first():
        raise RuntimeError("Timeline state exists; restore coordinated backup")
    if op.get_bind().execute(sa.text('SELECT 1 FROM development_stage_dependency_satisfactions LIMIT 1')).first():
        raise RuntimeError("Timeline state exists; restore coordinated backup")
    if op.get_bind().execute(sa.text('SELECT 1 FROM development_stage_dependencies LIMIT 1')).first():
        raise RuntimeError("Timeline state exists; restore coordinated backup")
    if op.get_bind().execute(sa.text('SELECT 1 FROM development_stages LIMIT 1')).first():
        raise RuntimeError("Timeline state exists; restore coordinated backup")
    if op.get_bind().execute(sa.text('SELECT 1 FROM development_stage_plans LIMIT 1')).first():
        raise RuntimeError("Timeline state exists; restore coordinated backup")
    if op.get_bind().execute(sa.text('SELECT 1 FROM development_driver_steps LIMIT 1')).first():
        raise RuntimeError("Timeline state exists; restore coordinated backup")
    if op.get_bind().execute(sa.text('SELECT 1 FROM development_execution_gates LIMIT 1')).first():
        raise RuntimeError("Timeline state exists; restore coordinated backup")
    if op.get_bind().execute(sa.text('SELECT 1 FROM development_decision_requests LIMIT 1')).first():
        raise RuntimeError("Timeline state exists; restore coordinated backup")
    if op.get_bind().execute(sa.text('SELECT 1 FROM development_role_snapshots LIMIT 1')).first():
        raise RuntimeError("Timeline state exists; restore coordinated backup")
    if op.get_bind().execute(sa.text('SELECT 1 FROM role_configuration_bindings LIMIT 1')).first():
        raise RuntimeError("Timeline state exists; restore coordinated backup")
    if op.get_bind().execute(sa.text('SELECT 1 FROM role_configuration_revisions LIMIT 1')).first():
        raise RuntimeError("Timeline state exists; restore coordinated backup")
    if op.get_bind().execute(sa.text('SELECT 1 FROM development_artifacts LIMIT 1')).first():
        raise RuntimeError("Timeline state exists; restore coordinated backup")
    op.drop_table('development_stage_writers')
    op.drop_table('development_stage_dependency_satisfactions')
    op.drop_table('development_stage_dependencies')
    op.drop_table('development_stages')
    op.drop_table('development_stage_plans')
    op.drop_table('development_driver_steps')
    op.drop_table('development_execution_gates')
    op.drop_table('development_decision_requests')
    op.drop_table('development_role_snapshots')
    op.drop_table('role_configuration_bindings')
    op.drop_table('role_configuration_revisions')
    op.drop_table('development_artifacts')
