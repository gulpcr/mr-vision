#!/usr/bin/env bash
# Nightly encrypted backup to any storage provider (S3, Azure Blob, GCS, Backblaze B2,
# SFTP ... anything rclone speaks). The provider only ever receives ciphertext.
#
#   sudo ops/backup.sh            (ops/systemd/mrcv-backup.timer runs it at 02:30)
#
#   * databases: pg_dump (custom format) of the platform DB and of the Orthanc index DB,
#     validated with pg_restore --list, then age-encrypted to BACKUP_AGE_RECIPIENT — a
#     PUBLIC key; the matching private key is kept offline (ops/KEYS.md), so this host
#     cannot decrypt its own backups and a stolen host or remote yields nothing;
#   * configuration: /etc/mrcv/mrcv.env, the (already SOPS-encrypted) secrets file and
#     the internal PKI, as one age-encrypted tarball;
#   * objects (Orthanc DICOM + AI artifacts in MinIO): incremental rclone sync through an
#     rclone "crypt" remote (file contents and names encrypted, BACKUP_CRYPT_PASSWORD);
#     objects deleted or changed locally are kept under _versions/<date>/ remotely.
#
# Settings come from the stack's tmpfs env file (/run/mrcv/.env, written by up.sh):
#   BACKUP_AGE_RECIPIENT   age1... public key (required)
#   BACKUP_REMOTE          rclone remote name holding the destination (default "offsite")
#   BACKUP_PATH            path/bucket on that remote (required), e.g. "mrcv-backups/site1"
#   BACKUP_RETENTION_DAYS  database / config copies kept remotely (default 35)
#   RCLONE_CONFIG_OFFSITE_*  the destination's rclone settings (type, provider, keys ...;
#                          keep credentials in the SOPS secrets file)
#   BACKUP_CRYPT_PASSWORD, BACKUP_CRYPT_SALT, MINIO_BACKUP_USER/PASSWORD (secrets-init.sh)
# Exits non-zero on any failure (systemd marks mrcv-backup.service failed); on success
# touches /var/lib/mrcv/last-backup-ok, which ops/deploy/preflight.sh checks.
# Restore and the monthly restore drill: ops/restore.sh.
set -Eeuo pipefail
shopt -s inherit_errexit
umask 077

MRCV_DIR="${MRCV_DIR:-/opt/mrcv}"
ENV_FILE="${MRCV_ENV_FILE:-/run/mrcv/.env}"
WORK_ROOT="${BACKUP_WORK_DIR:-/srv/mrcv/backups}"   # on the LUKS volume
STAMP_DIR="${BACKUP_STAMP_DIR:-/var/lib/mrcv}"

log() { printf '%s mrcv-backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
trap 'log "FAILED at line ${LINENO} (exit $?)"' ERR
[ -r "$ENV_FILE" ] || { log "env file $ENV_FILE not readable (is the stack up?)"; exit 1; }
# shellcheck source=lib/backup-common.sh
. "$MRCV_DIR/ops/lib/backup-common.sh"
backup_settings
: "${RECIPIENT:?BACKUP_AGE_RECIPIENT is not set}"

ts="$(date -u +%Y%m%dT%H%M%SZ)"
day="$(date -u +%Y/%m/%d)"
install -d -m 0700 "$WORK_ROOT"
WORK="$(mktemp -d "$WORK_ROOT/run.XXXXXX")"
trap 'rm -rf -- "$WORK" "${RCLONE_ENV:-}"' EXIT
setup_rclone

# ── databases ────────────────────────────────────────────────────────────────
dump_db() {  # dump_db <database> <output>
  # $POSTGRES_USER expands INSIDE the container (socket auth, no secret on a command line).
  compose exec -T postgres sh -c "exec pg_dump -U \"\$POSTGRES_USER\" -d '$1' -Fc" > "$2"
  local size; size="$(stat -c %s "$2")"
  if [ "$size" -lt 1024 ] || [ "$(head -c 5 "$2")" != "PGDMP" ]; then
    log "dump of $1 looks invalid (size=$size)"; exit 1
  fi
  compose exec -T postgres pg_restore --list < "$2" > /dev/null
}
for name in "$DB_NAME" orthanc; do
  log "dumping $name"
  dump_db "$name" "$WORK/$name.dump"
  age -r "$RECIPIENT" -o "$WORK/${name}_${ts}.dump.age" "$WORK/$name.dump"
  rm -f "$WORK/$name.dump"
done

# Physical base backup: with the continuously shipped WAL (wal-shipper) this allows a
# point-in-time restore to within minutes of a failure (ops/restore.sh --pitr-drill).
log "base backup (point-in-time recovery)"
compose exec -T postgres sh -c 'exec pg_basebackup -U "$POSTGRES_USER" -D - -Ft -X none -z' \
  > "$WORK/base.tar.gz"
[ "$(stat -c %s "$WORK/base.tar.gz")" -gt 1024 ] || { log "base backup looks empty"; exit 1; }
age -r "$RECIPIENT" -o "$WORK/base_${ts}.tar.gz.age" "$WORK/base.tar.gz"
rm -f "$WORK/base.tar.gz"
( cd "$WORK" && sha256sum "./base_${ts}.tar.gz.age" > "base_${ts}.tar.gz.age.sha256" )

log "packing configuration"
config=()
for p in /etc/mrcv/mrcv.env /etc/mrcv/secrets.enc.env "$PKI"; do
  [ -e "$p" ] && config+=("${p#/}")
done
if [ ${#config[@]} -gt 0 ]; then
  tar -C / -cf - "${config[@]}" | age -r "$RECIPIENT" -o "$WORK/config_${ts}.tar.age"
fi

( cd "$WORK" && sha256sum ./*.dump.age > "SHA256SUMS_${ts}" )
[ ${#config[@]} -gt 0 ] && ( cd "$WORK" && sha256sum "./config_${ts}.tar.age" > "config_${ts}.tar.age.sha256" )
log "uploading database and configuration copies"
rclone copy /work "${REMOTE}:${DEST_PATH}/db/${day}" --include "*.dump.age" --include "SHA256SUMS_*"
[ ${#config[@]} -gt 0 ] && rclone copy /work "${REMOTE}:${DEST_PATH}/config/${day}" --include "config_*.tar.age*"
rclone copy /work "${REMOTE}:${DEST_PATH}/base/${day}" --include "base_*.tar.gz.age*"
rclone check /work "${REMOTE}:${DEST_PATH}/db/${day}" --one-way --include "*.dump.age"

# ── objects ──────────────────────────────────────────────────────────────────
for bucket in "$ARTIFACT_BUCKET" "$DICOM_BUCKET"; do
  log "syncing bucket $bucket (encrypted, incremental)"
  rclone sync "mrcvminio:${bucket}" "mrcvcrypt:${bucket}" \
    --backup-dir "mrcvcrypt:_versions/${day}/${bucket}" --transfers 8 --checkers 16 --stats-one-line
done

# ── remote retention (databases / configuration / object versions) ───────────
log "pruning copies older than ${KEEP_DAYS} days"
rclone delete "${REMOTE}:${DEST_PATH}/db" --min-age "${KEEP_DAYS}d"
rclone delete "${REMOTE}:${DEST_PATH}/config" --min-age "${KEEP_DAYS}d"
rclone delete "mrcvcrypt:_versions" --min-age "${KEEP_DAYS}d" 2>/dev/null || true
rclone delete "${REMOTE}:${DEST_PATH}/base" --min-age "${KEEP_DAYS}d" 2>/dev/null || true
# WAL is needed back to the oldest base backup kept, plus a day of margin.
rclone delete "${REMOTE}:${DEST_PATH}/wal" --min-age "$((KEEP_DAYS + 1))d" 2>/dev/null || true

install -d -m 0755 "$STAMP_DIR"
touch "$STAMP_DIR/last-backup-ok"
log "backup complete: ${REMOTE}:${DEST_PATH} (${day})"
