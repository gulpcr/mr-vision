#!/usr/bin/env bash
# Host-side compliance checks before the production stack starts (ops/deploy/up.sh runs
# this and refuses to start on any FAIL). Read-only; safe to run any time:
#
#   sudo MRCV_ENV_FILE=/run/mrcv/.env ops/deploy/preflight.sh [--first-deploy]
#
# --first-deploy downgrades "no recent backup" to a warning (nothing to back up yet).
# The application checks its own settings at start-up as well
# (backend/app/config.py: insecure_config_problems / production_config_problems).
set -uo pipefail

MRCV_DIR="${MRCV_DIR:-/opt/mrcv}"
ENV_FILE="${MRCV_ENV_FILE:-/run/mrcv/.env}"
FIRST_DEPLOY=0
[ "${1:-}" = "--first-deploy" ] && FIRST_DEPLOY=1

fails=0; warns=0
pass() { printf '  PASS  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; fails=$((fails+1)); }
warn() { printf '  WARN  %s\n' "$*"; warns=$((warns+1)); }
envval() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-; }

echo "MRCV preflight ($(date -u +%FT%TZ))"

# ── Secrets / env file ───────────────────────────────────────────────────────
echo "secrets"
if [ ! -r "$ENV_FILE" ]; then
  fail "env file $ENV_FILE not readable"
else
  [ "$(findmnt -no FSTYPE -T "$ENV_FILE" 2>/dev/null)" = tmpfs ] \
    && pass "env file is on tmpfs" || fail "env file $ENV_FILE is not on tmpfs"
  [ "$(stat -c %a "$ENV_FILE")" = 600 ] && pass "env file mode 0600" || fail "env file mode is $(stat -c %a "$ENV_FILE")"
  missing=0
  for k in PUBLIC_DOMAIN JWT_SECRET_KEY PHI_HASH_SALT POSTGRES_PASSWORD POSTGRES_APP_PASSWORD \
           REDIS_PASSWORD MINIO_SECRET_KEY MINIO_APP_PASSWORD MINIO_ORTHANC_PASSWORD \
           MINIO_KMS_SECRET_KEY ORTHANC_PASSWORD ORTHANC_DB_PASSWORD ORTHANC_WEBHOOK_SECRET \
           ORTHANC_STORAGE_MASTER_KEY BACKUP_AGE_RECIPIENT; do
    v="$(envval "$k")"
    case "$v" in
      ""|*REPLACE*|*changeme*|orthanc|admin|password) fail "$k is empty or a placeholder"; missing=1 ;;
    esac
  done
  [ "$missing" = 0 ] && pass "required secrets and settings present"
  for k in $(grep -oE '^[A-Z_]+' "$ENV_FILE" | sort | uniq -d); do
    warn "$k is set more than once in the env file (last wins)"
  done
  for f in /etc/mrcv/.env "$MRCV_DIR/.env"; do
    [ -f "$f" ] && warn "a plaintext env file exists on disk ($f) - remove it"
  done
fi

# ── Encryption at rest ───────────────────────────────────────────────────────
echo "encryption at rest"
root="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null)"
if [ -z "$root" ]; then
  fail "docker is not running"
else
  src="$(findmnt -no SOURCE -T "$root")"
  if lsblk -sno TYPE "$src" 2>/dev/null | grep -qx crypt; then
    pass "Docker data-root $root is on an encrypted (dm-crypt/LUKS) device ($src)"
  else
    fail "Docker data-root $root is on $src, which is not LUKS-encrypted (ops/deploy/bootstrap.sh)"
  fi
fi
for d in /srv/mrcv/pki /srv/mrcv/backups; do
  [ -d "$d" ] || continue
  s="$(findmnt -no SOURCE -T "$d")"
  lsblk -sno TYPE "$s" 2>/dev/null | grep -qx crypt && pass "$d is on the encrypted volume" \
    || fail "$d is not on the encrypted volume"
done
if [ "$(cat /proc/sys/crypto/fips_enabled 2>/dev/null)" = 1 ]; then
  pass "kernel in FIPS mode (FIPS 140 validated modules)"
else
  warn "kernel not in FIPS mode (bootstrap.sh --fips for FIPS 140 validated modules)"
fi
swap="$(swapon --noheadings --show=NAME 2>/dev/null)"
for s in $swap; do
  lsblk -sno TYPE "$s" 2>/dev/null | grep -qx crypt || warn "swap $s is not encrypted (memory can hold PHI)"
done

# ── Internal PKI ─────────────────────────────────────────────────────────────
echo "certificates"
pki="$(envval MRCV_PKI_DIR)"; pki="${pki:-/srv/mrcv/pki}"
if [ ! -f "$pki/ca.crt" ]; then
  fail "no internal CA at $pki (ops/pki/make-certs.sh)"
else
  for svc in postgres redis minio orthanc backend; do
    c="$pki/$svc/server.crt"
    if [ ! -f "$c" ]; then fail "$svc certificate missing"; continue; fi
    openssl verify -CAfile "$pki/ca.crt" "$c" >/dev/null 2>&1 || { fail "$svc certificate not signed by the CA"; continue; }
    openssl x509 -checkend $((30*86400)) -noout -in "$c" >/dev/null \
      && pass "$svc certificate valid > 30 days" || fail "$svc certificate expires within 30 days (re-run make-certs.sh)"
  done
  openssl x509 -checkend $((180*86400)) -noout -in "$pki/ca.crt" >/dev/null || warn "internal CA expires within 180 days"
fi
byo="$(envval TLS_CERT_FILE)"
if [ -n "$byo" ]; then
  host_path="$(envval TLS_BYO_DIR)"; host_path="${host_path:-/srv/mrcv/tls}/$(basename "$byo")"
  if [ -f "$host_path" ]; then
    openssl x509 -checkend $((14*86400)) -noout -in "$host_path" >/dev/null \
      && pass "public certificate valid > 14 days" || fail "public certificate $host_path expires within 14 days"
  else
    fail "TLS_CERT_FILE set but $host_path does not exist"
  fi
fi

# ── Network exposure ─────────────────────────────────────────────────────────
echo "network"
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  pass "host firewall (ufw) active"
  ufw status | grep -E "^22(/tcp)?\s+ALLOW\s+Anywhere" >/dev/null && warn "SSH is open to the whole internet (restrict with bootstrap.sh --ssh-cidr)"
else
  fail "host firewall (ufw) is not active"
fi
if iptables -S DOCKER-USER 2>/dev/null | grep -q mrcv-dicom; then
  pass "DICOM port restricted to scanner networks (DOCKER-USER)"
else
  warn "no DOCKER-USER restriction on the DICOM port (Docker bypasses ufw for published ports; ops/deploy/docker-firewall.sh)"
fi
dicom="$(envval DICOM_PORT)"; dicom="${dicom:-2762}"
files=(-f docker-compose.yml -f docker-compose.prod.yml)
for overlay in $(envval MRCV_COMPOSE_OVERLAYS | tr ',' ' '); do files+=(-f "$overlay"); done
published="$(cd "$MRCV_DIR" && docker compose --env-file "$ENV_FILE" "${files[@]}" --profile letsencrypt \
  config --format json 2>/dev/null | python3 -c '
import json,sys
c=json.load(sys.stdin)
for n,s in c["services"].items():
    for p in s.get("ports",[]):
        print(n, p.get("published"))' 2>/dev/null)"
if [ -z "$published" ]; then
  fail "could not render the compose configuration (docker compose config)"
else
  bad="$(echo "$published" | awk -v d="$dicom" '!(($1=="nginx" && ($2=="80"||$2=="443")) || ($1=="orthanc" && $2==d))')"
  [ -z "$bad" ] && pass "only 80/443 (nginx) and DICOM $dicom (orthanc) are published" \
    || fail "unexpected published ports: $(echo "$bad" | tr '\n' ' ')"
fi
[ "$(envval DICOM_TLS_ENABLED)" = false ] && warn "DICOM TLS is off: allowed only when DICOM_BIND is a VPN address ($(envval DICOM_BIND))"

# ── TLS evidence (optional: needs the public endpoint up) ───────────────────
if [ "${PREFLIGHT_TLS_SCAN:-0}" = 1 ]; then
  echo "tls scan"
  if MRCV_ENV_FILE="$ENV_FILE" bash "$MRCV_DIR/ops/compliance/tls-scan.sh" >/tmp/mrcv-tls-scan.log 2>&1; then
    pass "public TLS scan clean ($(tail -1 /tmp/mrcv-tls-scan.log))"
  else
    fail "public TLS scan found problems: $(grep FAIL /tmp/mrcv-tls-scan.log | head -3 | tr '\n' ' ')"
  fi
fi

# ── Operations ───────────────────────────────────────────────────────────────
echo "operations"
[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ] \
  && pass "clock synchronised (audit timestamps)" || warn "clock is not NTP-synchronised"
systemctl is-enabled unattended-upgrades >/dev/null 2>&1 && pass "automatic security updates enabled" \
  || warn "unattended-upgrades not enabled"
ship_c="$(cd "$MRCV_DIR" && docker compose --env-file "$ENV_FILE" "${files[@]}" ps -q wal-shipper 2>/dev/null)"
if [ -n "$ship_c" ]; then
  last="$(docker exec "$ship_c" cat /wal-archive/.last-ship 2>/dev/null || echo 0)"
  if [ $(( $(date +%s) - ${last:-0} )) -lt 600 ]; then
    pass "WAL shipping alive (continuous off-site archive, RPO <= 15 min)"
  else
    fail "WAL shipper has not completed a pass in 10 minutes (docker compose logs wal-shipper)"
  fi
elif [ "$FIRST_DEPLOY" = 1 ]; then
  warn "wal-shipper not running yet (first deploy)"
else
  fail "wal-shipper is not running: the RPO is the nightly backup (24 h)"
fi
if systemctl is-active mrcv-backup.timer >/dev/null 2>&1; then
  pass "backup timer active"
  stamp=/var/lib/mrcv/last-backup-ok
  if [ -f "$stamp" ] && [ $(( $(date +%s) - $(stat -c %Y "$stamp") )) -lt $((36*3600)) ]; then
    pass "last successful backup $(date -u -r "$stamp" +%FT%TZ)"
  elif [ "$FIRST_DEPLOY" = 1 ]; then
    warn "no successful backup in the last 36 h (first deploy)"
  else
    fail "no successful backup in the last 36 h (journalctl -u mrcv-backup)"
  fi
else
  fail "mrcv-backup.timer is not active"
fi

echo "result: ${fails} FAIL, ${warns} WARN"
[ "$fails" = 0 ]
