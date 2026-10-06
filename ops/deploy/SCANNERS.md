# Connecting a scanner (DICOM C-STORE) — encrypted

Production accepts DICOM only over **mutual TLS** on `DICOM_PORT` (default **2762**, the
IANA DICOM-TLS port). Both ends authenticate: the scanner verifies the platform's
certificate, and the platform accepts only scanners presenting a certificate signed by a
CA in `orthanc/trusted-scanners.pem` (by default the platform's internal CA). Scanners
that cannot do DICOM-TLS connect through the WireGuard tunnel instead
([`ops/vpn/wireguard/`](../vpn/wireguard/README.md)).

## Per scanner

1. **Collect** from the modality engineer: AE title, the scanner's source IP (as seen by
   the platform), and whether it supports DICOM TLS with a client certificate (most
   current CT/MR/PET consoles do: "Secure DICOM", "TLS", "ISCL").
2. **Issue its certificate** on the platform host:
   ```bash
   sudo ops/pki/issue-client.sh ct-room-2 /srv/mrcv/pki
   # -> /srv/mrcv/pki/scanners/ct-room-2/{client.crt,client.key,ca.crt}
   ```
   Hand the three files to the engineer over a secure channel (in person / encrypted
   archive with the password by phone). Never e-mail the key in clear.
   *Vendor-installed certificate instead:* append the vendor's / hospital's CA to
   `/srv/mrcv/pki/orthanc/trusted-scanners.pem` and restart Orthanc.
3. **Register it** in `/etc/mrcv/mrcv.env`:
   ```
   ORTHANC_DICOM_MODALITIES={"ct2": ["CT2_AET", "10.20.0.12", 104]}
   SCANNER_CIDRS=10.20.0.0/24
   ```
   For tenant routing, map its AE title as described in the admin UI (DICOM endpoints).
   Apply: `sudo ops/deploy/up.sh` (restarts what changed) and
   `sudo systemctl restart mrcv-docker-firewall`.
4. **Configure the scanner**: destination host `PUBLIC_DOMAIN` (or the platform IP),
   port `2762`, called AE `MRI_AI`, TLS on, client certificate + key from step 2, trust
   `ca.crt`.
5. **Test** from the scanner (DICOM echo / "verify"), then send one study and check it
   appears in the worklist. From a laptop with DCMTK:
   ```bash
   echoscu -aet CT2_AET -aec MRI_AI +tls client.key client.crt +cf ca.crt PUBLIC_DOMAIN 2762
   ```
6. **Record** it in the scanner inventory (AE, IP, certificate expiry — 825 days — and
   contact). Re-issue before expiry with step 2.

## Revoking a scanner

Remove it from `ORTHANC_DICOM_MODALITIES` (the platform then refuses its AE title even with
a valid certificate) and apply. To make its certificate unusable everywhere, trust
per-scanner certificates instead of the CA: put only the current scanners' `client.crt`
files in `trusted-scanners.pem`.

## Why not plain DICOM on 4242?

DICOM over TCP is unencrypted: patient names, IDs and images would cross the network in
clear, which HIPAA 164.312(e) forbids for ePHI in transit over open networks. Port 4242
is published only in the VPN setup, bound to the tunnel address.
