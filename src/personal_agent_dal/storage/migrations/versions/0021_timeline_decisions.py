"""Decision receipts and project permission references."""
from alembic import op
import sqlalchemy as sa
revision="0021"
down_revision="0020"
branch_labels=None
depends_on=None


def upgrade():
    op.execute('\nCREATE TABLE development_decision_receipts (\n\tcommand_id TEXT NOT NULL, \n\tsource_message_ref TEXT NOT NULL, \n\tdecision_id TEXT NOT NULL, \n\tbody_digest TEXT NOT NULL, \n\tsealed_result TEXT NOT NULL, \n\tCONSTRAINT pk_development_decision_receipts PRIMARY KEY (command_id), \n\tCONSTRAINT uq_development_decision_receipts_source_message_ref UNIQUE (source_message_ref), \n\tCONSTRAINT fk_development_decision_receipts_decision_id FOREIGN KEY(decision_id) REFERENCES development_decision_requests (decision_id)\n)\n\n')
    op.execute('\nCREATE TABLE development_project_authorizations (\n\tgrant_id TEXT NOT NULL, \n\tversion INTEGER NOT NULL, \n\tproject_id TEXT NOT NULL, \n\tsubject TEXT NOT NULL, \n\tsealed_grant TEXT NOT NULL, \n\tdigest TEXT NOT NULL, \n\texpires_at TEXT NOT NULL, \n\trevoked INTEGER NOT NULL, \n\tCONSTRAINT pk_development_project_authorizations PRIMARY KEY (grant_id)\n)\n\n')
    op.execute('\nCREATE TABLE development_project_bindings (\n\tworkflow_id TEXT NOT NULL, \n\tproject_id TEXT NOT NULL, \n\tgrant_id TEXT NOT NULL, \n\tgrant_version INTEGER NOT NULL, \n\troute_artifact_id TEXT NOT NULL, \n\tcandidate_digest TEXT NOT NULL, \n\tsealed_binding TEXT NOT NULL, \n\tCONSTRAINT pk_development_project_bindings PRIMARY KEY (workflow_id), \n\tCONSTRAINT fk_development_project_bindings_workflow_id FOREIGN KEY(workflow_id) REFERENCES development_workflows (workflow_id), \n\tCONSTRAINT fk_development_project_bindings_grant_id FOREIGN KEY(grant_id) REFERENCES development_project_authorizations (grant_id), \n\tCONSTRAINT fk_development_project_bindings_route_artifact_id FOREIGN KEY(route_artifact_id) REFERENCES development_artifacts (artifact_id)\n)\n\n')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM development_project_bindings LIMIT 1')).first():
        raise RuntimeError("Timeline decisions exist; preserve authority")
    if op.get_bind().execute(sa.text('SELECT 1 FROM development_project_authorizations LIMIT 1')).first():
        raise RuntimeError("Timeline decisions exist; preserve authority")
    if op.get_bind().execute(sa.text('SELECT 1 FROM development_decision_receipts LIMIT 1')).first():
        raise RuntimeError("Timeline decisions exist; preserve authority")
    op.drop_table('development_project_bindings')
    op.drop_table('development_project_authorizations')
    op.drop_table('development_decision_receipts')
