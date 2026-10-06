#!/bin/sh
# One-shot (service `pg-init` in docker-compose.prod.yml): creates the role and database
# that hold Orthanc's index (patient/study tags) once Orthanc moves off its SQLite file.
# Idempotent; connects over TLS as the platform owner role.
set -eu
: "${ORTHANC_DB_PASSWORD:?set ORTHANC_DB_PASSWORD}"
export PGPASSWORD="$POSTGRES_PASSWORD"
export PGSSLMODE=verify-full PGSSLROOTCERT=/run/pki/ca.crt
psql_() { psql -v ON_ERROR_STOP=1 -h postgres -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"; }
until pg_isready -h postgres -U "$POSTGRES_USER" >/dev/null 2>&1; do sleep 2; done
psql_ -q -v pw="$ORTHANC_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE orthanc LOGIN PASSWORD %L', :'pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orthanc') \gexec
SELECT format('ALTER ROLE orthanc LOGIN PASSWORD %L', :'pw') \gexec
SELECT 'CREATE DATABASE orthanc OWNER orthanc'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'orthanc') \gexec
REVOKE ALL ON DATABASE orthanc FROM PUBLIC;
SQL
echo "pg-init: orthanc index database ready"
