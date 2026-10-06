# shellcheck shell=bash disable=SC2034
# Shared by ops/backup.sh and ops/restore.sh (sourced, not executed).
#
# Expects: MRCV_DIR, ENV_FILE (the stack's tmpfs env), WORK (a private work directory on
# the encrypted volume). Provides: envval, compose, rclone (in a container sharing the
# MinIO container's network namespace), and the remotes
#   mrcvminio:   the platform's MinIO (read-only backup user)
#   ${REMOTE}:   the operator's destination (RCLONE_CONFIG_<REMOTE>_* in the secrets)
#   mrcvcrypt:   ${REMOTE}:${DEST_PATH}/objects, encrypted with BACKUP_CRYPT_PASSWORD

RCLONE_IMAGE="${RCLONE_IMAGE:-rclone/rclone:1.68}"

envval() { { grep -E "^$1=" "$ENV_FILE" || true; } | tail -1 | cut -d= -f2-; }

backup_settings() {
  RECIPIENT="$(envval BACKUP_AGE_RECIPIENT)"
  REMOTE="$(envval BACKUP_REMOTE)"; REMOTE="${REMOTE:-offsite}"
  DEST_PATH="$(envval BACKUP_PATH)"
  KEEP_DAYS="$(envval BACKUP_RETENTION_DAYS)"; KEEP_DAYS="${KEEP_DAYS:-35}"
  PKI="$(envval MRCV_PKI_DIR)"; PKI="${PKI:-/srv/mrcv/pki}"
  DB_NAME="$(envval POSTGRES_DB)"; DB_NAME="${DB_NAME:-mri_platform}"
  ARTIFACT_BUCKET="$(envval MINIO_BUCKET)"; ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-mri-artifacts}"
  DICOM_BUCKET="$(envval ORTHANC_S3_BUCKET)"; DICOM_BUCKET="${DICOM_BUCKET:-orthanc-dicom}"
  : "${DEST_PATH:?BACKUP_PATH is not set}"
  COMPOSE_FILES=(-f "$MRCV_DIR/docker-compose.yml" -f "$MRCV_DIR/docker-compose.prod.yml")
  local overlay
  for overlay in $(envval MRCV_COMPOSE_OVERLAYS | tr ',' ' '); do
    COMPOSE_FILES+=(-f "$MRCV_DIR/$overlay")
  done
}

compose() { docker compose --project-directory "$MRCV_DIR" --env-file "$ENV_FILE" "${COMPOSE_FILES[@]}" "$@"; }

# Writes the rclone settings to a tmpfs file next to the env file (they hold credentials).
setup_rclone() {
  MINIO_CONTAINER="$(compose ps -q minio)"
  [ -n "$MINIO_CONTAINER" ] || { echo "minio container is not running" >&2; return 1; }
  RCLONE_ENV="$(mktemp "$(dirname "$ENV_FILE")/rclone.XXXXXX")"
  # --ca-cert REPLACES rclone's trust store: public roots (destination) + internal CA (MinIO).
  cat /etc/ssl/certs/ca-certificates.crt "$PKI/ca.crt" > "$WORK/ca-bundle.crt"
  local pw salt
  # Obscured via stdin, so the passwords never appear in a process listing.
  pw="$(envval BACKUP_CRYPT_PASSWORD | docker run -i --rm "$RCLONE_IMAGE" obscure -)"
  salt="$(envval BACKUP_CRYPT_SALT | docker run -i --rm "$RCLONE_IMAGE" obscure -)"
  {
    echo "RCLONE_CONFIG=/dev/null"   # every remote comes from these variables
    grep -E '^RCLONE_CONFIG_' "$ENV_FILE" || true
    printf 'RCLONE_CONFIG_MRCVMINIO_TYPE=s3\nRCLONE_CONFIG_MRCVMINIO_PROVIDER=Minio\n'
    printf 'RCLONE_CONFIG_MRCVMINIO_ENDPOINT=https://localhost:9000\n'
    printf 'RCLONE_CONFIG_MRCVMINIO_ACCESS_KEY_ID=%s\n' "${RESTORE_MINIO_USER:-$(envval MINIO_BACKUP_USER)}"
    printf 'RCLONE_CONFIG_MRCVMINIO_SECRET_ACCESS_KEY=%s\n' "${RESTORE_MINIO_PASSWORD:-$(envval MINIO_BACKUP_PASSWORD)}"
    printf 'RCLONE_CONFIG_MRCVCRYPT_TYPE=crypt\n'
    printf 'RCLONE_CONFIG_MRCVCRYPT_REMOTE=%s:%s/objects\n' "$REMOTE" "$DEST_PATH"
    printf 'RCLONE_CONFIG_MRCVCRYPT_PASSWORD=%s\nRCLONE_CONFIG_MRCVCRYPT_PASSWORD2=%s\n' "$pw" "$salt"
  } > "$RCLONE_ENV"
  chmod 0600 "$RCLONE_ENV"
}

rclone() {
  docker run --rm --network "container:${MINIO_CONTAINER}" --env-file "$RCLONE_ENV" \
    -v "$WORK:/work" "$RCLONE_IMAGE" --ca-cert /work/ca-bundle.crt "$@"
}

# Create the RLS-bound application role (alembic 044) if it does not exist. Roles are
# cluster-wide and not part of a database dump, so a restored or migrated database needs
# it before its GRANTs are restored. The password is sent on stdin (dollar-quoted) and
# never appears in a process listing.
ensure_app_role() {
  local user pw tag
  user="$(envval POSTGRES_APP_USER)"; user="${user:-mrv_app}"
  pw="$(envval POSTGRES_APP_PASSWORD)"
  [ -n "$pw" ] || { echo "POSTGRES_APP_PASSWORD missing from $ENV_FILE" >&2; return 1; }
  tag="pw$(openssl rand -hex 6)"
  printf 'DO $do$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $%s$%s$%s$) THEN EXECUTE format(%s, $%s$%s$%s$, $%s$%s$%s$); END IF; END $do$;\n' \
    "$tag" "$user" "$tag" \
    "'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE'" \
    "$tag" "$user" "$tag" "$tag" "$pw" "$tag" \
    | compose exec -T postgres sh -c 'exec psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -q' >/dev/null
}
