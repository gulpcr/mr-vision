#!/usr/bin/env bash
# Point-in-time recovery drill (checklist ADM-05: RPO <= 15 min, RTO <= 2 h).
#
#   sudo ops/pitr-drill.sh --identity /media/usb/backup-age.key [--target "2026-10-02 14:00:00+00"]
#
# Rebuilds the platform database in a THROWAWAY postgres container from the latest
# off-site base backup (ops/backup.sh) plus the continuously shipped WAL (ops/wal-ship.sh),
# replaying to --target (default: as far as the WAL goes), then reports:
#   RTO  wall-clock time from start to a queryable database
#   RPO  age of the newest WAL segment off-site (or, with --target, data lag at the target)
# and row counts next to the live database. Touches nothing live. Record the result in the
# contingency-plan test log (policies/06). For a real recovery, the recovered data
# directory (kept with --keep) replaces the postgres volume of a freshly deployed stack.
set -Eeuo pipefail
shopt -s inherit_errexit
umask 077

MRCV_DIR="${MRCV_DIR:-/opt/mrcv}"
ENV_FILE="${MRCV_ENV_FILE:-/run/mrcv/.env}"
WORK_ROOT="${BACKUP_WORK_DIR:-/srv/mrcv/backups}"
IDENTITY=""; TARGET=""; KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --identity) IDENTITY="$2"; shift 2 ;;
    --target) TARGET="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
log() { printf '%s pitr-drill: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }
[ -r "$IDENTITY" ] || die "--identity FILE (the offline backup age key) is required"
[ -r "$ENV_FILE" ] || die "env file $ENV_FILE not readable"

# shellcheck source=lib/backup-common.sh
. "$MRCV_DIR/ops/lib/backup-common.sh"
backup_settings
started=$(date +%s)
install -d -m 0700 "$WORK_ROOT"
WORK="$(mktemp -d "$WORK_ROOT/pitr.XXXXXX")"
CONTAINER="mrcv-pitr-drill-$$"
cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -f -- "${RCLONE_ENV:-}"
  if [ "$KEEP" = 0 ]; then rm -rf -- "$WORK"; else log "kept $WORK/pgdata"; fi
}
trap cleanup EXIT
setup_rclone

log "fetching the latest base backup"
latest="$(rclone lsf -R "${REMOTE}:${DEST_PATH}/base" --files-only | grep -E 'base_.*\.tar\.gz\.age$' | sort | tail -1)"
[ -n "$latest" ] || die "no base backup under ${REMOTE}:${DEST_PATH}/base (has ops/backup.sh run?)"
rclone copy "${REMOTE}:${DEST_PATH}/base/$(dirname "$latest")" /work/base --include "$(basename "$latest")*"
( cd "$WORK/base" && sha256sum -c --quiet "$(basename "$latest").sha256" ) || die "base backup checksum mismatch"
log "fetching shipped WAL"
newest_wal="$(rclone lsjson "${REMOTE}:${DEST_PATH}/wal" --files-only 2>/dev/null \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(max((x["ModTime"] for x in d), default=""))')"
rclone copy "${REMOTE}:${DEST_PATH}/wal" /work/walenc

install -d -m 0700 "$WORK/pgdata" "$WORK/wal"
age -d -i "$IDENTITY" "$WORK/base/$(basename "$latest")" | tar -xz -C "$WORK/pgdata" \
  || die "cannot decrypt / unpack the base backup (wrong --identity?)"
n=0
for f in "$WORK"/walenc/*.age; do
  [ -e "$f" ] || continue
  age -d -i "$IDENTITY" -o "$WORK/wal/$(basename "$f" .age)" "$f" || die "cannot decrypt WAL $(basename "$f")"
  n=$((n + 1))
done
log "base $(basename "$latest"), $n WAL segment(s)"
# The container's postgres user must read the segments (the work dir itself stays 0700).
chmod 0755 "$WORK/wal"
find "$WORK/wal" -type f -exec chmod 0644 {} +

{
  echo "restore_command = 'cp /wal/%f %p'"
  echo "recovery_target_action = 'promote'"
  [ -n "$TARGET" ] && echo "recovery_target_time = '$TARGET'"
} >> "$WORK/pgdata/postgresql.auto.conf"
: > "$WORK/pgdata/recovery.signal"
# The production instance runs with a custom hba file; the drill uses socket trust only.
printf 'local all all trust\n' > "$WORK/pgdata/pg_hba.conf"

docker run -d --name "$CONTAINER" --network none \
  -v "$WORK/pgdata:/var/lib/postgresql/data" -v "$WORK/wal:/wal:ro" \
  -e POSTGRES_PASSWORD=unused postgres:16-alpine >/dev/null
db="$DB_NAME"
user="$(envval POSTGRES_USER)"; user="${user:-mri_admin}"
q() { docker exec "$CONTAINER" psql -U "$user" -d "$db" -tAqc "$1" 2>/dev/null; }
for _ in $(seq 1 600); do
  [ "$(q 'SELECT pg_is_in_recovery()')" = f ] && break
  if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != true ]; then
    docker logs --tail 30 "$CONTAINER" >&2
    die "the recovery instance stopped (see the log above)"
  fi
  sleep 2
done
[ "$(q 'SELECT pg_is_in_recovery()')" = f ] || { docker logs --tail 30 "$CONTAINER" >&2; die "recovery did not complete"; }
rto=$(( $(date +%s) - started ))

latest_data="$(q "SET app.platform = 'on'; SELECT max(timestamp) FROM audit_log")"
counts="$(q "SET app.platform = 'on'; SELECT (SELECT count(*) FROM studies)||' studies, '||(SELECT count(*) FROM results_index)||' results, '||(SELECT count(*) FROM audit_log)||' audit rows'")"
live="$(compose exec -T postgres sh -c "psql -U \"\$POSTGRES_USER\" -d '$db' -tAqc \"SET app.platform = 'on'; SELECT (SELECT count(*) FROM studies)||' studies, '||(SELECT count(*) FROM results_index)||' results, '||(SELECT count(*) FROM audit_log)||' audit rows'\"")"
# RPO: without --target, the age of the newest WAL off-site (what a disaster right now
# would lose); with --target, how far before the target the newest recovered data is.
if [ -n "$TARGET" ]; then
  ref="$TARGET"; since="$latest_data"
else
  ref="$(date -u +%FT%TZ)"; since="$newest_wal"
fi
rpo="?"
if [ -n "$since" ]; then
  rpo="$(docker exec "$CONTAINER" psql -U "$user" -d "$db" -tAqc \
    "SELECT round(extract(epoch FROM (timestamptz '${ref}' - timestamptz '${since}')))" 2>/dev/null || echo "?")"
fi

log "recovered: $counts"
log "live:      $live"
log "newest recovered data: $latest_data   newest WAL off-site: ${newest_wal:-none}"
log "RTO ${rto}s (target <= 7200)   RPO ${rpo}s (target <= 900)"
if [ "$rto" -le 7200 ] && [ "$rpo" != "?" ] && [ "${rpo%.*}" -le 900 ]; then
  log "PITR drill PASSED - record it in the contingency-plan test log"
else
  log "PITR drill: targets NOT met"
  exit 1
fi
