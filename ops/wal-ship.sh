#!/bin/sh
# Continuous off-site shipping of PostgreSQL WAL segments (checklist ADM-05: RPO <= 15 min).
#
# Runs as the `wal-shipper` service (docker-compose.prod.yml). Postgres archives every
# completed WAL segment into /wal-archive (archive_command) and switches segments at least
# every archive_timeout (300 s); this loop age-encrypts each segment to the OFFLINE backup
# key (BACKUP_AGE_RECIPIENT) and uploads it to ${BACKUP_REMOTE}:${BACKUP_PATH}/wal/, then
# deletes the local copy. Worst-case data loss = archive_timeout + one ship interval.
#
# Point-in-time restore / drill: ops/restore.sh --pitr-drill (base backup from ops/backup.sh
# + these segments).
set -eu

: "${BACKUP_AGE_RECIPIENT:?}" "${BACKUP_PATH:?}"
REMOTE="${BACKUP_REMOTE:-offsite}"
INTERVAL="${WAL_SHIP_INTERVAL:-60}"
ARCHIVE=/wal-archive
WORK=/tmp/wal-ship
mkdir -p "$WORK"
export RCLONE_CONFIG=/dev/null
# rclone's --ca-cert replaces the trust store: public roots + the internal CA.
CA_ARGS=""
if [ -f /run/pki/ca.crt ]; then
  cat /etc/ssl/certs/ca-certificates.crt /run/pki/ca.crt > "$WORK/ca-bundle.crt"
  CA_ARGS="--ca-cert $WORK/ca-bundle.crt"
fi

log() { printf '%s wal-ship: %s\n' "$(date -u +%FT%TZ)" "$*"; }
log "shipping $ARCHIVE every ${INTERVAL}s to ${REMOTE}:${BACKUP_PATH}/wal"

while :; do
  shipped=0
  for f in "$ARCHIVE"/*; do
    [ -f "$f" ] || continue
    name="$(basename "$f")"
    case "$name" in .*) continue ;; esac            # still being written by archive_command
    age -r "$BACKUP_AGE_RECIPIENT" -o "$WORK/$name.age" "$f"
    # shellcheck disable=SC2086
    if rclone $CA_ARGS copyto "$WORK/$name.age" "${REMOTE}:${BACKUP_PATH}/wal/$name.age" --retries 3; then
      rm -f "$f" "$WORK/$name.age"
      shipped=$((shipped + 1))
    else
      rm -f "$WORK/$name.age"
      log "upload of $name failed; will retry"
      break
    fi
  done
  [ "$shipped" -gt 0 ] && log "shipped $shipped segment(s)"
  # Heartbeat for preflight / monitoring: last successful pass.
  date -u +%s > "$ARCHIVE/.last-ship"
  sleep "$INTERVAL"
done
