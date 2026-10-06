#!/usr/bin/env bash
# ##########################################################################################
# ##                                                                                      ##
# ##   STOP. BEFORE ANY PHI TOUCHES THIS PROJECT:                                         ##
# ##     1. Sign the Google Cloud Business Associate Agreement (BAA) for the billing /   ##
# ##        organisation account that owns this project (Console -> Compliance ->         ##
# ##        "Google Cloud BAA", or via your Google account team).                          ##
# ##     2. Use ONLY services listed as covered by the BAA (Compute Engine, Persistent   ##
# ##        Disk, Cloud Storage, Secret Manager, Vertex AI Gemini, Cloud Logging ... ).   ##
# ##        Google AI Studio / GEMINI_API_KEY is NOT covered — production uses           ##
# ##        GEMINI_BACKEND=vertex.                                                        ##
# ##     3. Keep PHI out of resource names, labels, metadata and log messages.            ##
# ##                                                                                      ##
# ##########################################################################################
#
# Provisions the single-VM production footprint for the MRCV platform. Idempotent-ish:
# every resource is created only if it does not exist yet (nothing is updated or deleted),
# so re-running after a partial failure is safe. Review, then run from an admin
# workstation with `gcloud auth login` as a project owner:
#
#   PROJECT_ID=my-proj SCANNER_CIDRS=203.0.113.10/32,198.51.100.0/28 \
#   BACKUP_BUCKET=my-proj-mrcv-backups bash ops/providers/gcp/provision.sh
#
# Creates:
#   * APIs: compute, aiplatform, secretmanager, storage, iap, logging, monitoring
#   * service account mrcv-vm (Vertex AI user, Secret accessor, log/metric writer,
#     objectAdmin on the backup bucket only)
#   * dedicated VPC + subnet (NOT the "default" network: it ships with a
#     0.0.0.0/0 SSH rule) and firewall rules:
#       tcp:80,443  from 0.0.0.0/0            (nginx; 80 only redirects / ACME)
#       tcp:4242    from ${SCANNER_CIDRS}     (DICOM C-STORE — plaintext, see runbook §6)
#       tcp:22      from 35.235.240.0/20      (IAP TCP forwarding only — no public SSH)
#   * static external IPv4 address
#   * SSD data disk (Docker data-root /var/lib/docker: Postgres, Orthanc, MinIO, models)
#     + daily snapshot schedule, 14-day retention
#   * GPU VM (default g2-standard-8 = 1x NVIDIA L4 24 GB), OS Login, Shielded VM,
#     startup script ops/providers/gcp/startup.sh (mounts data disk, installs Docker + NVIDIA)
#   * backup bucket: uniform access, public access prevention, versioning,
#     lifecycle delete after 35 days
#
# Encryption: Google-managed keys at rest for disks, snapshots, bucket and secrets
# (no CMEK by decision). In transit: TLS (nginx/Let's Encrypt), Google APIs over HTTPS.
set -Eeuo pipefail

# ── parameters ───────────────────────────────────────────────────────────────
: "${PROJECT_ID:?set PROJECT_ID}"
: "${SCANNER_CIDRS:?set SCANNER_CIDRS (comma-separated scanner/VPN egress CIDRs for DICOM 4242)}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"            # must offer the GPU: gcloud compute accelerator-types list --filter=zone:ZONE
NAME="${NAME:-mrcv}"                     # prefix for every resource
VM_NAME="${VM_NAME:-${NAME}-prod}"
# GPU sizing: g2-standard-8 = 8 vCPU / 32 GB / 1x L4 (24 GB). The worker (TotalSegmentator,
# SAM-Med3D, MONAI) and Ollama share that GPU; medgemma:27b alone needs ~17-20 GB, so for
# the 27B model use g2-standard-24 (2x L4) or a2-highgpu-1g (A100 40 GB). The 4B
# medgemma1.5 model fits on one L4 alongside the pipelines.
MACHINE_TYPE="${MACHINE_TYPE:-g2-standard-8}"
# G2 machine types have their L4(s) attached implicitly. For N1/A2 families set e.g.
# ACCELERATOR="type=nvidia-tesla-t4,count=1" (A2 also attach implicitly).
ACCELERATOR="${ACCELERATOR:-}"
# OS image. Default: plain Ubuntu 22.04 LTS; startup.sh installs the NVIDIA driver +
# container toolkit + Docker. Alternative: a Deep Learning VM image with the driver
# preinstalled, e.g. IMAGE_PROJECT=deeplearning-platform-release and an IMAGE_FAMILY from
#   gcloud compute images list --project deeplearning-platform-release \
#     --filter="family~ubuntu AND family~nvidia" --format="value(family)" | sort -u
# (startup.sh skips the driver install when nvidia-smi already works).
IMAGE_PROJECT="${IMAGE_PROJECT:-ubuntu-os-cloud}"
IMAGE_FAMILY="${IMAGE_FAMILY:-ubuntu-2204-lts}"
BOOT_DISK_SIZE="${BOOT_DISK_SIZE:-100GB}"
BOOT_DISK_TYPE="${BOOT_DISK_TYPE:-pd-balanced}"
DATA_DISK="${DATA_DISK:-${NAME}-data}"
DATA_DISK_SIZE="${DATA_DISK_SIZE:-1000GB}"   # DICOM archive + models + Postgres; grows online
DATA_DISK_TYPE="${DATA_DISK_TYPE:-pd-ssd}"
# Secure Boot needs signed NVIDIA modules (Ubuntu's ubuntu-drivers packages are signed);
# leave off unless you verified the driver loads with it on.
SECURE_BOOT="${SECURE_BOOT:-false}"
NETWORK="${NETWORK:-${NAME}-vpc}"
SUBNET="${SUBNET:-${NAME}-subnet}"
SUBNET_RANGE="${SUBNET_RANGE:-10.60.0.0/24}"
ADDRESS="${ADDRESS:-${NAME}-ip}"
SA_NAME="${SA_NAME:-${NAME}-vm}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
BACKUP_BUCKET="${BACKUP_BUCKET:-${PROJECT_ID}-${NAME}-backups}"
SNAPSHOT_POLICY="${SNAPSHOT_POLICY:-${NAME}-data-daily}"
IAP_RANGE="35.235.240.0/20"
TAG_WEB="${NAME}-web"; TAG_DICOM="${NAME}-dicom"; TAG_SSH="${NAME}-iap-ssh"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

G=(gcloud --project="${PROJECT_ID}" --quiet)
log() { printf '\n== %s\n' "$*"; }
exists() { "${G[@]}" "$@" >/dev/null 2>&1; }

# ── APIs ─────────────────────────────────────────────────────────────────────
log "enabling APIs"
"${G[@]}" services enable compute.googleapis.com aiplatform.googleapis.com \
    secretmanager.googleapis.com storage.googleapis.com iap.googleapis.com \
    logging.googleapis.com monitoring.googleapis.com

# ── service account ──────────────────────────────────────────────────────────
log "service account ${SA_EMAIL}"
exists iam service-accounts describe "${SA_EMAIL}" \
    || "${G[@]}" iam service-accounts create "${SA_NAME}" --display-name="MRCV production VM"
# Project-level roles. secretAccessor could instead be granted per secret
# (gcloud secrets add-iam-policy-binding mrcv-<VAR> ...) for least privilege.
for role in roles/aiplatform.user roles/secretmanager.secretAccessor \
            roles/logging.logWriter roles/monitoring.metricWriter; do
    "${G[@]}" projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="serviceAccount:${SA_EMAIL}" --role="${role}" --condition=None >/dev/null
done

# ── backup bucket ────────────────────────────────────────────────────────────
log "backup bucket gs://${BACKUP_BUCKET}"
if ! exists storage buckets describe "gs://${BACKUP_BUCKET}"; then
    "${G[@]}" storage buckets create "gs://${BACKUP_BUCKET}" --location="${REGION}" \
        --default-storage-class=STANDARD --uniform-bucket-level-access \
        --public-access-prevention
fi
lifecycle="$(mktemp)"; trap 'rm -f "${lifecycle}"' EXIT
cat > "${lifecycle}" <<'JSON'
{
  "rule": [
    {"action": {"type": "Delete"}, "condition": {"age": 35, "isLive": true}},
    {"action": {"type": "Delete"}, "condition": {"daysSinceNoncurrentTime": 7, "isLive": false}}
  ]
}
JSON
"${G[@]}" storage buckets update "gs://${BACKUP_BUCKET}" --versioning \
    --lifecycle-file="${lifecycle}" --public-access-prevention
# objectAdmin on THIS bucket only (spec). Hardening option: grant objectCreator +
# objectViewer instead and add a bucket retention policy so a compromised VM cannot
# delete backups.
"${G[@]}" storage buckets add-iam-policy-binding "gs://${BACKUP_BUCKET}" \
    --member="serviceAccount:${SA_EMAIL}" --role=roles/storage.objectAdmin >/dev/null

# ── network + firewall ───────────────────────────────────────────────────────
log "network ${NETWORK}"
exists compute networks describe "${NETWORK}" \
    || "${G[@]}" compute networks create "${NETWORK}" --subnet-mode=custom
exists compute networks subnets describe "${SUBNET}" --region="${REGION}" \
    || "${G[@]}" compute networks subnets create "${SUBNET}" --network="${NETWORK}" \
        --region="${REGION}" --range="${SUBNET_RANGE}" --enable-private-ip-google-access

fw() {  # fw NAME TAG PORTS SOURCES DESCRIPTION
    exists compute firewall-rules describe "$1" || "${G[@]}" compute firewall-rules create "$1" \
        --network="${NETWORK}" --direction=INGRESS --action=ALLOW --target-tags="$2" \
        --rules="$3" --source-ranges="$4" --description="$5"
}
fw "${NAME}-allow-web"   "${TAG_WEB}"   tcp:80,tcp:443 0.0.0.0/0        "HTTPS (80 = ACME + redirect)"
fw "${NAME}-allow-dicom" "${TAG_DICOM}" tcp:4242       "${SCANNER_CIDRS}" "DICOM C-STORE from scanners only"
fw "${NAME}-allow-iap-ssh" "${TAG_SSH}" tcp:22         "${IAP_RANGE}"     "SSH via IAP TCP forwarding only"
# Everything else inbound is denied by the VPC's implied deny-ingress rule.
# If SCANNER_CIDRS changes later:
#   gcloud compute firewall-rules update ${NAME}-allow-dicom --source-ranges=...

# ── static IP ────────────────────────────────────────────────────────────────
log "static address ${ADDRESS}"
exists compute addresses describe "${ADDRESS}" --region="${REGION}" \
    || "${G[@]}" compute addresses create "${ADDRESS}" --region="${REGION}" --network-tier=PREMIUM
IP="$("${G[@]}" compute addresses describe "${ADDRESS}" --region="${REGION}" --format='value(address)')"

# ── data disk + snapshot schedule ────────────────────────────────────────────
log "data disk ${DATA_DISK} + snapshot schedule ${SNAPSHOT_POLICY}"
exists compute resource-policies describe "${SNAPSHOT_POLICY}" --region="${REGION}" \
    || "${G[@]}" compute resource-policies create snapshot-schedule "${SNAPSHOT_POLICY}" \
        --region="${REGION}" --daily-schedule --start-time=03:00 \
        --max-retention-days=14 --on-source-disk-delete=keep-auto-snapshots \
        --storage-location="${REGION}" --description="Daily data-disk snapshot, 14 days"
exists compute disks describe "${DATA_DISK}" --zone="${ZONE}" \
    || "${G[@]}" compute disks create "${DATA_DISK}" --zone="${ZONE}" \
        --size="${DATA_DISK_SIZE}" --type="${DATA_DISK_TYPE}" \
        --resource-policies="${SNAPSHOT_POLICY}"
# Attach the schedule even if the disk pre-existed (no-op error if already attached).
"${G[@]}" compute disks add-resource-policies "${DATA_DISK}" --zone="${ZONE}" \
    --resource-policies="${SNAPSHOT_POLICY}" >/dev/null 2>&1 || true

# ── VM ───────────────────────────────────────────────────────────────────────
log "VM ${VM_NAME} (${MACHINE_TYPE}) in ${ZONE}"
if ! exists compute instances describe "${VM_NAME}" --zone="${ZONE}"; then
    accel=(); [ -n "${ACCELERATOR}" ] && accel=(--accelerator="${ACCELERATOR}")
    boot=(--shielded-vtpm --shielded-integrity-monitoring)
    [ "${SECURE_BOOT}" = true ] && boot+=(--shielded-secure-boot)
    "${G[@]}" compute instances create "${VM_NAME}" --zone="${ZONE}" \
        --machine-type="${MACHINE_TYPE}" "${accel[@]}" \
        --maintenance-policy=TERMINATE --restart-on-failure \
        --image-project="${IMAGE_PROJECT}" --image-family="${IMAGE_FAMILY}" \
        --boot-disk-size="${BOOT_DISK_SIZE}" --boot-disk-type="${BOOT_DISK_TYPE}" \
        --disk="name=${DATA_DISK},device-name=mrcv-data,mode=rw,boot=no,auto-delete=no" \
        --network="${NETWORK}" --subnet="${SUBNET}" --address="${IP}" \
        --tags="${TAG_WEB},${TAG_DICOM},${TAG_SSH}" \
        --service-account="${SA_EMAIL}" --scopes=cloud-platform \
        --metadata=enable-oslogin=TRUE,block-project-ssh-keys=TRUE,serial-port-enable=FALSE \
        --metadata-from-file=startup-script="${HERE}/startup.sh" \
        "${boot[@]}" \
        --labels=app=mrcv,env=prod
fi

cat <<EOF

Done. Static IP: ${IP}
Next (ops/providers/gcp/GCP_DEPLOYMENT.md):
  * DNS: A record ${IP} for your PUBLIC_DOMAIN (lower the TTL first on the old record)
  * Admin access: grant roles/iap.tunnelResourceAccessor + roles/compute.osAdminLogin to
    admins, then:  gcloud compute ssh ${VM_NAME} --zone ${ZONE} --tunnel-through-iap
  * Secrets:     ops/providers/gcp/GCP_DEPLOYMENT.md §3 (gcloud secrets create ${NAME}-<VAR> ...)
EOF
