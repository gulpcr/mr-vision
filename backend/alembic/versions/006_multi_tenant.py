"""Multi-tenant: add tenant_id to studies, job_runs, results_index.

Revision ID: 006
Revises: 005
Create Date: 2026-03-17
"""

from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("studies", sa.Column("tenant_id", sa.String(36), nullable=True, server_default="default"))
    op.add_column("job_runs", sa.Column("tenant_id", sa.String(36), nullable=True, server_default="default"))
    op.add_column("results_index", sa.Column("tenant_id", sa.String(36), nullable=True, server_default="default"))
    op.create_index("ix_studies_tenant_id", "studies", ["tenant_id"])
    op.create_index("ix_job_runs_tenant_id", "job_runs", ["tenant_id"])
    op.create_index("ix_results_tenant_id", "results_index", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_results_tenant_id", "results_index")
    op.drop_index("ix_job_runs_tenant_id", "job_runs")
    op.drop_index("ix_studies_tenant_id", "studies")
    op.drop_column("results_index", "tenant_id")
    op.drop_column("job_runs", "tenant_id")
    op.drop_column("studies", "tenant_id")
