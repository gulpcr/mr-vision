#!/usr/bin/env bash
# Create the RLS-bound application role (POSTGRES_APP_USER / POSTGRES_APP_PASSWORD from
# the stack env) before restoring a database dump that grants to it. Idempotent.
#
#   sudo ops/migrate/create-app-role.sh
set -Eeuo pipefail
shopt -s inherit_errexit
MRCV_DIR="${MRCV_DIR:-/opt/mrcv}"
ENV_FILE="${MRCV_ENV_FILE:-/run/mrcv/.env}"
# shellcheck source=../lib/backup-common.sh
. "$MRCV_DIR/ops/lib/backup-common.sh"
COMPOSE_FILES=(-f "$MRCV_DIR/docker-compose.yml" -f "$MRCV_DIR/docker-compose.prod.yml")
ensure_app_role
echo "create-app-role: $(envval POSTGRES_APP_USER) present"
