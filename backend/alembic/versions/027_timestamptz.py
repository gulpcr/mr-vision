"""Convert every timestamp column to TIMESTAMP WITH TIME ZONE; add study_date_precision.

Revision ID: 027
Revises: 026
Create Date: 2026-07-29

Why
---
All 41 DateTime columns were ``TIMESTAMP WITHOUT TIME ZONE``. That is unsafe in two
distinct ways:

1. **FHIR conformance.** The R4 ``instant`` datatype *requires* a timezone offset
   (DiagnosticReport.issued, Observation.issued, AuditEvent.recorded,
   Provenance.recorded). A naive value serialises as "2026-07-29T00:00:00" and is
   rejected outright by any validator. See FHIR_R4_COMPLIANCE_AUDIT.md §3.4.
2. **Correctness inside the app.** Because the columns were naive, code had to strip
   tzinfo to talk to them — asyncpg refuses to bind a tz-aware datetime to a naive
   column. That produced two competing conventions in the codebase: some call sites
   wrote aware datetimes (job_orchestrator, portal_service, retention_service,
   active_learning_service) while others deliberately stripped it
   (alerting_service, reading_service, repositories, the stale-job reaper). The
   aware writers only appeared to work; ``RetentionService.apply_policies`` binds an
   aware cutoff against a naive column and fails at runtime. After this migration
   aware is correct everywhere and the strippers are removed.

The UTC assumption
------------------
Stored values are treated as UTC, so the conversion is ``USING col AT TIME ZONE 'UTC'``.
That is a real assumption and it is worth stating why it holds here:

* Python writes were UTC — either ``datetime.now(timezone.utc)`` (with or without a
  ``.replace(tzinfo=None)`` that only drops the label, not the offset) or
  ``datetime.utcnow()``.
* ``server_default=func.now()`` resolves to Postgres ``now()``, a timestamptz cast into
  the naive column using the **session** TimeZone. The postgres:16-alpine service in
  docker-compose.yml sets no TZ/PGTZ, so that is UTC.
* app/application/alerting_service.py documents the same conclusion in a comment:
  "naive TIMESTAMP columns (stored as UTC via func.now())".

VERIFY BEFORE RUNNING ON A DATABASE THAT HAS EVER RUN WITH A NON-UTC SERVER TZ:

    SHOW timezone;
    SELECT created_at, now() AT TIME ZONE 'UTC' FROM studies ORDER BY created_at DESC LIMIT 5;

If stored values are offset from UTC by a fixed amount, change the literal in
``_TZ_SOURCE`` below to that zone (e.g. 'Asia/Karachi') — the conversion is otherwise
silent and shifts every timestamp in the database.

studies.study_date
------------------
Also adds ``studies.study_date_precision``. DICOM StudyDate (0008,0020) and StudyTime
(0008,0030) are separate tags and the ingest path previously read only the date, so
every study_date is midnight — indistinguishable from a study genuinely acquired at
00:00. Existing rows are therefore backfilled to 'date' (the server default) and only
newly ingested studies that carry StudyTime get 'second'.

Additive and type-widening only: no column or table is dropped, and every existing
value is preserved (relabelled, not rewritten).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


# The timezone the existing naive values are understood to be in. See the module
# docstring before changing this — it decides what every stored instant means.
_TZ_SOURCE = "UTC"

# Every DateTime column in app/infrastructure/database/models.py, by table.
# Kept explicit rather than reflected so the migration is deterministic and reviewable.
_TIMESTAMP_COLUMNS: dict[str, tuple[str, ...]] = {
    "studies": (
        "study_date", "assigned_at", "reported_at", "signed_at", "created_at", "updated_at",
    ),
    "series": ("created_at",),
    "job_runs": ("started_at", "completed_at", "created_at", "updated_at"),
    "results_index": ("created_at",),
    "usecase_registry": ("registered_at",),
    "audit_log": ("timestamp",),
    "users": ("created_at", "updated_at"),
    "tenants": ("created_at",),
    "patients": ("created_at",),
    "orders": ("created_at",),
    "roles": ("created_at", "updated_at"),
    "model_versions": ("created_at",),
    "ab_experiments": ("created_at",),
    "ab_assignments": ("created_at",),
    "batch_uploads": ("created_at", "updated_at"),
    "batch_upload_items": ("created_at",),
    "review_queue": ("reviewed_at", "created_at"),
    "alert_rules": ("created_at",),
    "alert_history": ("created_at",),
    "retention_policies": ("created_at",),
    "share_links": ("expires_at", "created_at"),
    "critical_alerts": ("acknowledged_at", "escalated_at", "created_at"),
    "mammography_reports": ("created_at", "updated_at"),
    "mri_reports": ("created_at", "updated_at"),
}


def _existing_tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _existing_columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _assert_source_timezone(bind) -> None:
    """Refuse to run if the database's timezone contradicts ``_TZ_SOURCE``.

    The conversion below relabels every stored timestamp as ``_TZ_SOURCE``. If that is
    wrong, every date in the database shifts by a fixed offset — silently, with no error
    and no way to tell afterwards which values were already correct. That is the kind of
    failure that surfaces weeks later as "the turnaround-time report looks odd", so the
    assumption is checked rather than documented and hoped for.

    ``server_default=func.now()`` resolves to Postgres ``now()`` cast into a naive column
    using the session TimeZone, so the server's zone is what determined what got stored.
    Override by setting ``_TZ_SOURCE`` to the zone the data is actually in.
    """
    try:
        server_tz = bind.execute(sa.text("SHOW timezone")).scalar_one()
    except Exception:
        # Not Postgres (e.g. a SQLite test harness) — nothing to verify.
        return

    normalised = str(server_tz).strip().upper()
    if _TZ_SOURCE.upper() == "UTC" and normalised not in ("UTC", "ETC/UTC", "GMT", "UCT", "Z"):
        raise RuntimeError(
            f"Migration 027 would interpret every existing timestamp as {_TZ_SOURCE}, but "
            f"this database's timezone is '{server_tz}'. Converting under the wrong "
            f"assumption shifts every stored date silently.\n\n"
            f"Check what the data actually contains:\n"
            f"  SELECT created_at, now() AT TIME ZONE 'UTC' FROM studies "
            f"ORDER BY created_at DESC LIMIT 5;\n\n"
            f"Then either set the session/server timezone to UTC, or edit _TZ_SOURCE in "
            f"this migration to '{server_tz}' if the stored values are in that zone."
        )


def upgrade() -> None:
    bind = op.get_bind()
    _assert_source_timezone(bind)
    tables = _existing_tables(bind)

    for table, columns in _TIMESTAMP_COLUMNS.items():
        if table not in tables:
            # Tolerate a database that predates one of the feature migrations.
            continue
        present = _existing_columns(bind, table)
        for column in columns:
            if column not in present:
                continue
            # ALTER ... TYPE timestamptz on a naive column would otherwise interpret
            # values in the *session* timezone; AT TIME ZONE pins it explicitly.
            op.execute(
                f'ALTER TABLE "{table}" '
                f'ALTER COLUMN "{column}" TYPE TIMESTAMP WITH TIME ZONE '
                f"USING \"{column}\" AT TIME ZONE '{_TZ_SOURCE}'"
            )

    if "studies" in tables and "study_date_precision" not in _existing_columns(bind, "studies"):
        op.add_column(
            "studies",
            sa.Column(
                "study_date_precision",
                sa.String(8),
                nullable=False,
                server_default="date",
            ),
        )
        op.create_check_constraint(
            "ck_studies_study_date_precision",
            "studies",
            "study_date_precision IN ('date', 'second')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    if "studies" in tables and "study_date_precision" in _existing_columns(bind, "studies"):
        op.drop_constraint("ck_studies_study_date_precision", "studies", type_="check")
        op.drop_column("studies", "study_date_precision")

    for table, columns in _TIMESTAMP_COLUMNS.items():
        if table not in tables:
            continue
        present = _existing_columns(bind, table)
        for column in columns:
            if column not in present:
                continue
            # Reverses the relabelling exactly: back to naive values in _TZ_SOURCE.
            op.execute(
                f'ALTER TABLE "{table}" '
                f'ALTER COLUMN "{column}" TYPE TIMESTAMP WITHOUT TIME ZONE '
                f"USING \"{column}\" AT TIME ZONE '{_TZ_SOURCE}'"
            )
