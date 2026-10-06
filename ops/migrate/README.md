# Moving to the production stack (old host → new host)

Moves an existing installation (today: the single Windows/Docker Desktop host, plain
storage) onto a new host running the encrypted production stack. The old host stays
read-only and running until the new one is verified; nothing on it is deleted by these
steps. Allow a maintenance window: scanners pause, the old site goes read-only.

**Identity must carry over**: `secrets-init.sh --import <old .env>` keeps
`JWT_SECRET_KEY`, `PHI_HASH_SALT`, the DB passwords, `ORTHANC_*` and the audit-chain
settings. With a new `JWT_SECRET_KEY` every MFA secret and the audit hash chain become
unverifiable.

## 1. Prepare the new host

`ops/deploy/README.md` steps 1–3, then bring up only the data services:

```bash
sudo ops/deploy/up.sh --prepare-only
C="docker compose --env-file /run/mrcv/.env -f docker-compose.yml -f docker-compose.prod.yml"
sudo $C up -d postgres pg-init redis minio minio-init orthanc
```

## 2. Freeze the old host

Point scanners away (or stop accepting: remove `ORTHANC_DICOM_MODALITIES`), stop the
old `worker` and `beat` so nothing writes, keep `postgres`, `minio`, `orthanc` running.

## 3. Database

```bash
# on the old host (dump over the local socket; nothing leaves unencrypted)
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > platform.dump
# copy over SSH:  scp platform.dump new-host:/srv/mrcv/backups/
# on the new host — the database must be empty (the backend has not started yet).
# The RLS app role is cluster-wide (not in the dump): create it first.
sudo ops/migrate/create-app-role.sh
sudo $C exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error' \
  < /srv/mrcv/backups/platform.dump
shred -u /srv/mrcv/backups/platform.dump   # and on the old host
```

`create-app-role.sh` uses `POSTGRES_APP_USER` / `POSTGRES_APP_PASSWORD` from the secrets
(imported from the old `.env`, so the backend connects exactly as before). The backend's
`alembic upgrade head` then applies the migrations the old host did not have yet.

## 4. Orthanc (DICOM) — re-sent into encrypted storage, verified per file

```bash
# tunnel the old Orthanc to the new host (encrypted in transit), bound to the Docker
# bridge address so a container on the new host can reach it as host.docker.internal
ssh -N -L 172.17.0.1:18042:127.0.0.1:8042 old-host &
# run the copy inside the compose network (the new Orthanc is not published):
sudo docker run --rm --network "$(basename $PWD)_default" --add-host host.docker.internal:host-gateway \
  -v /srv/mrcv/pki/ca.crt:/ca.crt:ro -v /srv/mrcv/backups:/state -v "$PWD/ops/migrate:/m:ro" \
  -e ORTHANC_SRC_PASSWORD -e ORTHANC_DST_PASSWORD python:3.11-slim \
  python /m/orthanc-copy.py --src http://host.docker.internal:18042 --allow-insecure-remote \
     --dst https://orthanc:8042 --dst-ca /ca.crt --state /state/orthanc-copy.state
```

Resumable (re-run after an interruption); every instance is read back and compared by
SHA-256; study labels are copied. It must end with `VERIFIED`. Then
`--verify-only --sample 500` once more. (`--allow-insecure-remote` only because the source
is an SSH tunnel endpoint seen through the Docker gateway.)

## 5. AI artifacts (MinIO)

```bash
ssh -N -L 172.17.0.1:19000:127.0.0.1:9000 old-host &
sudo OLD_MINIO_ACCESS_KEY=... OLD_MINIO_SECRET_KEY=... ops/migrate/minio-copy.sh
```

Objects land encrypted (bucket default SSE-S3). Counts must match.

## 6. Start and verify

```bash
sudo ops/deploy/up.sh --build --first-deploy
```

* sign in, open three studies of different tenants, open each in the viewer, download a
  report PDF; check a signed report shows as the signed copy;
* `/api/admin/audit/verify` → `valid: true` (hash chain intact after the move);
* run the first backup and a restore drill;
* re-point scanners ([`ops/deploy/SCANNERS.md`](../deploy/SCANNERS.md)), send a test study.

## 7. Retire the old host

Only after a week of normal operation and a successful restore drill: export the old
host's audit log for the record (or keep its database dump encrypted with the backup age
key for the 6-year retention), then destroy its data volumes and disks (provider's secure
wipe / `blkdiscard` / physical destruction), and record the disposal (HIPAA 164.310(d)(2)).
