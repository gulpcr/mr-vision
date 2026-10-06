#!/bin/sh
# Starts postgres:16-alpine with TLS on and plaintext refused (docker-compose.prod.yml).
#
# Postgres refuses a private key that is group/world readable or not owned by it. Bind
# mounts cannot guarantee that (Windows / Docker Desktop shows every file as 0777 root),
# so the key is copied out of the read-only PKI mount with the right owner and mode.
#
# Continuous WAL archiving (checklist ADM-05, RPO <= 15 min): every completed segment is
# copied to /wal-archive (written to a dot-file first, then renamed, so the wal-shipper
# never uploads a half-written segment) and a segment switch is forced at least every
# ARCHIVE_TIMEOUT seconds (default 300).
set -eu
PKI=/run/pki-src
DST=/var/lib/postgresql/tls
mkdir -p "$DST"
install -o postgres -g postgres -m 0644 "$PKI/server.crt" "$DST/server.crt"
install -o postgres -g postgres -m 0600 "$PKI/server.key" "$DST/server.key"
if [ -d /wal-archive ]; then
  chown postgres:postgres /wal-archive
  chmod 0700 /wal-archive
  set -- \
    -c wal_level=replica \
    -c archive_mode=on \
    -c "archive_command=test ! -f /wal-archive/%f && cp %p /wal-archive/.%f.tmp && mv /wal-archive/.%f.tmp /wal-archive/%f" \
    -c "archive_timeout=${ARCHIVE_TIMEOUT:-300}" \
    "$@"
fi
exec docker-entrypoint.sh postgres \
  -c ssl=on \
  -c ssl_cert_file="$DST/server.crt" \
  -c ssl_key_file="$DST/server.key" \
  -c ssl_min_protocol_version=TLSv1.3 \
  -c password_encryption=scram-sha-256 \
  -c hba_file=/etc/postgresql/pg_hba.conf \
  "$@"
