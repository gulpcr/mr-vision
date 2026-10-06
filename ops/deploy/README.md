# Production deployment — any provider

Everything that makes the platform HIPAA-ready at the infrastructure level is in this
repository; deploy day is six steps. The same procedure works on any Linux GPU host
(AWS, Azure, GCP, Hetzner, on-premises). Provider-specific notes: [`ops/providers/`](../providers/README.md).

| Control | How | Where |
|---|---|---|
| Encryption at rest — all data | LUKS2 (AES-XTS-512) volume holding Docker's data-root | `bootstrap.sh` |
| Encryption at rest — DICOM | Orthanc client-side AES-256 before upload + MinIO SSE | `orthanc/orthanc.prod.json`, `minio/init.sh` |
| Encryption at rest — artifacts | MinIO SSE-S3 (bucket default) | `minio/init.sh` |
| Encryption at rest — backups | age (offline key) + rclone crypt, provider sees ciphertext only | `ops/backup.sh` |
| Encryption in transit — internal | TLS on every hop, verified against an internal CA; plaintext refused | `docker-compose.prod.yml`, `ops/pki/` |
| Encryption in transit — public | TLS 1.2/1.3 (Let's Encrypt or your certificate), HSTS | `nginx/templates-tls/` |
| Encryption in transit — scanners | mutual DICOM-TLS (or WireGuard) | `SCANNERS.md`, `ops/vpn/wireguard/` |
| Secrets | SOPS + age, decrypted to tmpfs only | `secrets-init.sh`, `up.sh` |
| Destinations | external AI only with `EXTERNAL_AI_BAA_CONFIRMED`; webhooks/FHIR only to `OUTBOUND_ALLOWED_HOSTS` | app settings |
| Startup gate | app refuses insecure settings (`PRODUCTION_MODE`); `preflight.sh` checks the host | `config.py`, `preflight.sh` |

## Before deploy day

* A **BAA** with the hosting provider (and with the backup storage provider, and Google if
  Gemini will be used). Without a BAA for Gemini leave `EXTERNAL_AI_BAA_CONFIRMED=false`:
  the platform then uses only the local MedGemma.
* An **offline backup key** (on a laptop or USB stick kept off the server):
  `age-keygen -o backup-age.key` → keep the file offline, note its public key `age1...`.
  Optionally a second **escrow key** the same way for the secrets file (ops/KEYS.md).
* The host: Ubuntu 22.04/24.04, NVIDIA GPU, a **second empty disk** for data (or use a
  loop file), a DNS name, SSH from your admin address.

## Deploy day

```bash
# 1. host: checkout + bootstrap (encrypted volume, Docker, NVIDIA toolkit, firewall, PKI, units)
sudo git clone <private repo> /opt/mrcv && cd /opt/mrcv
sudo ops/deploy/bootstrap.sh --data-device /dev/nvme1n1 --ssh-cidr <your-ip>/32
sudo cryptsetup luksAddKey --key-file /etc/mrcv/luks.key /dev/nvme1n1   # recovery passphrase → escrow

# 2. settings (non-secret)
sudo nano /etc/mrcv/mrcv.env          # PUBLIC_DOMAIN, SCANNER_CIDRS, BACKUP_*, RCLONE_CONFIG_OFFSITE_*

# 3. secrets (generated, or imported from the current deployment's .env)
sudo ops/deploy/secrets-init.sh --import /path/to/old/.env \
     --recipient "$(sudo age-keygen -y /etc/mrcv/age.key)" --recipient age1<escrow key>
sudo SOPS_AGE_KEY_FILE=/etc/mrcv/age.key sops /etc/mrcv/secrets.enc.env   # add RCLONE_CONFIG_OFFSITE_ credentials

# 4. DNS: A record PUBLIC_DOMAIN → this host (Let's Encrypt needs it; skip with your own cert)

# 5. start (builds the images, runs migrations, issues the certificate)
sudo ops/deploy/up.sh --build --first-deploy

# 6. migrate data from the old host (if any) and verify — ops/migrate/README.md
sudo systemctl start mrcv-backup && sudo ops/deploy/preflight.sh     # must end "0 FAIL"
```

After step 5: `https://PUBLIC_DOMAIN/health` reports database, Redis and MinIO `ok`; sign
in as the platform admin; the backend log shows no `insecure_config` line and
`config_warning` only for things you intend (e.g. external AI off).

## Every boot

`mrcv-docker-firewall.service` restricts the DICOM port, then `mrcv-stack.service` runs
`up.sh` (decrypt to tmpfs → preflight → compose up). Secrets never exist on disk in
plaintext; `/run/mrcv` vanishes at power-off.

## Routine operations

| Task | Command |
|---|---|
| Status | `cd /opt/mrcv && sudo docker compose --env-file /run/mrcv/.env -f docker-compose.yml -f docker-compose.prod.yml ps` |
| Update the code | `git pull && sudo ops/deploy/up.sh --build` |
| Compliance check | `sudo ops/deploy/preflight.sh` |
| Backup now / log | `sudo systemctl start mrcv-backup` / `journalctl -u mrcv-backup` |
| Monthly restore drill | `sudo ops/restore.sh --drill --identity /media/usb/backup-age.key` |
| Monthly audit review | UI → Audit Review (generated on the 1st) |
| Edit a secret | `sudo SOPS_AGE_KEY_FILE=/etc/mrcv/age.key sops /etc/mrcv/secrets.enc.env && sudo ops/deploy/up.sh` |
| Renew internal certificates | `sudo ops/pki/make-certs.sh /srv/mrcv/pki && sudo ops/deploy/up.sh` (reissues those < 30 days) |
| Onboard a scanner | [`SCANNERS.md`](SCANNERS.md) |
| Rotate a key | [`ops/KEYS.md`](../KEYS.md) |

## Testing without a server

The TLS stack can be brought up on a workstation with a throwaway project name and
certificates from `ops/pki/make-certs.sh <dir>` — no published ports collide with a
running development stack (only the DICOM port, 2762):

```bash
docker compose -p mrcvtls --env-file test.env -f docker-compose.yml -f docker-compose.prod.yml \
  up -d postgres pg-init redis minio minio-init orthanc
```

`test.env` sets `MRCV_PKI_DIR`, `MRCV_RUNTIME_DIR` (with `orthanc-keys/master.key` and
`storage-keys.json`) and every variable the compose file marks required. The backend test
suites run against it with `DB_SSL_MODE=verify-full REDIS_TLS=true MINIO_SECURE=true
ORTHANC_SCHEME=https` and the CA mounted (see `backend/tests/rls`).
