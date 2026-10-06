# GCP deployment runbook (provider notes)

> **Start with [`ops/deploy/README.md`](../../deploy/README.md)** — the provider-neutral
> deploy (bootstrap with an encrypted data volume, internal TLS, SOPS secrets, preflight,
> `up.sh`). This file keeps only what is specific to Google Cloud: `provision.sh` /
> `startup.sh` to create the VM, disk, snapshots, firewall and bucket, and the optional
> Secret Manager path (`render-env.sh`) instead of SOPS. The compose files are
> `docker-compose.yml` + `docker-compose.prod.yml` + this directory's
> `docker-compose.gcp.yml` overlay (Gemini via Vertex AI).

Target: **one GPU Compute Engine VM** running the existing docker-compose stack with the
production overrides `docker-compose.prod.yml` + `ops/providers/gcp/docker-compose.gcp.yml`. Encryption at rest: Google-managed keys
(persistent disks, snapshots, Cloud Storage, Secret Manager). Encryption in transit:
TLS 1.2/1.3 at nginx (Let's Encrypt), HTTPS to every Google API, IAP TLS tunnel for
admin SSH. Work through the sections **in order**; do not skip §1.

| File | Purpose |
|---|---|
| `docker-compose.prod.yml` + `ops/providers/gcp/docker-compose.gcp.yml` | generic production override (TLS everywhere, encrypted storage, certbot, ollama, no internal ports) + the Vertex Gemini overlay |
| `nginx/templates-tls/`, `nginx/templates-bootstrap/`, `nginx/snippets/`, `nginx/tls-entrypoint.sh` | TLS config, ACME-only bootstrap, security headers, cert watcher |
| `ops/providers/gcp/provision.sh` (+ `startup.sh`) | gcloud provisioning (VM, disk, snapshots, firewall, bucket, SA) |
| `ops/providers/gcp/render-env.sh`, `ops/providers/gcp/mrcv.env.example` | boot-time `.env` from Secret Manager |
| `ops/backup.sh`, `ops/systemd/*` | nightly pg_dump to GCS; boot units |
| `ops/providers/gcp/migrate-data.md` | moving DB / Orthanc / MinIO off the Windows host |

---

## 1. BAA and scope (blocking)

1. Sign the **Google Cloud BAA** for the organisation / billing account that will own
   the project. No PHI goes into the project before this is done.
2. Use only BAA-covered services. This design uses Compute Engine, Persistent Disk,
   Cloud Storage, Secret Manager, Vertex AI (Gemini), Cloud Logging/Monitoring, IAP.
   **Google AI Studio (`GEMINI_API_KEY`) is not covered** — production runs
   `GEMINI_BACKEND=vertex` (forced by the compose override). Keep the API-key path for
   local development only, with de-identified data.
3. Confirm the Gemini model you use is offered on Vertex AI in `GCP_LOCATION`
   (`GEMINI_MODEL`, default `gemini-2.5-flash`) and that data-residency needs match
   the region.
4. Keep PHI out of resource names, labels, metadata and log lines.

## 2. Provision

From an admin workstation (project owner, `gcloud auth login`):

```bash
PROJECT_ID=my-proj REGION=us-central1 ZONE=us-central1-a \
SCANNER_CIDRS=203.0.113.10/32 BACKUP_BUCKET=my-proj-mrcv-backups \
bash ops/providers/gcp/provision.sh
```

Read the script's header first: machine type / GPU sizing (L4 24 GB is shared by the
worker and Ollama — `medgemma:27b` needs a bigger GPU), image choice (Ubuntu + driver
install vs Deep Learning VM image), data-disk size. The VM has an external IP for
80/443/4242 only; SSH is reachable **only** through IAP:

```bash
gcloud compute ssh mrcv-prod --zone us-central1-a --tunnel-through-iap
```

Grant admins `roles/iap.tunnelResourceAccessor` and `roles/compute.osAdminLogin`
(OS Login is enforced; project SSH keys are blocked). On first boot `startup.sh`
formats/mounts the data disk at `/var/lib/docker`, installs Docker + NVIDIA driver +
container toolkit and reboots once. Check: `nvidia-smi`, `docker compose version`
(**must be ≥ 2.24.4** — the override uses `!reset` / `!override`), `df -h /var/lib/docker`.

## 3. Secrets

One Secret Manager secret per variable, id `mrcv-<VAR>`, regional replication. Values are
fed through **stdin** so they never appear in shell history or `ps`:

```bash
REGION=us-central1
mk() { gcloud secrets create "mrcv-$1" --replication-policy=user-managed --locations="$REGION" --data-file=- ; }
# brand-new random values (only for secrets that have NO existing data depending on them):
openssl rand -hex 32 | tr -d '\n' | mk REDIS_PASSWORD      # must stay URL-safe (hex)
```

**Migrating an existing install: copy the values from the old host's `.env`
unchanged** for `JWT_SECRET_KEY` (MFA-secret encryption, audit chain, tenant JWTs),
`PHI_HASH_SALT`, `POSTGRES_PASSWORD`, `POSTGRES_APP_PASSWORD`, `MINIO_SECRET_KEY`,
`ORTHANC_PASSWORD`. From Git Bash on the old host, without echoing values:

```bash
for k in POSTGRES_PASSWORD POSTGRES_APP_PASSWORD REDIS_PASSWORD MINIO_SECRET_KEY \
         ORTHANC_PASSWORD ORTHANC_WEBHOOK_SECRET JWT_SECRET_KEY PHI_HASH_SALT SECRET_KEY; do
  grep -E "^$k=" .env | tail -n1 | cut -d= -f2- | tr -d '\r\n' | mk "$k"
done
```

(Strip surrounding quotes first if the old `.env` quotes values.) Required secrets:
`POSTGRES_PASSWORD POSTGRES_APP_PASSWORD REDIS_PASSWORD MINIO_SECRET_KEY ORTHANC_PASSWORD
ORTHANC_WEBHOOK_SECRET JWT_SECRET_KEY PHI_HASH_SALT SECRET_KEY`. Optional:
`ORTHANC_USERNAME MINIO_ACCESS_KEY API_KEY HF_TOKEN AUDIT_CHAIN_PREVIOUS_MASTER_KEY
ORTHANC_DICOM_MODALITIES`. Derived at render time (do not create): `CELERY_BROKER_URL`,
`CELERY_RESULT_BACKEND`, `ORTHANC_BASIC_AUTH`. Never create `GEMINI_API_KEY`.

On the VM:

```bash
sudo mkdir -p /etc/mrcv
sudo cp /opt/mrcv/ops/providers/gcp/mrcv.env.example /etc/mrcv/mrcv.env   # after §4 clone; then edit
sudo bash /opt/mrcv/ops/providers/gcp/render-env.sh                       # -> /opt/mrcv/.env (0600)
```

`render-env.sh` refuses to run if a secret name appears in the plain settings file.

## 4. Deploy

Deploy from a **committed, pushed** revision (the working copy on the old host has
uncommitted changes — commit/tag them first).

```bash
sudo git clone <repo-url> /opt/mrcv && cd /opt/mrcv && sudo git checkout <tag>
sudo bash ops/providers/gcp/startup.sh            # installs the systemd units now that the repo exists
C="docker compose -f docker-compose.yml -f docker-compose.prod.yml -f ops/providers/gcp/docker-compose.gcp.yml"
sudo $C config -q                        # must print nothing
sudo $C build
sudo $C up --no-start                    # creates volumes; migrate data now (§6) if moving
sudo systemctl enable --now mrcv-render-env.service mrcv-stack.service
sudo $C exec ollama ollama pull <MEDGEMMA_MODEL>   # only if MEDGEMMA_ENABLED=true
```

Production settings enforced by the override: `ENFORCE_SECURE_CONFIG=true`,
`API_DOCS_ENABLED=false`, `VIEWER_COOKIE_SECURE=true`,
`ALLOWED_ORIGINS=https://${PUBLIC_DOMAIN}`, `GEMINI_BACKEND=vertex`,
`OLLAMA_BASE_URL=http://ollama:11434`, and **`LOGIN_IP_ATTEMPTS_PER_MINUTE=20`** — on
Linux nginx sees the real client IP (Docker Desktop NATed everyone to one address), so
the per-IP login limit can be strict again. Only 80, 443 and 4242 are published;
Postgres / Redis / MinIO / Orthanc HTTP / backend are reachable only inside the compose
network (`sudo $C exec postgres psql ...`, or an IAP tunnel + `docker compose exec`).
The backend runs from the image (no bind mount, no `--reload`) with
`--proxy-headers --forwarded-allow-ips='*'`; client IPs for rate limits and audit come
from nginx's `X-Real-IP`.

Backups: `echo BACKUP_BUCKET=my-proj-mrcv-backups | sudo tee /etc/mrcv/backup.env`, then
`sudo systemctl enable --now mrcv-backup.timer`; test once with
`sudo systemctl start mrcv-backup.service && journalctl -u mrcv-backup -n 20`.

## 5. TLS

- nginx starts in **bootstrap mode** (ACME challenge only, everything else 503) until
  `/etc/letsencrypt/live/$PUBLIC_DOMAIN/` exists; the `certbot` service issues the
  certificate over the webroot challenge as soon as DNS points at the VM, then renews
  every 12 h. nginx's watcher restarts it into TLS mode / reloads on renewal.
- Dry run first with `CERTBOT_STAGING=1` in `/etc/mrcv/mrcv.env` if you want; then
  delete the staging cert (`$C run --rm --entrypoint certbot certbot delete
  --cert-name $PUBLIC_DOMAIN`), set it back to 0, re-render and restart.
- Policy: TLS 1.2 + 1.3, ECDHE AEAD ciphers, HSTS (1 year, no includeSubDomains until
  every tenant subdomain is HTTPS), `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: strict-origin-when-cross-origin`,
  `Content-Security-Policy: frame-ancestors 'self'` + `X-Frame-Options: SAMEORIGIN`
  (the OHIF iframe is same-origin, so it keeps working), HTTP → HTTPS 301 except
  `/.well-known/acme-challenge/`.
- The dev/live HTTP config (`nginx/templates/`) is unchanged; only the GCP override
  mounts the TLS templates.

## 6. Migrate data — and encrypt DICOM

Follow `ops/providers/gcp/migrate-data.md` (freeze → export → HTTPS/IAP transfer → checksum →
import → verify → certificate → DNS switch).

**DICOM on port 4242 is plaintext.** Firewalling it to `SCANNER_CIDRS` limits *who* can
connect but does not encrypt PHI crossing the internet. Before real studies flow, use
one of:
1. **Cloud VPN (HA VPN) from the hospital network** into the VPC, and change the
   `mrcv-allow-dicom` rule's source ranges to the on-prem ranges seen through the VPN
   (preferred; works with every scanner).
2. **DICOM TLS** in Orthanc (`DicomTlsEnabled`, certificate/key, trusted CA; e.g. env
   `ORTHANC__DICOM_TLS_ENABLED=true` + mounted cert files) — only if every modality
   supports DICOM TLS.
3. An on-prem DICOM router that forwards to the VM over TLS or the VPN.

## 7. Verify

```bash
D=your.domain
curl -sI http://$D/            | head -3   # 301 -> https://$D/
curl -sI http://$D/.well-known/acme-challenge/x | head -1   # 404 from nginx, not a redirect
curl -sI https://$D/           # 200 + strict-transport-security, x-content-type-options,
                               #   referrer-policy, content-security-policy: frame-ancestors 'self'
curl -s  https://$D/health     # {"status": ...} deep health (DB/Redis/MinIO)
curl -sI https://$D/docs | head -1          # 404 (API docs disabled)
curl -s --tls-max 1.1 https://$D/ -o /dev/null || echo "TLS<=1.1 refused (good)"
docker run --rm drwetter/testssl.sh --quiet --severity MEDIUM https://$D/   # expect no findings >= MEDIUM
nmap -Pn -p 1-65535 --open $D   # from OUTSIDE the scanner CIDRs: only 80,443 open
                                # (22 and 4242 filtered; 4242 open only from SCANNER_CIDRS)
```

In the browser: log in (the `mrv_viewer` cookie must be `Secure; HttpOnly`), open a study
in the embedded OHIF viewer (no mixed-content or frame errors in the console), watch a
job's live progress (WebSocket over `wss://`), download a PDF report.

Vertex AI: run a job with an LLM feature enabled, or in the backend container:
`python -c "import asyncio;from app.config import get_settings as s;from app.infrastructure.llm.gemini_client import GeminiClient as G;c=G(s().gemini_api_key,s().gemini_model);print(c.backend,c.ready,asyncio.run(c.generate_text('Reply with {\"ok\":true}')))"`
→ `vertex True {"ok":true}` (no PHI in the test prompt).

**Restore drill** (do it now, then quarterly):

```bash
f=$(gcloud storage ls "gs://$BACKUP_BUCKET/db/**.dump" | sort | tail -n1)
gcloud storage cp "$f" "$f.sha256" /var/tmp/ && cd /var/tmp && sha256sum -c "$(basename "$f").sha256"
docker run -d --rm --name restore-drill -e POSTGRES_PASSWORD=drill postgres:16-alpine
sleep 5; docker exec -i restore-drill pg_restore -U postgres -d postgres --no-owner < "/var/tmp/$(basename "$f")"
docker exec restore-drill psql -U postgres -Atc "select count(*) from studies"
docker stop restore-drill; shred -u "/var/tmp/$(basename "$f")"*
```

Also test restoring a data-disk snapshot to a new disk once (Console → Snapshots →
Create disk) to confirm Orthanc/MinIO volumes come back.

## 8. Operate

- Updates: `cd /opt/mrcv && git pull && $C build && $C up -d` (backend runs migrations).
- Logs: `$C logs -f backend worker nginx`; container logs rotate at 5 × 50 MB.
- Secrets change: update the secret version, `sudo systemctl restart mrcv-render-env
  mrcv-stack` (and `$C up -d` to recreate affected containers). Rotate `JWT_SECRET_KEY`
  only with `backend/scripts/rotate_master_key.py`.
- Snapshots: daily 03:00, 14-day retention; DB dumps: daily 02:30, 35-day lifecycle.

## 9. Decommission the old Windows host

Only after §7 passes and at least one nightly backup + restore drill succeeded:

1. Revoke what the old host held: delete the AI Studio `GEMINI_API_KEY`, rotate
   `HF_TOKEN`, remove the old host's firewall/NAT forwards and DNS records, revoke any
   gcloud credentials used for the migration (`gcloud auth revoke`).
2. Remove the stack and its data: `docker compose down -v` (deletes the named volumes),
   then Docker Desktop → Troubleshoot → *Clean / Purge data* (removes the WSL2 disk image
   that held the volumes).
3. Delete the DICOM source folders, `C:\mrcv-migration`, any `pg_dump` files and the
   old `.env`; wipe free space (`cipher /w:C:\`).
4. SSDs do not reliably overwrite in place: for assurance follow **NIST SP 800-88**
   (BitLocker-encrypted drive → crypto-erase / manufacturer secure erase, or physical
   destruction) and record it (who, when, method, disk serials) for the HIPAA file.
5. Remove `gs://BACKUP_BUCKET/migration/` if not done in migrate-data §7.
