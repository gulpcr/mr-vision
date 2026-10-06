#!/usr/bin/env bash
# GCE startup script for the MRCV VM (set by ops/providers/gcp/provision.sh as metadata
# "startup-script"; runs as root on EVERY boot, so every step is idempotent).
#
#  1. Formats (first boot only) and mounts the SSD data disk at /var/lib/docker, so all
#     Docker volumes (Postgres, Orthanc DICOM, MinIO, models, certificates) live on it.
#  2. Installs Docker Engine + compose plugin (>= 2.24.4 needed for !reset), the NVIDIA
#     driver (skipped if nvidia-smi already works, e.g. on a Deep Learning VM image) and
#     the NVIDIA container toolkit.
#  3. Installs the mrcv systemd units once the repo is present at /opt/mrcv.
# Logs: sudo journalctl -u google-startup-scripts.service
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive

DEV=/dev/disk/by-id/google-mrcv-data     # device-name=mrcv-data in provision.sh
MNT=/var/lib/docker
log() { echo "mrcv-startup: $*"; }

# ── 1. data disk ─────────────────────────────────────────────────────────────
if [ -b "${DEV}" ]; then
    if ! blkid "${DEV}" >/dev/null 2>&1; then
        log "formatting new data disk ${DEV}"
        mkfs.ext4 -m 0 -E lazy_itable_init=0,lazy_journal_init=0,discard -L mrcv-data "${DEV}"
    fi
    uuid="$(blkid -s UUID -o value "${DEV}")"
    mkdir -p "${MNT}"
    if ! grep -q "${uuid}" /etc/fstab; then
        # nofail: the VM still boots (for repair via IAP SSH) if the disk is missing.
        echo "UUID=${uuid} ${MNT} ext4 discard,defaults,nofail 0 2" >> /etc/fstab
    fi
    mountpoint -q "${MNT}" || mount "${MNT}"
    # Docker must never start on the boot disk by accident.
    mkdir -p /etc/systemd/system/docker.service.d
    cat > /etc/systemd/system/docker.service.d/10-mrcv-data-disk.conf <<'EOF'
[Unit]
RequiresMountsFor=/var/lib/docker
EOF
    systemctl daemon-reload
else
    log "WARNING: data disk ${DEV} not attached - Docker data would land on the boot disk"
fi

# ── 2. Docker + NVIDIA ───────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
    log "installing Docker Engine"
    apt-get update -q
    apt-get install -y -q ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -q
    apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    # Log rotation for anything not covered by the compose logging options.
    cat > /etc/docker/daemon.json <<'EOF'
{ "log-driver": "json-file", "log-opts": { "max-size": "50m", "max-file": "5" } }
EOF
    systemctl enable --now docker
fi

if ! nvidia-smi >/dev/null 2>&1; then
    log "installing NVIDIA driver (reboot follows)"
    apt-get install -y -q ubuntu-drivers-common linux-headers-"$(uname -r)"
    ubuntu-drivers install --gpgpu
    touch /var/lib/mrcv-needs-reboot
fi

if ! command -v nvidia-ctk >/dev/null 2>&1; then
    log "installing NVIDIA container toolkit"
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
        | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg --yes
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' \
        > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get update -q
    apt-get install -y -q nvidia-container-toolkit
    nvidia-ctk runtime configure --runtime=docker
    systemctl restart docker
fi

if [ -f /var/lib/mrcv-needs-reboot ]; then
    rm -f /var/lib/mrcv-needs-reboot
    log "rebooting to load the NVIDIA driver"
    systemctl reboot
    exit 0
fi

# ── 3. systemd units (after the repo is cloned to /opt/mrcv) ─────────────────
if [ -d /opt/mrcv/ops/systemd ]; then
    changed=0
    for unit in /opt/mrcv/ops/systemd/*.service /opt/mrcv/ops/systemd/*.timer; do
        dst="/etc/systemd/system/$(basename "${unit}")"
        if ! cmp -s "${unit}" "${dst}"; then install -m 0644 "${unit}" "${dst}"; changed=1; fi
    done
    [ "${changed}" = 1 ] && systemctl daemon-reload
    if [ -r /etc/mrcv/mrcv.env ]; then
        systemctl enable mrcv-render-env.service mrcv-stack.service >/dev/null
    fi
    if [ -r /etc/mrcv/backup.env ]; then
        systemctl enable --now mrcv-backup.timer >/dev/null
    fi
fi
log "done"
