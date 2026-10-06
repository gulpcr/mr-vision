#!/usr/bin/env bash
# Prepare a fresh Linux GPU host (Ubuntu 22.04 / 24.04) for the platform — any provider.
#
#   sudo ops/deploy/bootstrap.sh --data-device /dev/nvme1n1 --ssh-cidr 198.51.100.7/32 [--yes]
#   sudo ops/deploy/bootstrap.sh --loopfile 500G             --ssh-cidr 198.51.100.7/32
#
#   --data-device DEV   an empty second disk: becomes a LUKS2 volume (ALL DATA ON IT IS LOST)
#   --loopfile SIZE     no second disk: a LUKS2 container file /var/lib/mrcv-data.img
#   --ssh-cidr CIDR     who may reach SSH (repeatable). Without it SSH stays open (WARN).
#   --vpn               also open udp/51820 for the scanner WireGuard tunnel
#   --fips              FIPS 140-validated crypto modules (Ubuntu Pro "fips-updates":
#                       kernel crypto for LUKS, OpenSSL). Needs UBUNTU_PRO_TOKEN; reboot after.
#   --yes               do not ask before formatting
#
# What it does (each step is skipped if already done):
#   1. packages: Docker Engine + compose plugin, NVIDIA container toolkit (if a GPU is
#      present), cryptsetup, ufw, unattended-upgrades, chrony, age, sops, rclone, jq;
#   2. encrypted data volume (LUKS2, AES-XTS) mounted at /srv/mrcv, unlocked at boot with
#      a root-only key file on the boot disk (/etc/mrcv/luks.key — see ops/KEYS.md for
#      the recovery passphrase and the network-bound Clevis/Tang alternative);
#   3. Docker's data-root moved onto it: every volume (Postgres, Redis, MinIO, Orthanc
#      index, worker scratch, container logs and env) is encrypted at rest;
#   4. firewall: deny inbound except SSH (from --ssh-cidr), 80/443 and the DICOM port
#      (restricted to SCANNER_CIDRS by docker-firewall.sh);
#   5. automatic security updates, time sync;
#   6. internal PKI (ops/pki/make-certs.sh) and this host's age key for SOPS;
#   7. systemd units: stack, backup timer, DICOM firewall.
set -Eeuo pipefail
umask 022

DATA_DEV=""; LOOP_SIZE=""; YES=0; VPN=0; FIPS=0
SSH_CIDRS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --data-device) DATA_DEV="$2"; shift 2 ;;
    --loopfile) LOOP_SIZE="$2"; shift 2 ;;
    --ssh-cidr) SSH_CIDRS+=("$2"); shift 2 ;;
    --vpn) VPN=1; shift ;;
    --fips) FIPS=1; shift ;;
    --yes) YES=1; shift ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SRV=/srv/mrcv
ETC=/etc/mrcv
MAPPER=mrcv_data
LOOPFILE=/var/lib/mrcv-data.img
SOPS_VERSION="${SOPS_VERSION:-3.9.1}"

log() { printf '\n== %s\n' "$*"; }
die() { printf 'bootstrap: ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"
. /etc/os-release
[ "${ID:-}" = ubuntu ] || echo "bootstrap: WARNING: tested on Ubuntu; continuing on ${PRETTY_NAME:-unknown}"
if [ -z "$DATA_DEV" ] && [ -z "$LOOP_SIZE" ] && ! [ -e "/dev/mapper/$MAPPER" ]; then
  die "give --data-device DEV or --loopfile SIZE (the data volume must be encrypted)"
fi
install -d -m 0700 "$ETC"

# ── 1. packages ──────────────────────────────────────────────────────────────
log "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q ca-certificates curl gnupg cryptsetup ufw unattended-upgrades chrony \
  age rclone jq openssl python3 lsb-release
if ! command -v docker >/dev/null; then
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
if ! command -v sops >/dev/null; then
  arch="$(dpkg --print-architecture)"
  url="https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}"
  curl -fsSLo /tmp/sops "${url}/sops-v${SOPS_VERSION}.linux.${arch}"
  curl -fsSLo /tmp/sops.checksums "${url}/sops-v${SOPS_VERSION}.checksums.txt"
  (cd /tmp && grep " sops-v${SOPS_VERSION}.linux.${arch}\$" sops.checksums | sed "s| sops-v.*| sops|" | sha256sum -c -)
  install -m 0755 /tmp/sops /usr/local/bin/sops
  rm -f /tmp/sops /tmp/sops.checksums
fi
if lspci 2>/dev/null | grep -qi nvidia && ! command -v nvidia-ctk >/dev/null; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor -o /etc/apt/keyrings/nvidia-container-toolkit.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/etc/apt/keyrings/nvidia-container-toolkit.gpg] https://#' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -q
  apt-get install -y -q nvidia-container-toolkit
  nvidia-ctk runtime configure --runtime=docker
  command -v nvidia-smi >/dev/null || echo "bootstrap: NOTE: install the NVIDIA driver (ubuntu-drivers install) and reboot"
fi
docker compose version >/dev/null || die "docker compose plugin missing"

# ── 1b. FIPS 140 modules (checklist TEC-04) ─────────────────────────────────
if [ "$FIPS" = 1 ]; then
  log "FIPS 140 validated cryptographic modules"
  : "${UBUNTU_PRO_TOKEN:?--fips needs UBUNTU_PRO_TOKEN (Ubuntu Pro subscription)}"
  command -v pro >/dev/null || apt-get install -y -q ubuntu-advantage-tools
  pro status --format json 2>/dev/null | grep -q '"attached": true' || pro attach "$UBUNTU_PRO_TOKEN"
  pro enable fips-updates --assume-yes
  if [ "$(cat /proc/sys/crypto/fips_enabled 2>/dev/null)" != 1 ]; then
    echo "bootstrap: FIPS modules installed - REBOOT now, then run this command again"
    echo "           (after reboot: cat /proc/sys/crypto/fips_enabled -> 1)"
    exit 0
  fi
fi
# FIPS mode: PBKDF2 for the LUKS key slot (argon2id is not a FIPS-approved KDF).
PBKDF=argon2id
[ "$(cat /proc/sys/crypto/fips_enabled 2>/dev/null)" = 1 ] && PBKDF=pbkdf2

# ── 2. encrypted data volume ─────────────────────────────────────────────────
log "encrypted data volume"
if ! [ -e "/dev/mapper/$MAPPER" ]; then
  if [ -n "$LOOP_SIZE" ]; then
    [ -e "$LOOPFILE" ] || fallocate -l "$LOOP_SIZE" "$LOOPFILE"
    chmod 0600 "$LOOPFILE"
    target="$LOOPFILE"
  else
    [ -b "$DATA_DEV" ] || die "$DATA_DEV is not a block device"
    if lsblk -no MOUNTPOINT "$DATA_DEV" | grep -q .; then die "$DATA_DEV (or a partition) is mounted"; fi
    target="$DATA_DEV"
  fi
  if ! cryptsetup isLuks "$target" 2>/dev/null; then
    if [ "$YES" != 1 ]; then
      read -r -p "Format $target as LUKS2? EVERYTHING ON IT IS LOST. Type yes: " a
      [ "$a" = yes ] || die "aborted"
    fi
    [ -f "$ETC/luks.key" ] || { head -c 64 /dev/urandom > "$ETC/luks.key"; chmod 0400 "$ETC/luks.key"; }
    cryptsetup luksFormat --type luks2 --cipher aes-xts-plain64 --key-size 512 --pbkdf "$PBKDF" \
      --batch-mode --key-file "$ETC/luks.key" "$target"
    echo "bootstrap: add a recovery passphrase now and escrow it (ops/KEYS.md):"
    echo "           cryptsetup luksAddKey --key-file $ETC/luks.key $target"
  fi
  cryptsetup open --key-file "$ETC/luks.key" "$target" "$MAPPER"
  blkid "/dev/mapper/$MAPPER" >/dev/null 2>&1 || mkfs.ext4 -q -L mrcv-data "/dev/mapper/$MAPPER"
  if [ -n "$LOOP_SIZE" ]; then
    # crypttab handles a file path by attaching a loop device itself.
    grep -q "^$MAPPER " /etc/crypttab || echo "$MAPPER $LOOPFILE $ETC/luks.key luks,discard" >> /etc/crypttab
  else
    uuid="$(cryptsetup luksUUID "$target")"
    grep -q "^$MAPPER " /etc/crypttab || echo "$MAPPER UUID=$uuid $ETC/luks.key luks,discard" >> /etc/crypttab
  fi
fi
install -d -m 0755 "$SRV"
grep -q " $SRV " /etc/fstab || echo "/dev/mapper/$MAPPER $SRV ext4 defaults,noatime 0 2" >> /etc/fstab
mountpoint -q "$SRV" || mount "$SRV"
lsblk -sno TYPE "$(findmnt -no SOURCE -T "$SRV")" | grep -qx crypt || die "$SRV is not on the encrypted volume"
install -d -m 0755 "$SRV/docker" "$SRV/tls"
install -d -m 0700 "$SRV/backups"

# ── 3. Docker data-root on the encrypted volume ──────────────────────────────
log "docker data-root"
want="$SRV/docker"
current="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || true)"
if [ "$current" != "$want" ]; then
  if [ -n "$current" ] && [ -d "$current" ] && [ -n "$(ls -A "$current/volumes" 2>/dev/null | grep -v metadata.db)" ]; then
    die "Docker already has volumes in $current; move them deliberately (ops/migrate/README.md)"
  fi
  systemctl stop docker docker.socket 2>/dev/null || true
  python3 - "$want" <<'PY'
import json, os, sys
p = "/etc/docker/daemon.json"
cfg = json.load(open(p)) if os.path.exists(p) else {}
cfg.update({
    "data-root": sys.argv[1],
    "log-driver": "json-file",
    "log-opts": {"max-size": "50m", "max-file": "5"},
    "live-restore": True,
})
os.makedirs("/etc/docker", exist_ok=True)
json.dump(cfg, open(p, "w"), indent=2)
PY
  install -d /etc/systemd/system/docker.service.d
  printf '[Unit]\nRequiresMountsFor=%s\n' "$SRV" > /etc/systemd/system/docker.service.d/10-mrcv-data.conf
  systemctl daemon-reload
  systemctl start docker
fi
[ "$(docker info -f '{{.DockerRootDir}}')" = "$want" ] || die "docker data-root is not $want"

# ── 4. firewall ──────────────────────────────────────────────────────────────
log "firewall"
ufw --force default deny incoming >/dev/null
ufw --force default allow outgoing >/dev/null
if [ ${#SSH_CIDRS[@]} -gt 0 ]; then
  for c in "${SSH_CIDRS[@]}"; do ufw allow from "$c" to any port 22 proto tcp comment "mrcv ssh" >/dev/null; done
  ufw delete allow 22/tcp >/dev/null 2>&1 || true
  ufw delete allow OpenSSH >/dev/null 2>&1 || true
else
  ufw allow 22/tcp comment "mrcv ssh (restrict with --ssh-cidr)" >/dev/null
fi
ufw allow 80/tcp comment "mrcv http (ACME + redirect)" >/dev/null
ufw allow 443/tcp comment "mrcv https" >/dev/null
[ "$VPN" = 1 ] && ufw allow 51820/udp comment "mrcv scanner wireguard" >/dev/null
ufw --force enable >/dev/null
ufw status verbose | sed 's/^/  /'

# ── 5. updates + time ────────────────────────────────────────────────────────
log "updates and time"
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
  > /etc/apt/apt.conf.d/20auto-upgrades
systemctl enable --now unattended-upgrades chrony >/dev/null 2>&1 || true

# ── 6. PKI + age key ─────────────────────────────────────────────────────────
log "internal PKI and secrets key"
sh "$REPO/ops/pki/make-certs.sh" "$SRV/pki"
if [ ! -f "$ETC/age.key" ]; then
  age-keygen -o "$ETC/age.key" 2>/dev/null
  chmod 0400 "$ETC/age.key"
fi
echo "  host age public key: $(age-keygen -y "$ETC/age.key")"
[ -f "$ETC/mrcv.env" ] || { install -m 0600 "$REPO/ops/deploy/mrcv.env.example" "$ETC/mrcv.env"; echo "  created $ETC/mrcv.env - edit it"; }

# ── 7. systemd units ─────────────────────────────────────────────────────────
log "systemd units"
for u in mrcv-stack.service mrcv-backup.service mrcv-backup.timer mrcv-docker-firewall.service; do
  sed "s|/opt/mrcv|$REPO|g" "$REPO/ops/systemd/$u" > "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable mrcv-docker-firewall.service mrcv-stack.service mrcv-backup.timer >/dev/null

cat <<EOF

bootstrap complete. Next (ops/deploy/README.md):
  1. edit $ETC/mrcv.env (PUBLIC_DOMAIN, SCANNER_CIDRS, BACKUP_* ...)
  2. ops/deploy/secrets-init.sh [--import OLD_ENV] --recipient <escrow age key>
  3. escrow the keys in ops/KEYS.md (LUKS recovery passphrase, age keys)
  4. point DNS at this host, then: ops/deploy/up.sh --build --first-deploy
EOF
