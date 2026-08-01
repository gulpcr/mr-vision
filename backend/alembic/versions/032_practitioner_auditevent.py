"""Practitioner / PractitionerRole / Organization identity, and AuditEvent-shaped audit.

Revision ID: 032
Revises: 031
Create Date: 2026-07-29

FHIR_R4_COMPLIANCE_AUDIT.md steps 7 and 8. One migration because both are additive column
work on existing tables plus one join table, and they share a backfill pass.

Step 7 — Practitioner / PractitionerRole / Organization
    ``users`` gains a professional identifier (NPI / licence) and HumanName parts. A login
    account was being used as a clinical actor without the credential that distinguishes
    one clinician from another, and ``full_name`` was a flat string where FHIR requires
    structure.

    ``user_roles`` replaces the single ``users.role`` string as the authoritative set: FHIR
    models a practitioner holding several PractitionerRoles across organizations and
    specialties, bounded by a ``period``. The period matters for audit — to interpret a
    report signed two years ago you need the role its signer held *then*.
    ``users.role`` is kept as a denormalised primary-role cache so ``require_permission``
    and every existing RBAC path are untouched.

    ``tenants`` gains identifier / type / address so it can be emitted as a real
    Organization and act as ``Identifier.assigner``.

Step 8 — AuditEvent
    ``audit_log.actor`` was one untyped String(128) holding four different kinds of value
    depending on the call site ("system", "celery_worker", a user id, or a username), so a
    row could not be resolved to a Practitioner or joined to ``users`` without guessing.
    Typed ``actor_type`` / ``actor_id`` / ``actor_display`` fix that.

    ``action_crude`` records AuditEvent.action, which is a **fixed** five-code set
    (C/R/U/D/E); the existing 20-value domain vocabulary is AuditEvent.subtype and stays
    in ``action``. Splitting them is what makes "every *read* of this record" answerable.

    ``outcome`` / ``source_observer`` / ``client_ip`` complete AuditEvent.agent and .source.

Backfill is conservative: ``actor_id`` is populated only where the legacy ``actor`` value
unambiguously matches a ``users.id`` or a unique ``users.username``. "system" and
"celery_worker" become ``actor_type='system'``. Anything ambiguous is left NULL rather
than guessed — a misattributed audit entry is worse than an unattributed one.

Additive only: no column or table is dropped, ``actor`` is still written by all callers.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


# Domain action → AuditEvent.action (C/R/U/D/E). Anything unlisted stays NULL rather than
# being forced into a bucket it does not belong in.
_ACTION_TO_CRUDE: dict[str, str] = {
    "study_received": "C",
    "job_created": "C",
    "job_started": "E",
    "job_completed": "E",
    "job_failed": "E",
    "job_cancelled": "U",
    "job_retried": "E",
    "result_stored": "C",
    "result_viewed": "R",
    "report_generated": "R",
    "config_changed": "U",
    "user_login": "E",
    "user_logout": "E",
    "user_created": "C",
    "batch_started": "E",
    "batch_completed": "E",
    "review_submitted": "U",
    "alert_triggered": "C",
    "data_purged": "D",
    "phi_deidentified": "U",
    # Written by services rather than the AuditAction enum.
    "patient_created": "C",
    "patient_updated": "U",
    "order_created": "C",
    "order_updated": "U",
    "order_linked_study": "U",
    "study_claimed": "U",
    "study_assigned": "U",
    "study_auto_assigned": "U",
    "study_unclaimed": "U",
    "study_reported": "U",
    "study_signed": "U",
    "mammography_report_saved": "U",
    "role_created": "C",
    "role_updated": "U",
    "role_deleted": "D",
}

_SYSTEM_ACTORS = ("system", "celery_worker", "", None)


def _cols(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    # ── Step 7: Practitioner ──────────────────────────────────────────────────
    if "users" in tables:
        present = _cols(bind, "users")
        for name, col in (
            ("identifier_system", sa.Column("identifier_system", sa.String(128), nullable=True)),
            ("identifier_value", sa.Column("identifier_value", sa.String(128), nullable=True)),
            ("family_name", sa.Column("family_name", sa.String(128), nullable=True)),
            ("given_names", sa.Column("given_names", sa.JSON, nullable=True)),
            ("name_prefix", sa.Column("name_prefix", sa.String(32), nullable=True)),
            ("name_suffix", sa.Column("name_suffix", sa.String(32), nullable=True)),
        ):
            if name not in present:
                op.add_column("users", col)
        if "identifier_value" not in present:
            op.create_index("ix_users_identifier_value", "users", ["identifier_value"])

    # ── Step 7: Organization ──────────────────────────────────────────────────
    if "tenants" in tables:
        present = _cols(bind, "tenants")
        for name, col in (
            ("identifier_system", sa.Column("identifier_system", sa.String(128), nullable=True)),
            ("identifier_value", sa.Column("identifier_value", sa.String(128), nullable=True)),
            ("org_type", sa.Column("org_type", sa.String(32), nullable=True)),
            ("address", sa.Column("address", sa.JSON, nullable=True)),
        ):
            if name not in present:
                op.add_column("tenants", col)

    # ── Step 7: PractitionerRole ──────────────────────────────────────────────
    if "user_roles" not in tables and "users" in tables:
        op.create_table(
            "user_roles",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("role_name", sa.String(64), nullable=False),
            sa.Column(
                "organization_id",
                sa.String(36),
                sa.ForeignKey("tenants.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("role_code_system", sa.String(128), nullable=True),
            sa.Column("role_code", sa.String(64), nullable=True),
            sa.Column("specialty_code_system", sa.String(128), nullable=True),
            sa.Column("specialty_code", sa.String(64), nullable=True),
            sa.Column(
                "period_start",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
            sa.Column("is_primary", sa.Boolean, nullable=False, server_default="false"),
            sa.Column("tenant_id", sa.String(36), nullable=False, server_default="default"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index("ix_user_roles_user_id", "user_roles", ["user_id"])
        op.create_index("ix_user_roles_tenant_id", "user_roles", ["tenant_id"])
        op.create_index(
            "ix_user_roles_user_role", "user_roles", ["user_id", "role_name"], unique=True
        )
        # Seed from the existing single role so the authoritative table starts complete.
        seeded = bind.execute(
            sa.text(
                "INSERT INTO user_roles (id, user_id, role_name, tenant_id, is_primary, "
                "                        organization_id) "
                "SELECT md5(random()::text || u.id), u.id, u.role, "
                "       COALESCE(u.tenant_id, 'default'), true, u.tenant_id "
                "  FROM users u WHERE u.role IS NOT NULL"
            )
        )
        print(f"032: seeded {seeded.rowcount} user_roles row(s) from users.role")

    # ── Step 8: AuditEvent ────────────────────────────────────────────────────
    if "audit_log" in tables:
        present = _cols(bind, "audit_log")
        for name, col in (
            ("actor_type", sa.Column("actor_type", sa.String(16), nullable=True)),
            ("actor_id", sa.Column("actor_id", sa.String(128), nullable=True)),
            ("actor_display", sa.Column("actor_display", sa.String(256), nullable=True)),
            ("action_crude", sa.Column("action_crude", sa.String(1), nullable=True)),
            ("outcome", sa.Column("outcome", sa.String(2), nullable=True)),
            ("source_observer", sa.Column("source_observer", sa.String(128), nullable=True)),
            ("client_ip", sa.Column("client_ip", sa.String(64), nullable=True)),
        ):
            if name not in present:
                op.add_column("audit_log", col)

        if "action_crude" not in present:
            op.create_index("ix_audit_action_crude", "audit_log", ["action_crude"])
            op.create_check_constraint(
                "ck_audit_action_crude",
                "audit_log",
                "action_crude IS NULL OR action_crude IN ('C', 'R', 'U', 'D', 'E')",
            )
        if "actor_id" not in present:
            op.create_index("ix_audit_actor_id", "audit_log", ["actor_id"])
            op.create_index("ix_audit_actor_time", "audit_log", ["actor_id", "timestamp"])

        # Backfill action_crude from the domain vocabulary.
        mapped = 0
        for action, crude in _ACTION_TO_CRUDE.items():
            r = bind.execute(
                sa.text(
                    "UPDATE audit_log SET action_crude = :crude "
                    " WHERE action = :action AND action_crude IS NULL"
                ),
                {"crude": crude, "action": action},
            )
            mapped += r.rowcount or 0

        # Machine actors.
        sysr = bind.execute(
            sa.text(
                "UPDATE audit_log SET actor_type = 'system', actor_display = actor "
                " WHERE actor_type IS NULL AND (actor IS NULL OR actor IN "
                "       ('system', 'celery_worker', ''))"
            )
        )
        # Human actors: only where the legacy value unambiguously identifies one user.
        byid = bind.execute(
            sa.text(
                "UPDATE audit_log a SET actor_type = 'practitioner', actor_id = u.id, "
                "       actor_display = COALESCE(NULLIF(u.full_name, ''), u.username) "
                "  FROM users u WHERE a.actor_type IS NULL AND a.actor = u.id"
            )
        )
        byname = bind.execute(
            sa.text(
                "UPDATE audit_log a SET actor_type = 'practitioner', actor_id = u.id, "
                "       actor_display = COALESCE(NULLIF(u.full_name, ''), u.username) "
                "  FROM users u WHERE a.actor_type IS NULL AND a.actor = u.username "
                "    AND (SELECT COUNT(*) FROM users u2 WHERE u2.username = a.actor) = 1"
            )
        )
        left = bind.execute(
            sa.text("SELECT COUNT(*) FROM audit_log WHERE actor_type IS NULL")
        ).scalar_one()
        print(
            f"032: audit backfill — action_crude={mapped}, system={sysr.rowcount}, "
            f"by-id={byid.rowcount}, by-username={byname.rowcount}, "
            f"unresolved-left-null={left}"
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "audit_log" in tables:
        present = _cols(bind, "audit_log")
        if "action_crude" in present:
            op.drop_constraint("ck_audit_action_crude", "audit_log", type_="check")
            op.drop_index("ix_audit_action_crude", table_name="audit_log")
        if "actor_id" in present:
            op.drop_index("ix_audit_actor_time", table_name="audit_log")
            op.drop_index("ix_audit_actor_id", table_name="audit_log")
        for name in (
            "client_ip", "source_observer", "outcome", "action_crude",
            "actor_display", "actor_id", "actor_type",
        ):
            if name in present:
                op.drop_column("audit_log", name)

    if "user_roles" in tables:
        op.drop_table("user_roles")

    if "tenants" in tables:
        present = _cols(bind, "tenants")
        for name in ("address", "org_type", "identifier_value", "identifier_system"):
            if name in present:
                op.drop_column("tenants", name)

    if "users" in tables:
        present = _cols(bind, "users")
        if "identifier_value" in present:
            op.drop_index("ix_users_identifier_value", table_name="users")
        for name in (
            "name_suffix", "name_prefix", "given_names", "family_name",
            "identifier_value", "identifier_system",
        ):
            if name in present:
                op.drop_column("users", name)
