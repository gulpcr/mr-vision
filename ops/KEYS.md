# Key inventory and rotation

Every cryptographic key the production deployment depends on: where it lives, who can
use it, what is lost without it, and how to rotate it. Keep this table current; it is
part of the HIPAA documentation (164.312(a)(2)(iv), 164.316).

**Escrow** = a copy kept OFF the server (password manager vault or sealed USB in a safe),
by two named people. A key marked *escrow: required* that exists only on the server is
a single point of total data loss.

| Key | Lives at | Protects | Lost ⇒ | Escrow |
|---|---|---|---|---|
| LUKS key file | `/etc/mrcv/luks.key` (boot disk, root 0400) | data volume (all Docker data) | volume unreadable unless the recovery passphrase is known | recovery passphrase: **required** |
| LUKS recovery passphrase | keyslot 1 of the data volume | same | — | **required** |
| Host age key | `/etc/mrcv/age.key` | decrypts `secrets.enc.env` at boot | use the escrow age key | optional (the escrow key covers it) |
| Escrow age key | offline | second recipient of `secrets.enc.env` | secrets unrecoverable if the host is lost | **is** the escrow |
| Backup age key (private) | **offline only** — never on the server | decrypts DB/config backups | backups unreadable | **required** (two copies) |
| `BACKUP_CRYPT_PASSWORD` / `_SALT` | secrets file | object backups (rclone crypt) | object backups unreadable | in the secrets backup (config tarball, age-encrypted) |
| `ORTHANC_STORAGE_MASTER_KEY` (+ previous) | secrets file → tmpfs | every DICOM file in MinIO | **all images unreadable** | in the secrets; **also escrow separately** |
| `MINIO_KMS_SECRET_KEY` | secrets file → MinIO env | MinIO SSE-S3 (all objects) | **all objects unreadable** | in the secrets; **also escrow separately** |
| `JWT_SECRET_KEY` | secrets file | tokens, MFA secrets at rest, audit hash chain | users re-enrol MFA; chain verification needs the old key | in the secrets |
| `PHI_HASH_SALT` | secrets file | pseudonymous patient hashes | hashes no longer match | in the secrets |
| DB / Redis / MinIO / Orthanc passwords | secrets file | service authentication | reset them (no data loss) | in the secrets |
| Internal CA key | `/srv/mrcv/pki/ca.key` (LUKS) | all internal certificates + scanner certs | reissue everything incl. scanner certs | in the config backup |
| Public TLS key | Let's Encrypt volume / `TLS_BYO_DIR` | HTTPS | reissue | not needed |
| WireGuard keys | `/etc/wireguard/` per site | scanner tunnels | regenerate per peer | not needed |

## Rotation

**Database / Redis / MinIO / Orthanc passwords** — edit the value with
`sops /etc/mrcv/secrets.enc.env`, apply it to the service (Postgres:
`ALTER ROLE ... PASSWORD`, MinIO: `mc admin user add` again / root via env, Redis and
Orthanc: env only), then `ops/deploy/up.sh`. Annually or when a person with access leaves.

**`JWT_SECRET_KEY`** — only with `backend/scripts/rotate_master_key.py` (re-encrypts MFA
secrets, records the audit-chain cut-over in `AUDIT_CHAIN_PREVIOUS_MASTER_KEY` /
`AUDIT_CHAIN_ROTATED_AFTER_SEQ`); then put the new key and the two chain settings in the
secrets. Every user is signed out.

**Orthanc storage master key** (yearly, or on suspicion) — new files use the new key,
existing files stay readable through `PreviousMasterKeys` (verified in testing):
```bash
sudo SOPS_AGE_KEY_FILE=/etc/mrcv/age.key sops /etc/mrcv/secrets.enc.env
#   ORTHANC_STORAGE_PREVIOUS_KEY_<old id>=<old ORTHANC_STORAGE_MASTER_KEY value>
#   ORTHANC_STORAGE_MASTER_KEY=<openssl rand -base64 32>
#   ORTHANC_STORAGE_KEY_ID=<old id + 1>
sudo ops/deploy/up.sh
```
Never delete a previous key while files encrypted with it exist (Orthanc's
`/move-storage` / re-ingest can re-encrypt them first).

**`MINIO_KMS_SECRET_KEY`** — MinIO's built-in KMS key cannot be rotated in place; to
rotate, deploy a fresh MinIO with the new key and copy the buckets across
(`ops/migrate/minio-copy.sh`, and Orthanc's bucket with `mc mirror`). For routine key
rotation without copying, connect MinIO to an external KES/KMS instead.

**Internal PKI** — `make-certs.sh` reissues leaf certificates within 30 days of expiry
(825-day validity); `preflight.sh` fails on certificates expiring in < 30 days. The CA is
valid 10 years; replacing it means reissuing scanner certificates.

**Backup age key** — generate a new pair, set `BACKUP_AGE_RECIPIENT` to the new public
key; keep the old private key until the last backup encrypted with it has expired
(`BACKUP_RETENTION_DAYS`).

**Host age key** — `age-keygen` a new one, re-encrypt: `sops updatekeys` with the new
recipient list (`.sops.yaml` or `--age`), remove the old key file.

## Exceptions (documented)

* worker → Ollama (`http://ollama:11434`): Ollama has no TLS; the hop never leaves the host
  (container network on the same machine, on the encrypted host). Accepted risk.
* nginx → UI / OHIF containers: plain HTTP inside the host; they serve only static
  application code, no patient data passes through them.
