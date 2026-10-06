#!/usr/bin/env bash
# Restore from the encrypted backups written by ops/backup.sh — or prove that you could.
#
#   sudo ops/restore.sh --drill --identity /media/usb/backup-age.key [--date YYYY/MM/DD]
#   sudo ops/restore.sh --full  --identity /media/usb/backup-age.key [--date YYYY/MM/DD]
#
#   --identity FILE  the OFFLINE age private key matching BACKUP_AGE_RECIPIENT (ops/KEYS.md).
#                    Bring it to the host only for the restore; it is never copied.
#   --date           backup day (default: the latest one on the remote)
#
# --drill (monthly, HIPAA 164.308(a)(7)(ii)(D) testing of the contingency plan): decrypts
#   the dumps, restores them into scratch databases restore_drill_*, prints row counts of
#   key tables next to the live ones, verifies the encrypted object copies against MinIO
#   (rclone cryptcheck), then drops the scratch databases. Touches nothing live.
# --full (disaster recovery onto a freshly deployed, EMPTY stack: ops/deploy/up.sh has run,
#   backend/worker/beat/orthanc are stopped): restores both databases in place and syncs
#   every object back into MinIO. Refuses if the platform DB already has studies.
set -Eeuo pipefail
shopt -s inherit_errexit
umask 077

MRCV_DIR="${MRCV_DIR:-/opt/mrcv}"
ENV_FILE="${MRCV_ENV_FILE:-/run/mrcv/.env}"
WORK_ROOT="${BACKUP_WORK_DIR:-/srv/mrcv/backups}"
MODE=""; IDENTITY=""; DAY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --drill) MODE=drill; shift ;;
    --full) MODE=full; shift ;;
    --identity) IDENTITY="$2"; shift 2 ;;
    --date) DAY="$2"; shift 2 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
log() { printf '%s mrcv-restore: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }
[ -n "$MODE" ] || die "choose --drill or --full"
[ -r "$IDENTITY" ] || die "--identity FILE (the offline backup age key) is required"
[ -r "$ENV_FILE" ] || die "env file $ENV_FILE not readable (run ops/deploy/up.sh first)"

# shellcheck source=lib/backup-common.sh
. "$MRCV_DIR/ops/lib/backup-common.sh"
backup_settings
install -d -m 0700 "$WORK_ROOT"
WORK="$(mktemp -d "$WORK_ROOT/restore.XXXXXX")"
trap 'rm -rf -- "$WORK" "${RCLONE_ENV:-}"' EXIT
if [ "$MODE" = full ]; then
  # The read-only backup user cannot write objects back.
  RESTORE_MINIO_USER="$(envval MINIO_ACCESS_KEY)"
  RESTORE_MINIO_PASSWORD="$(envval MINIO_SECRET_KEY)"
fi
setup_rclone

if [ -z "$DAY" ]; then
  DAY="$(rclone lsf "${REMOTE}:${DEST_PATH}/db" --dirs-only -R --max-depth 3 \
    | grep -E '^[0-9]{4}/[0-9]{2}/[0-9]{2}/$' | sort | tail -1 | tr -d '/' \
    | sed -E 's#^([0-9]{4})([0-9]{2})([0-9]{2})$#\1/\2/\3#')"
  [ -n "$DAY" ] || die "no backups found under ${REMOTE}:${DEST_PATH}/db"
fi
log "using backup of $DAY"
rclone copy "${REMOTE}:${DEST_PATH}/db/${DAY}" /work/in
# Several runs on one day: use the newest run (its checksum file names its dumps).
sums="$(find "$WORK/in" -name 'SHA256SUMS_*' | sort | tail -1)"
[ -n "$sums" ] || die "no checksum file in the backup of $DAY"
RUN_TS="${sums##*SHA256SUMS_}"
( cd "$WORK/in" && sha256sum -c --quiet "SHA256SUMS_${RUN_TS}" ) || die "checksum mismatch in the downloaded backup"
log "run $RUN_TS: checksums verified"

decrypt() {  # decrypt <name>: plaintext dump to $WORK/<name>.dump (on the encrypted volume)
  local src="$WORK/in/${1}_${RUN_TS}.dump.age"; [ -f "$src" ] || die "no dump of $1 in run $RUN_TS"
  age -d -i "$IDENTITY" -o "$WORK/$1.dump" "$src" || die "cannot decrypt $1 (wrong --identity?)"
}
psql_() { compose exec -T postgres sh -c "exec psql -v ON_ERROR_STOP=1 -U \"\$POSTGRES_USER\" -d '$1' -tAqc \"$2\""; }
restore_into() {  # restore_into <dump> <database>
  compose exec -T postgres sh -c "exec pg_restore -U \"\$POSTGRES_USER\" -d '$2' --no-owner --role=\"\$POSTGRES_USER\" --exit-on-error" < "$1"
}
# app.platform=on: see every tenant's rows (the owner role is subject to FORCE RLS).
COUNT_SQL="SET app.platform = 'on'; SELECT (SELECT count(*) FROM studies)||' studies, '||(SELECT count(*) FROM results_index)||' results, '||(SELECT count(*) FROM users)||' users, '||(SELECT count(*) FROM audit_log)||' audit rows'"

if [ "$MODE" = drill ]; then
  for name in "$DB_NAME" orthanc; do
    scratch="restore_drill_$(echo "$name" | tr -c 'a-z0-9_\n' '_')"
    decrypt "$name"; dump="$WORK/$name.dump"
    psql_ postgres "DROP DATABASE IF EXISTS $scratch" >/dev/null
    psql_ postgres "CREATE DATABASE $scratch" >/dev/null
    log "restoring $name into scratch database $scratch"
    restore_into "$dump" "$scratch"
    rm -f "$dump"
    if [ "$name" = "$DB_NAME" ]; then
      log "  backup: $(psql_ "$scratch" "$COUNT_SQL")"
      log "  live:   $(psql_ "$DB_NAME" "$COUNT_SQL")"
    else
      log "  backup: $(psql_ "$scratch" "SELECT count(*) FROM resources") Orthanc index resources"
    fi
    psql_ postgres "DROP DATABASE $scratch" >/dev/null
  done
  for bucket in "$ARTIFACT_BUCKET" "$DICOM_BUCKET"; do
    log "verifying encrypted copies of bucket $bucket"
    rclone cryptcheck "mrcvminio:${bucket}" "mrcvcrypt:${bucket}" --one-way
  done
  log "restore drill PASSED for $DAY — record it in the contingency-plan test log"
  exit 0
fi

# ── full restore ─────────────────────────────────────────────────────────────
existing="$(psql_ "$DB_NAME" "SELECT count(*) FROM studies" 2>/dev/null || echo 0)"
[ "${existing:-0}" = 0 ] || die "the platform database already holds $existing studies; --full restores only onto an empty stack"
for svc in backend worker beat orthanc; do
  [ -z "$(compose ps -q --status running "$svc")" ] || die "stop $svc first: docker compose ... stop backend worker beat orthanc"
done
ensure_app_role
for name in "$DB_NAME" orthanc; do
  decrypt "$name"; dump="$WORK/$name.dump"
  log "restoring $name"
  restore_into "$dump" "$name"
  rm -f "$dump"
done
for bucket in "$ARTIFACT_BUCKET" "$DICOM_BUCKET"; do
  log "restoring objects of bucket $bucket"
  rclone sync "mrcvcrypt:${bucket}" "mrcvminio:${bucket}" --transfers 8 --checkers 16 --stats-one-line
done
log "restore complete. Start the stack (ops/deploy/up.sh), then check /health and open a study."
