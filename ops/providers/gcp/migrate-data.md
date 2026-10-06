> Generic version (any provider, including the Orthanc copy into encrypted storage):
> [`ops/migrate/README.md`](../../migrate/README.md).

# Moving the data from the Windows host to the GCP VM

Scope: the PostgreSQL database (+ roles), the Orthanc DICOM archive (`orthanc_data`
volume) and the MinIO artifact store (`minio_data` volume). Optional: model caches.
Everything travels **encrypted in transit** (HTTPS to Cloud Storage, or SSH inside an
IAP TLS tunnel) and lands on Google-encrypted storage. Nothing is sent over plain FTP /
SMB / HTTP, and nothing is ever copied to a laptop or USB stick.

Run §1–§3 on the **old Windows host** in Git Bash (or the PowerShell equivalents noted),
§4–§6 on the **VM** (`gcloud compute ssh mrcv-prod --zone ZONE --tunnel-through-iap`).

Volume names: on the old host the compose project is `mr_computer-visuion`, so the
volumes are `mr_computer-visuion_postgres_data`, `..._orthanc_data`, `..._minio_data`,
`..._model_cache`. On the VM (repo at `/opt/mrcv`) they become `mrcv_<name>`. Check with
`docker volume ls` on both sides before you start.

## 0. Prerequisites

- The VM is provisioned, secrets are in Secret Manager **with the same values as the old
  `.env`** (see GCP_DEPLOYMENT.md §3 — `JWT_SECRET_KEY`, `PHI_HASH_SALT`,
  `POSTGRES_APP_PASSWORD`, `MINIO_*`, `ORTHANC_*` MUST be identical or MFA secrets,
  the audit hash chain, PHI hashes and stored artifacts become unreadable).
- The images are built on the VM and `docker compose -f docker-compose.yml -f
  docker-compose.prod.yml -f ops/providers/gcp/docker-compose.gcp.yml up --no-start` has created the empty volumes. **Do not start
  the stack yet** — especially not `backend` (it would run migrations on an empty DB).
- `gcloud` is installed on the old host and logged in as an admin
  (`gcloud auth login`) with write access to the backup bucket.
- A maintenance window: scanners paused / rerouted, users informed.

## 1. Freeze the old host

```bash
cd /c/sistems/projects/MR_Computer-Visuion
# Stop everything that writes. Postgres stays up for the dump.
docker compose stop nginx ui worker beat backend orthanc minio
```

Confirm no job is running (worklist empty) before stopping the worker.

## 2. Export

```bash
M=/c/mrcv-migration; mkdir -p "$M"
# (a) roles incl. the RLS app role mrv_app (password hashes only, no plaintext)
docker compose exec -T postgres sh -c 'pg_dumpall -U "$POSTGRES_USER" --roles-only' > "$M/roles.sql"
# (b) the database, custom format
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$M/mri_platform.dump"
docker compose stop postgres

# (c) Orthanc + MinIO volumes as tarballs (containers are stopped -> consistent)
for v in orthanc_data minio_data; do
  MSYS_NO_PATHCONV=1 docker run --rm -v "mr_computer-visuion_${v}:/src:ro" -v "C:/mrcv-migration:/out" \
    alpine tar -C /src -czf "/out/${v}.tgz" .
done
# (d) optional: model caches (tens of GB; they re-download on first use otherwise)
# MSYS_NO_PATHCONV=1 docker run --rm -v mr_computer-visuion_model_cache:/src:ro -v "C:/mrcv-migration:/out" alpine tar -C /src -czf /out/model_cache.tgz .

cd "$M" && sha256sum roles.sql mri_platform.dump *.tgz > SHA256SUMS && cat SHA256SUMS
```

PowerShell equivalent for the checksums: `Get-FileHash -Algorithm SHA256 C:\mrcv-migration\*`.

Untracked files the images need but git does not carry (the VM builds from a git clone):
list them with `git status --ignored --short backend/` — e.g. model checkpoints under
`backend/external/*/ckpt/` or `backend/app/usecases/*/model/weights/`. Tar and transfer
them the same way if they are not downloaded automatically.

## 3. Transfer (pick one)

**A. Via the backup bucket (HTTPS, integrity-checked)** — best for large archives:

```bash
gcloud storage cp --no-user-output-enabled /c/mrcv-migration/* gs://BACKUP_BUCKET/migration/
```

`gcloud storage` verifies CRC32C/MD5 of every object. The bucket has public access
prevention and uniform access; the 35-day lifecycle rule deletes leftovers, but delete
them explicitly after §6 anyway.

**B. Directly to the VM through IAP (SSH-in-TLS)** — fine for smaller data:

```bash
gcloud compute scp --tunnel-through-iap --zone ZONE --recurse /c/mrcv-migration mrcv-prod:/var/tmp/
```

## 4. Import on the VM

```bash
sudo -i
M=/var/tmp/mrcv-migration; mkdir -p "$M" && chmod 700 "$M"
gcloud storage cp "gs://BACKUP_BUCKET/migration/*" "$M/"      # (option A only)
cd "$M" && sha256sum -c SHA256SUMS                            # every line must say OK

cd /opt/mrcv
C="docker compose -f docker-compose.yml -f docker-compose.prod.yml -f ops/providers/gcp/docker-compose.gcp.yml"
$C up -d postgres
# roles first (mri_admin already exists -> that one "already exists" error is expected)
$C exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=0' < "$M/roles.sql"
$C exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --exit-on-error' < "$M/mri_platform.dump"

# volumes (created empty by `up --no-start`)
for v in orthanc_data minio_data; do
  docker run --rm -v "mrcv_${v}:/dst" -v "$M:/src:ro" alpine sh -c "cd /dst && tar -xzf /src/${v}.tgz"
done
```

If the role password for `mrv_app` should change on the new host, set the new value as
the secret `mrcv-POSTGRES_APP_PASSWORD` and run
`ALTER ROLE mrv_app PASSWORD '...'` via `$C exec postgres psql` (type it interactively,
not on the shell command line).

## 5. Verify the data

```bash
$C up -d            # backend runs `alembic upgrade head` (no-op / newer migrations only)
$C exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select count(*) from studies; select version_num from alembic_version;"'
$C exec -T orthanc sh -c 'du -sh /var/lib/orthanc/db'
```

Compare the study count and alembic revision with the old host (run the same `psql`
there before shutting it down), open a few studies in the viewer, download a PDF report
(MinIO artifacts), and check the audit hash chain still verifies with the migrated
`JWT_SECRET_KEY` (`GET /api/admin/audit/verify` as a platform admin).

## 6. First certificate and DNS switch

1. Lower the TTL of the existing DNS record to 300 s a day in advance.
2. Point the A record for `PUBLIC_DOMAIN` at the static IP from `provision.sh`.
3. The `certbot` service requests the certificate automatically over the webroot
   challenge (retries hourly until DNS resolves). To issue immediately instead:

   ```bash
   $C run --rm --entrypoint certbot certbot certonly --webroot -w /var/www/certbot \
       -d "$PUBLIC_DOMAIN" --email "$CERTBOT_EMAIL" --agree-tos --no-eff-email --non-interactive
   ```

   Within `CERT_CHECK_INTERVAL` (5 min) nginx leaves bootstrap mode and serves HTTPS
   (`$C logs nginx | grep tls-entrypoint`). `$C restart nginx` forces it right away.
4. Re-point the scanners' DICOM destination to the new IP (see GCP_DEPLOYMENT.md §6
   about encrypting DICOM), send a test study, confirm it appears and is processed.

## 7. Clean up the transfer copies

```bash
gcloud storage rm -r gs://BACKUP_BUCKET/migration/        # VM or old host
shred -u /var/tmp/mrcv-migration/* && rmdir /var/tmp/mrcv-migration   # VM
```

The copies on the old host are destroyed with the rest of it (GCP_DEPLOYMENT.md §9).
