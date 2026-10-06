# Provider notes

The deployment itself is provider-neutral ([`ops/deploy/README.md`](../deploy/README.md)):
any Linux GPU VM or bare-metal host with a second disk works. What differs per provider
is the BAA, how you get the VM and disk, and which managed services you may (but need
not) use.

| | BAA | GPU VM | Second disk | Backup target (rclone) | Notes |
|---|---|---|---|---|---|
| **AWS** | AWS BAA via AWS Artifact (account-level, self-service) | g5 / g6 (A10G / L4) | EBS gp3 (also enable EBS default encryption) | S3, `TYPE=s3 PROVIDER=AWS` | Security group: 443, 80, DICOM from scanner CIDRs, 22 from admin. Use only [HIPAA-eligible services](https://aws.amazon.com/compliance/hipaa-eligible-services-reference/). |
| **Azure** | Included in the Microsoft Product Terms / DPA for in-scope services | NCasT4_v3 / NC A10 v4 | Managed disk (SSE on by default) | Blob, `TYPE=azureblob` | NSG rules as for AWS. |
| **Google Cloud** | Google Cloud BAA (accept in the console) | g2 (L4), n1 + T4 | Persistent Disk | GCS, `TYPE=gcs` | Scripts in [`gcp/`](gcp/GCP_DEPLOYMENT.md) create the VM, disk, snapshots, firewall and bucket; overlay `gcp/docker-compose.gcp.yml` sends Gemini through Vertex AI (Google BAA). |
| **Hetzner / OVH / other EU hosts** | Usually **no HIPAA BAA**; a GDPR DPA instead | dedicated GPU servers | local NVMe (use `--data-device`) | any S3-compatible / SFTP | Acceptable for non-US / non-HIPAA customers only, unless the provider signs a BAA. |
| **On-premises** | none needed for the hardware you own | your server | second NVMe | the hospital's NAS (SFTP/SMB) or an S3 provider with a BAA | Physical safeguards (164.310) become yours. |

Common to all:

* **The disk encryption the provider offers is not enough on its own** — it protects
  against stolen provider disks, not against a snapshot or a mis-shared volume in your
  account. The platform's LUKS volume and application-level encryption cover those.
* **External AI**: only with a BAA covering that service (`EXTERNAL_AI_BAA_CONFIRMED`).
  The local MedGemma (ollama container) needs none.
* **Firewall**: the provider's network firewall *and* the host's ufw/DOCKER-USER rules
  (bootstrap.sh) — both.
