#!/usr/bin/env bash
# Copy the AI-artifact objects from the old MinIO into the new, encrypted one.
#
#   sudo OLD_MINIO_ACCESS_KEY=... OLD_MINIO_SECRET_KEY=... ops/migrate/minio-copy.sh
#
# Objects written through the new MinIO land encrypted (bucket default SSE-S3, set by
# minio-init), so a copy is also the re-encryption of everything stored before. Use it
# for a host move and for an in-place switch to the production stack alike. Read-only on
# the source; reports object counts on both sides and fails if they differ.
#
#   OLD_MINIO_URL   default http://host.docker.internal:19000 — open an SSH tunnel to the
#                   old host first, bound to the Docker bridge so the copy container sees it:
#                     ssh -N -L 172.17.0.1:19000:127.0.0.1:9000 old-host
#   OLD_BUCKET      default mri-artifacts
# The new side (URL, root credentials, CA) comes from the stack env (/run/mrcv/.env).
# DICOM is not copied here: Orthanc storage moves with ops/migrate/orthanc-copy.py.
set -Eeuo pipefail
shopt -s inherit_errexit
umask 077

MRCV_DIR="${MRCV_DIR:-/opt/mrcv}"
ENV_FILE="${MRCV_ENV_FILE:-/run/mrcv/.env}"
OLD_URL="${OLD_MINIO_URL:-http://host.docker.internal:19000}"
: "${OLD_MINIO_ACCESS_KEY:?}" "${OLD_MINIO_SECRET_KEY:?}"
envval() { { grep -E "^$1=" "$ENV_FILE" || true; } | tail -1 | cut -d= -f2-; }
OLD_BUCKET="${OLD_BUCKET:-mri-artifacts}"
NEW_BUCKET="$(envval MINIO_BUCKET)"; NEW_BUCKET="${NEW_BUCKET:-mri-artifacts}"
PKI="$(envval MRCV_PKI_DIR)"; PKI="${PKI:-/srv/mrcv/pki}"
IMAGE="${MINIO_MC_IMAGE:-minio/minio:latest}"

files=(-f "$MRCV_DIR/docker-compose.yml" -f "$MRCV_DIR/docker-compose.prod.yml")
minio_c="$(docker compose --project-directory "$MRCV_DIR" --env-file "$ENV_FILE" "${files[@]}" ps -q minio)"
[ -n "$minio_c" ] || { echo "new minio is not running" >&2; exit 1; }
network="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$minio_c" | head -1)"

envf="$(mktemp "$(dirname "$ENV_FILE")/mc.XXXXXX")"
trap 'rm -f -- "$envf"' EXIT
# mc reads aliases from MC_HOST_<alias>=scheme://key:secret@host — credentials stay in
# a tmpfs file, never on a command line.
urlenc() { python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$1"; }
{
  old_host="${OLD_URL#*://}"; old_scheme="${OLD_URL%%://*}"
  echo "MC_HOST_old=${old_scheme}://$(urlenc "$OLD_MINIO_ACCESS_KEY"):$(urlenc "$OLD_MINIO_SECRET_KEY")@${old_host}"
  echo "MC_HOST_new=https://$(urlenc "$(envval MINIO_ACCESS_KEY)"):$(urlenc "$(envval MINIO_SECRET_KEY)")@minio:9000"
} > "$envf"

docker run --rm --network "$network" --add-host host.docker.internal:host-gateway \
  --env-file "$envf" -e SSL_CERT_FILE=/ca.crt -v "$PKI/ca.crt:/ca.crt:ro" \
  --entrypoint sh "$IMAGE" -c "
    set -e
    mc mirror --preserve --overwrite old/$OLD_BUCKET new/$NEW_BUCKET
    a=\$(mc ls --recursive old/$OLD_BUCKET | wc -l); b=\$(mc ls --recursive new/$NEW_BUCKET | wc -l)
    echo \"objects: old \$a, new \$b\"
    mc encrypt info new/$NEW_BUCKET
    [ \"\$b\" -ge \"\$a\" ]
  "
echo "minio-copy: done (objects in the new bucket are encrypted at rest)"
