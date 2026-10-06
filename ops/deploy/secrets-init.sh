#!/usr/bin/env bash
# Generate every production secret into one SOPS + age encrypted dotenv file.
#
#   sudo ops/deploy/secrets-init.sh [--import OLD_ENV] [--out FILE] [--recipient AGE_PUBKEY]...
#
#   --import OLD_ENV   take existing values from the current deployment's .env for every
#                      key it has (REQUIRED when moving an existing installation: the JWT
#                      master key, PHI hash salt, DB passwords... must stay the same or MFA
#                      secrets, the audit hash chain and stored data become unreadable)
#   --out FILE         default /etc/mrcv/secrets.enc.env
#   --recipient KEY    age public key(s) that can decrypt; default: this host's key
#                      (/etc/mrcv/age.key, created by bootstrap.sh). Add the offline escrow
#                      key too (ops/KEYS.md) so the secrets survive the loss of this host.
#
# The plaintext never touches a disk: values are generated in memory and piped to sops.
# The encrypted file is safe to keep in a PRIVATE config repository; ops/deploy/up.sh
# decrypts it to tmpfs (/run/mrcv) at start-up. Re-running refuses to overwrite an
# existing file (rotate individual keys per ops/KEYS.md instead).
set -Eeuo pipefail
umask 077

OUT=/etc/mrcv/secrets.enc.env
IMPORT=""
RECIPIENTS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --import) IMPORT="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --recipient) RECIPIENTS+=("$2"); shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v sops >/dev/null || { echo "sops not installed (ops/deploy/bootstrap.sh installs it)" >&2; exit 1; }
command -v openssl >/dev/null || { echo "openssl not installed" >&2; exit 1; }
[ -e "$OUT" ] && { echo "$OUT already exists; refusing to overwrite (see ops/KEYS.md to rotate)" >&2; exit 1; }

if [ ${#RECIPIENTS[@]} -eq 0 ]; then
  [ -r /etc/mrcv/age.key ] || { echo "no --recipient and no /etc/mrcv/age.key" >&2; exit 1; }
  RECIPIENTS+=("$(grep -o 'age1[0-9a-z]*' /etc/mrcv/age.key | head -1)")
fi
recipients="$(IFS=,; echo "${RECIPIENTS[*]}")"

# Value of KEY in the imported env file (last assignment wins, quotes stripped).
imported() {
  [ -n "$IMPORT" ] || return 1
  local line
  line="$(grep -E "^${1}=" "$IMPORT" | tail -1)" || return 1
  line="${line#*=}"; line="${line%\"}"; line="${line#\"}"; line="${line%\'}"; line="${line#\'}"
  [ -n "$line" ] || return 1
  printf '%s' "$line"
}

rand() { openssl rand -base64 48 | tr -d '/+=\n' | cut -c1-"${1:-40}"; }

declare -A V
keep_or() {  # keep_or KEY generated-value
  local v
  if v="$(imported "$1")"; then V[$1]="$v"; echo "  $1: imported" >&2
  else V[$1]="$2"; echo "  $1: generated" >&2; fi
}

echo "secrets:" >&2
# ── must survive a host move (import them) ──
keep_or JWT_SECRET_KEY "$(rand 64)"
keep_or PHI_HASH_SALT "$(rand 32)"
keep_or POSTGRES_PASSWORD "$(rand)"
keep_or POSTGRES_APP_PASSWORD "$(rand)"
keep_or REDIS_PASSWORD "$(rand)"
keep_or ORTHANC_USERNAME "orthanc"
keep_or ORTHANC_PASSWORD "$(rand)"
keep_or ORTHANC_WEBHOOK_SECRET "$(rand)"
keep_or MINIO_ACCESS_KEY "mrcv-root"
keep_or MINIO_SECRET_KEY "$(rand)"
# ── new with the encrypted production stack ──
keep_or MINIO_APP_USER "mrcv-app"
keep_or MINIO_APP_PASSWORD "$(rand)"
keep_or MINIO_ORTHANC_USER "mrcv-orthanc"
keep_or MINIO_ORTHANC_PASSWORD "$(rand)"
keep_or MINIO_BACKUP_USER "mrcv-backup"
keep_or MINIO_BACKUP_PASSWORD "$(rand)"
keep_or MINIO_KMS_SECRET_KEY "mrcv-key:$(openssl rand -base64 32)"
keep_or ORTHANC_DB_PASSWORD "$(rand)"
keep_or ORTHANC_STORAGE_KEY_ID "1"
keep_or ORTHANC_STORAGE_MASTER_KEY "$(openssl rand -base64 32)"
keep_or BACKUP_CRYPT_PASSWORD "$(rand 48)"
keep_or BACKUP_CRYPT_SALT "$(rand 48)"
# Optional secrets: carried over only when the old deployment had them.
for k in AUDIT_CHAIN_PREVIOUS_MASTER_KEY AUDIT_CHAIN_ROTATED_AFTER_SEQ SECRET_KEY API_KEY \
         GEMINI_API_KEY HF_TOKEN; do
  if v="$(imported "$k")"; then V[$k]="$v"; echo "  $k: imported" >&2; fi
done
# Derived: what nginx sends to Orthanc.
V[ORTHANC_BASIC_AUTH]="$(printf '%s:%s' "${V[ORTHANC_USERNAME]}" "${V[ORTHANC_PASSWORD]}" | base64 -w0)"

mkdir -p "$(dirname "$OUT")"
{
  for k in $(printf '%s\n' "${!V[@]}" | sort); do
    printf '%s=%s\n' "$k" "${V[$k]}"
  done
} | sops --encrypt --age "$recipients" --input-type dotenv --output-type dotenv /dev/stdin > "$OUT"
chmod 0600 "$OUT"
echo "wrote $OUT (decryptable by: $recipients)" >&2
echo "next: escrow the keys listed in ops/KEYS.md, then ops/deploy/up.sh" >&2
