"""Frozen project templates and versioned phone authorization proposals."""
from alembic import op
import sqlalchemy as sa
revision='0023'
down_revision='0022'
branch_labels=None
depends_on=None

def upgrade():
    op.execute('\nCREATE TABLE development_project_templates (\n\ttemplate_id TEXT NOT NULL, \n\tproject_id TEXT NOT NULL, \n\trevision INTEGER NOT NULL, \n\tactive INTEGER NOT NULL, \n\tdigest TEXT NOT NULL, \n\tsealed_template TEXT NOT NULL, \n\tobserved_at TEXT NOT NULL, \n\texpires_at TEXT NOT NULL, \n\tevidence_digest TEXT NOT NULL, \n\tactor TEXT NOT NULL, \n\tCONSTRAINT pk_development_project_templates PRIMARY KEY (template_id), \n\tCONSTRAINT project_template_revision UNIQUE (project_id, revision), \n\tCONSTRAINT ck_development_project_templates_project_template_state CHECK (revision >= 1 AND active IN (0,1))\n)\n\n')
    op.execute("\nCREATE TABLE development_authorization_proposals (\n\tproposal_id TEXT NOT NULL, \n\tworkflow_id TEXT NOT NULL, \n\ttemplate_id TEXT NOT NULL, \n\tgeneration INTEGER NOT NULL, \n\tstatus TEXT NOT NULL, \n\tbinding_digest TEXT NOT NULL, \n\tsealed_proposal TEXT NOT NULL, \n\texpires_at TEXT NOT NULL, \n\tCONSTRAINT pk_development_authorization_proposals PRIMARY KEY (proposal_id), \n\tCONSTRAINT authorization_generation UNIQUE (workflow_id, generation), \n\tCONSTRAINT ck_development_authorization_proposals_authorization_proposal_state CHECK (generation >= 1 AND status IN ('pending','granted','superseded','expired')), \n\tCONSTRAINT fk_development_authorization_proposals_workflow_id FOREIGN KEY(workflow_id) REFERENCES development_workflows (workflow_id), \n\tCONSTRAINT fk_development_authorization_proposals_template_id FOREIGN KEY(template_id) REFERENCES development_project_templates (template_id)\n)\n\n")
    op.execute('\nCREATE TABLE development_authorization_policies (\n\tgrant_id TEXT NOT NULL, \n\tworkflow_id TEXT NOT NULL, \n\ttemplate_id TEXT NOT NULL, \n\ttemplate_digest TEXT NOT NULL, \n\tproposal_id TEXT NOT NULL, \n\tCONSTRAINT pk_development_authorization_policies PRIMARY KEY (grant_id), \n\tCONSTRAINT fk_development_authorization_policies_grant_id FOREIGN KEY(grant_id) REFERENCES development_project_authorizations (grant_id), \n\tCONSTRAINT uq_development_authorization_policies_workflow_id UNIQUE (workflow_id), \n\tCONSTRAINT fk_development_authorization_policies_workflow_id FOREIGN KEY(workflow_id) REFERENCES development_workflows (workflow_id), \n\tCONSTRAINT fk_development_authorization_policies_template_id FOREIGN KEY(template_id) REFERENCES development_project_templates (template_id), \n\tCONSTRAINT fk_development_authorization_policies_proposal_id FOREIGN KEY(proposal_id) REFERENCES development_authorization_proposals (proposal_id)\n)\n\n')
    op.create_index('uq_active_project_template','development_project_templates',['project_id'],unique=True,sqlite_where=sa.text('active = 1'))
    op.add_column('development_authorization_requests',sa.Column('generation',sa.Integer(),nullable=False,server_default='0'))
    op.add_column('development_authorization_requests',sa.Column('current_proposal_id',sa.Text()))

def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM development_authorization_proposals')).scalar_one():
        raise RuntimeError('Authorization history exists; reconcile and use the reviewed backup rollback plan')
    op.drop_column('development_authorization_requests','current_proposal_id')
    op.drop_column('development_authorization_requests','generation')
    op.drop_table('development_authorization_policies')
    op.drop_table('development_authorization_proposals')
    op.drop_table('development_project_templates')
