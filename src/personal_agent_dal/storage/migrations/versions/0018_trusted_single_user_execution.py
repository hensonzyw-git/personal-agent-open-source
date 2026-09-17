"""Explicit single-user execution authority; historical rows remain legacy."""
from alembic import op
import sqlalchemy as sa
from personal_agent_core.sqlite import UtcTimestamp

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None

ACTION_FIELDS = ('execution_contract_version', 'execution_role', 'execution_input_body',
                 'completion_mode', 'completion_policy_revision')
COMPLETE = """(execution_contract_version IS NULL AND execution_role IS NULL AND
execution_input_body IS NULL AND completion_mode IS NULL AND completion_policy_revision IS NULL)
OR (execution_contract_version IS NOT NULL AND execution_contract_version = 'dal.action-execution/1.0'
AND kind = 'provider' AND execution_role IS NOT NULL AND execution_role IN ('planner','coder','reviewer')
AND execution_input_body IS NOT NULL AND length(execution_input_body) > 0
AND completion_mode IS NOT NULL AND ((completion_mode = 'report_only' AND completion_policy_revision IS NULL)
OR (completion_mode = 'feature_transition' AND completion_policy_revision IS NOT NULL
AND length(completion_policy_revision) > 0 AND stage_id IS NULL)))"""


def ref(name, target, **kw):
    return sa.Column(name, sa.Text(), sa.ForeignKey(target, ondelete='RESTRICT'), **kw)


def sha(name, constraint_name):
    return sa.CheckConstraint(f"(length({name}) = 64 AND {name} NOT GLOB '*[^0-9a-f]*')", name=constraint_name)


def upgrade():
    with op.batch_alter_table('workflow_actions') as batch:
        for name in ACTION_FIELDS:
            batch.add_column(sa.Column(name, sa.Text(), nullable=True))
        batch.create_check_constraint('execution_contract_complete', COMPLETE)
    with op.batch_alter_table('workflow_selections') as batch:
        batch.add_column(sa.Column('action_id', sa.Text(), nullable=True))
        batch.create_foreign_key('fk_selection_action', 'workflow_actions', ['action_id'], ['action_id'], ondelete='RESTRICT')
    with op.batch_alter_table('worker_jobs') as batch:
        batch.add_column(sa.Column('execution_mode', sa.Text(), nullable=False, server_default='legacy_unclassified'))
        batch.create_check_constraint('execution_mode', "execution_mode IN ('provider_v1','legacy_non_provider','legacy_unclassified')")
    with op.batch_alter_table('provider_attempts') as batch:
        batch.add_column(sa.Column('report_receipt_id', sa.Text(), nullable=True))
        batch.create_foreign_key('fk_attempt_report_receipt', 'worker_result_receipts', ['report_receipt_id'], ['receipt_id'], ondelete='RESTRICT')
        batch.create_unique_constraint('uq_attempt_report_receipt', ['report_receipt_id'])
    op.create_table('execution_job_bindings',
        ref('attempt_id','provider_attempts.attempt_id',primary_key=True),
        ref('job_id','worker_jobs.job_id',nullable=False,unique=True),
        ref('selection_id','workflow_selections.selection_id',nullable=False),
        ref('snapshot_sha256','execution_snapshots.sha256',nullable=False),
        sa.Column('input_binding_sha256',sa.Text(),nullable=False),
        sa.Column('origin',sa.Text(),nullable=False),
        sa.Column('created_at',UtcTimestamp(),nullable=False),
        sha('input_binding_sha256', 'input_digest'), sa.CheckConstraint("origin IN ('initial','replacement')",name='origin'))
    op.create_table('execution_start_receipts',
        sa.Column('request_id',sa.Text(),primary_key=True),
        sa.Column('request_sha256',sa.Text(),nullable=False),
        sa.Column('actor',sa.Text(),nullable=False),
        ref('action_id','workflow_actions.action_id',nullable=False,unique=True),
        ref('selection_id','workflow_selections.selection_id',nullable=False),
        sa.Column('binding_sha256',sa.Text(),nullable=False),
        sa.Column('expires_at',UtcTimestamp(),nullable=False),
        ref('attempt_id','provider_attempts.attempt_id',nullable=False,unique=True),
        sa.Column('recorded_at',UtcTimestamp(),nullable=False),
        sha('request_sha256', 'request_digest'),sha('binding_sha256', 'binding_digest'),
        sa.CheckConstraint('length(actor) > 0 AND expires_at > recorded_at',name='authority'))
    op.create_table('execution_policy_lease_issuances',
        ref('attempt_id','provider_attempts.attempt_id',primary_key=True),
        sa.Column('job_lease_epoch',sa.Integer(),primary_key=True),
        ref('lease_id','leases.lease_id',nullable=False,unique=True),
        sa.CheckConstraint('job_lease_epoch >= 1',name='positive_epoch'))
    op.create_table('execution_result_envelopes',
        ref('attempt_id','provider_attempts.attempt_id',primary_key=True),
        sa.Column('result_sha256',sa.Text(),primary_key=True),
        sa.Column('body',sa.Text(),nullable=False),
        sa.Column('recorded_at',UtcTimestamp(),nullable=False),
        sha('result_sha256', 'result_digest'),sa.CheckConstraint('length(body) > 0',name='body_nonempty'))


def downgrade():
    for table in ('execution_job_bindings','execution_start_receipts','execution_policy_lease_issuances','execution_result_envelopes'):
        if op.get_bind().execute(sa.text(f'SELECT 1 FROM {table} LIMIT 1')).first():
            raise RuntimeError('execution authority exists; restore coordinated backup')
    for table, predicate in (('workflow_actions','execution_contract_version IS NOT NULL'),
                             ('workflow_selections','action_id IS NOT NULL'),
                             ('worker_jobs',"execution_mode <> 'legacy_unclassified'"),
                             ('provider_attempts','report_receipt_id IS NOT NULL')):
        if op.get_bind().execute(sa.text(f'SELECT 1 FROM {table} WHERE {predicate} LIMIT 1')).first():
            raise RuntimeError('execution authority exists; restore coordinated backup')
    for table in ('execution_result_envelopes','execution_policy_lease_issuances','execution_start_receipts','execution_job_bindings'):
        op.drop_table(table)
    with op.batch_alter_table('provider_attempts') as batch:
        batch.drop_constraint('fk_attempt_report_receipt',type_='foreignkey')
        batch.drop_constraint('uq_attempt_report_receipt',type_='unique')
        batch.drop_column('report_receipt_id')
    with op.batch_alter_table('worker_jobs') as batch:
        batch.drop_constraint('execution_mode',type_='check')
        batch.drop_column('execution_mode')
    with op.batch_alter_table('workflow_selections') as batch:
        batch.drop_constraint('fk_selection_action',type_='foreignkey')
        batch.drop_column('action_id')
    with op.batch_alter_table('workflow_actions') as batch:
        batch.drop_constraint('execution_contract_complete',type_='check')
        for name in ACTION_FIELDS: batch.drop_column(name)
