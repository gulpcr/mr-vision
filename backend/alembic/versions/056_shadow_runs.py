"""Shadow (experimental) inference runs, isolated from clinical results.

Revision ID: 056
Revises: 055
Create Date: 2026-10-02

A candidate model (A/B treatment arm, or any model under evaluation) must never replace
the result a radiologist reads. Such runs are now *shadow* jobs:

* ``job_runs.run_mode`` ('clinical' | 'shadow'), plus the experiment and the model version
  the shadow run was asked to use;
* ``results_index.is_shadow``: shadow results are stored for comparison only — never
  ``is_latest``, never in version history, exports, alerts, reports or the worklist.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "056"
down_revision = "055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_runs", sa.Column("run_mode", sa.String(16), nullable=False, server_default="clinical"))
    op.add_column("job_runs", sa.Column("experiment_id", sa.String(36), nullable=True))
    op.add_column("job_runs", sa.Column("model_version_override", sa.String(64), nullable=True))
    op.create_check_constraint("ck_job_runs_run_mode", "job_runs", "run_mode IN ('clinical', 'shadow')")
    op.add_column("results_index", sa.Column("is_shadow", sa.Boolean(), nullable=False, server_default="false"))
    # A shadow result can never be the clinical "latest" one.
    op.create_check_constraint("ck_results_shadow_not_latest", "results_index", "NOT (is_shadow AND is_latest)")
    op.create_index("ix_results_index_shadow", "results_index", ["is_shadow"], postgresql_where=sa.text("is_shadow"))


def downgrade() -> None:
    op.drop_index("ix_results_index_shadow", table_name="results_index")
    op.drop_constraint("ck_results_shadow_not_latest", "results_index", type_="check")
    op.drop_column("results_index", "is_shadow")
    op.drop_constraint("ck_job_runs_run_mode", "job_runs", type_="check")
    op.drop_column("job_runs", "model_version_override")
    op.drop_column("job_runs", "experiment_id")
    op.drop_column("job_runs", "run_mode")
