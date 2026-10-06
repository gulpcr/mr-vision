#!/usr/bin/env bash
# Evidence pack for an OCR inquiry / audit (checklist ADM-07, "Evidence Ready").
#
#   sudo ops/compliance/evidence-pack.sh [--days 365]
#
# Collects, into one age-encrypted archive (to BACKUP_AGE_RECIPIENT — it holds user names
# and client IPs, no patient identifiers):
#   system/     git commit, image digests, migration level, rendered-free configs
#               (compose files, nginx templates, Postgres/Redis settings - never .env)
#   audit/      hash-chain verification, audit-action counts, audit reviews and who
#               signed them off, WORM archive exports, break-glass grants (no MRN)
#   access/     every account: role, status, MFA enrolment, last sign-in
#   contracts/  tenant BAA register (counterparty, dates, document SHA-256)
#   rights/     patient-request register statistics (on-time / overdue) and restriction
#               counts per channel - counts only, no MRNs
#   ai/         AI QA metrics (edit distance, rejection rate) per use case
#   scans/      TLS (testssl), CI security scans (Trivy/bandit/semgrep/pip-audit/npm),
#               restore and PITR drill results - copied from EVIDENCE_DIR
#   policies/   SHA-256 + version history of every policy in COMPLIANCE_DIR
#   MANIFEST.sha256
# Output: ${EVIDENCE_DIR:-/srv/mrcv/evidence}/packs/evidence-<UTC stamp>.tar.gz.age
set -Eeuo pipefail
shopt -s inherit_errexit

MRCV_DIR="${MRCV_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
ENV_FILE="${MRCV_ENV_FILE:-/run/mrcv/.env}"
EVIDENCE_DIR="${EVIDENCE_DIR:-/srv/mrcv/evidence}"
COMPLIANCE_DIR="${COMPLIANCE_DIR:-/srv/mrcv/compliance}"
DAYS=365
while [ $# -gt 0 ]; do
  case "$1" in
    --days) DAYS="$2"; shift 2 ;;
    *) echo "usage: $0 [--days N]" >&2; exit 2 ;;
  esac
done
case "$DAYS" in ''|*[!0-9]*) echo "--days must be a number" >&2; exit 2 ;; esac

envval() { { grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | tail -1 | cut -d= -f2-; }
RECIPIENT="$(envval BACKUP_AGE_RECIPIENT)"
[ -n "$RECIPIENT" ] || { echo "BACKUP_AGE_RECIPIENT is not set (the pack is always encrypted)" >&2; exit 1; }
command -v age >/dev/null || { echo "age is not installed" >&2; exit 1; }
DB_NAME="$(envval POSTGRES_DB)"; DB_NAME="${DB_NAME:-mri_platform}"
DB_USER="$(envval POSTGRES_USER)"; DB_USER="${DB_USER:-mri_admin}"

COMPOSE_FILES=(-f "$MRCV_DIR/docker-compose.yml" -f "$MRCV_DIR/docker-compose.prod.yml")
for overlay in $(envval MRCV_COMPOSE_OVERLAYS | tr ',' ' '); do
  COMPOSE_FILES+=(-f "$MRCV_DIR/$overlay")
done
compose() { docker compose --project-directory "$MRCV_DIR" --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" "$@"; }

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
umask 077
WORK="$(mktemp -d "${TMPDIR:-/tmp}/evidence.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
P="$WORK/evidence-$stamp"
mkdir -p "$P"/{system,audit,access,contracts,rights,ai,scans,policies}
log() { echo "evidence-pack: $*"; }

# SQL as CSV, run inside the Postgres container (password via the environment only).
sql_csv() {
  local out="$1" query="$2"
  PGPASSWORD="$(envval POSTGRES_PASSWORD)" compose exec -T -e PGPASSWORD postgres \
    psql -h localhost -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -qAt \
    -c "COPY ($query) TO STDOUT WITH CSV HEADER" > "$out" \
    || { echo "query failed: ${out##*/}" > "$out.error"; rm -f "$out"; log "WARN ${out##*/} failed"; }
}
since="now() - interval '$DAYS days'"

# ── system ────────────────────────────────────────────────────────────────────
log "system"
{
  echo "generated_at=$(date -u +%FT%TZ)"
  echo "host=$(hostname)"
  echo "period_days=$DAYS"
  echo "git_commit=$(git -C "$MRCV_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "git_dirty_files=$(git -C "$MRCV_DIR" status --porcelain 2>/dev/null | wc -l)"
  echo "kernel=$(uname -r)"
  echo "fips_enabled=$(cat /proc/sys/crypto/fips_enabled 2>/dev/null || echo 0)"
} > "$P/system/host.txt"
compose ps --format '{{.Service}}\t{{.Image}}\t{{.Status}}' > "$P/system/services.tsv" || true
for c in $(compose ps -q); do
  docker inspect --format '{{.Name}}	{{.Config.Image}}	{{index .RepoDigests 0}}{{.Image}}' "$c" 2>/dev/null || true
done > "$P/system/image-digests.tsv"
sql_csv "$P/system/migration.csv" "SELECT version_num FROM alembic_version"
for f in docker-compose.yml docker-compose.prod.yml nginx/templates postgres redis ops/siem/vector.toml \
         minio/init.sh orthanc/orthanc.json .github/workflows; do
  [ -e "$MRCV_DIR/$f" ] && cp -r "$MRCV_DIR/$f" "$P/system/" 2>/dev/null || true
done
# Never ship key material or env files, even if one was left in a copied directory.
find "$P/system" \( -name '*.key' -o -name '*.pem' -o -name '.env*' -o -name '*.age' \) -delete

# ── audit ─────────────────────────────────────────────────────────────────────
log "audit"
chain_failed() {
  rm -f "$P/audit/chain-verification.json"
  echo "chain verification could not run (is the backend service up?)" > "$P/audit/chain-verification.error"
  log "WARN chain verification failed"
}
compose exec -T backend python - > "$P/audit/chain-verification.json" <<'PY' || chain_failed
import asyncio, json
from app.application.audit_integrity_service import AuditIntegrityService
from app.infrastructure.database.session import async_session_factory
from app.infrastructure.tenant.db_scope import platform_scope

async def main():
    with platform_scope():
        async with async_session_factory() as s:
            return await AuditIntegrityService(s).verify_chain()

print(json.dumps(asyncio.run(main()), indent=2, default=str))
PY
sql_csv "$P/audit/action-counts.csv" \
  "SELECT tenant_id, action, outcome, count(*) AS n, min(timestamp) AS first, max(timestamp) AS last
   FROM audit_log WHERE timestamp >= $since GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC"
sql_csv "$P/audit/audit-reviews.csv" \
  "SELECT tenant_id, period_start, period_end, findings_count, generated_at, reviewed_by_username,
          reviewed_at, review_notes IS NOT NULL AS has_notes
   FROM audit_reviews WHERE period_start >= $since ORDER BY 1, 2"
sql_csv "$P/audit/worm-archive-exports.csv" \
  "SELECT timestamp, entity_id AS archive_prefix, details->>'rows' AS rows,
          details->>'sha256' AS log_sha256, details->>'retain_until' AS retain_until,
          details->>'chain_valid' AS chain_valid
   FROM audit_log WHERE action = 'audit_archive_exported' ORDER BY timestamp"
sql_csv "$P/audit/break-glass.csv" \
  "SELECT tenant_id, username, created_at, expires_at, revoked_at, revoked_by, client_ip,
          length(reason) AS reason_chars
   FROM break_glass_grants WHERE created_at >= $since ORDER BY created_at"
sql_csv "$P/audit/security-events.csv" \
  "SELECT tenant_id, timestamp, action, actor_display, client_ip FROM audit_log
   WHERE timestamp >= $since AND action IN ('account_locked', 'session_reuse_detected',
     'artifact_integrity_violation', 'signed_document_integrity_failed', 'impersonation_started',
     'disclosure_blocked_by_restriction') ORDER BY timestamp"

# ── access ────────────────────────────────────────────────────────────────────
log "access"
sql_csv "$P/access/accounts.csv" \
  "SELECT tenant_id, username, role, status, is_active, is_platform_admin, is_platform_operator,
          totp_enabled AS mfa_enrolled, must_change_password, password_changed_at, last_login_at,
          created_at, updated_at
   FROM users ORDER BY tenant_id, username"
sql_csv "$P/access/role-changes.csv" \
  "SELECT tenant_id, timestamp, actor_display, details->>'target_username' AS username,
          details->>'previous_role' AS previous_role, details->>'role' AS new_role
   FROM audit_log WHERE action = 'user_role_changed' AND timestamp >= $since
   ORDER BY timestamp"

# ── contracts / rights / ai ──────────────────────────────────────────────────
log "contracts, rights, ai"
sql_csv "$P/contracts/tenant-baas.csv" \
  "SELECT t.id AS tenant_id, t.status AS tenant_status, b.counterparty, b.signatory_name,
          b.signatory_title, b.signed_on, b.effective_from, b.expires_on, b.document_name,
          b.document_sha256, b.recorded_by, b.terminated_at
   FROM tenants t LEFT JOIN tenant_baas b ON b.tenant_id = t.id ORDER BY 1, b.effective_from"
sql_csv "$P/rights/requests-summary.csv" \
  "SELECT tenant_id, request_type, status, count(*) AS n,
          count(*) FILTER (WHERE closed_at IS NOT NULL AND closed_at <= due_at) AS closed_on_time,
          count(*) FILTER (WHERE status IN ('open', 'extended') AND due_at < now()) AS overdue_now,
          count(*) FILTER (WHERE extended_at IS NOT NULL) AS extended
   FROM patient_requests WHERE received_at >= $since GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
sql_csv "$P/rights/restrictions-summary.csv" \
  "SELECT tenant_id, channel, count(*) FILTER (WHERE revoked_at IS NULL
            AND (expires_at IS NULL OR expires_at > now())) AS active, count(*) AS total
   FROM patient_restrictions GROUP BY 1, 2 ORDER BY 1, 2"
sql_csv "$P/ai/report-review-metrics.csv" \
  "SELECT tenant_id, usecase_name, modality, date_trunc('month', created_at) AS month, count(*) AS n,
          round(avg(similarity)::numeric, 4) AS mean_similarity,
          round(avg(edit_distance)::numeric, 1) AS mean_edit_distance,
          count(*) FILTER (WHERE outcome = 'rejected') AS rejected,
          sum(slots_mismatched) AS slot_discrepancies
   FROM ai_report_reviews WHERE created_at >= $since GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 4"

# ── scans and drills ──────────────────────────────────────────────────────────
log "scans"
for d in tls ci restore pitr backup; do
  [ -d "$EVIDENCE_DIR/$d" ] && cp -r "$EVIDENCE_DIR/$d" "$P/scans/" || true
done
ws="$(compose exec -T wal-shipper cat /wal-archive/.last-ship 2>/dev/null || true)"
[ -n "$ws" ] && echo "wal_last_shipped_epoch=$ws age_seconds=$(( $(date -u +%s) - ws ))" > "$P/scans/wal-shipping.txt"
[ -n "$(ls -A "$P/scans")" ] || echo "no scan or drill reports found in $EVIDENCE_DIR" > "$P/scans/MISSING.txt"

# ── policies ──────────────────────────────────────────────────────────────────
log "policies"
if [ -d "$COMPLIANCE_DIR" ]; then
  (cd "$COMPLIANCE_DIR" && find . -type f \( -name '*.md' -o -name '*.pdf' -o -name '*.csv' \) \
     -not -path './.git/*' -print0 | sort -z | xargs -0 -r sha256sum) > "$P/policies/sha256.txt"
  git -C "$COMPLIANCE_DIR" log --date=iso-strict --format='%H %ad %an %s' -- . \
    > "$P/policies/history.txt" 2>/dev/null || echo "not a git repository" > "$P/policies/history.txt"
  cp "$COMPLIANCE_DIR"/*.md "$P/policies/" 2>/dev/null || true
  [ -d "$COMPLIANCE_DIR/policies" ] && cp -r "$COMPLIANCE_DIR/policies" "$P/policies/documents" || true
else
  echo "COMPLIANCE_DIR $COMPLIANCE_DIR not found" > "$P/policies/MISSING.txt"
fi

# ── seal ──────────────────────────────────────────────────────────────────────
(cd "$P" && find . -type f ! -name MANIFEST.sha256 -print0 | sort -z | xargs -0 sha256sum) > "$P/MANIFEST.sha256"
install -d -m 0750 "$EVIDENCE_DIR/packs"
out="$EVIDENCE_DIR/packs/evidence-$stamp.tar.gz.age"
tar -C "$WORK" -czf - "evidence-$stamp" | age -r "$RECIPIENT" -o "$out"
sha256sum "$out" > "$out.sha256"
missing="$(find "$P" -name '*.error' -o -name 'MISSING.txt' | sed "s|$P/||" | tr '\n' ' ')"
log "wrote $out ($(du -h "$out" | cut -f1))"
[ -z "$missing" ] || log "incomplete sections: $missing"
