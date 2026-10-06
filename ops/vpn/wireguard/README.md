# Scanner VPN (WireGuard) — for modalities without DICOM-TLS

DICOM from a scanner to the platform must be encrypted in transit (HIPAA 164.312(e)).
The default is **mutual DICOM-TLS** on port 2762 ([ops/deploy/SCANNERS.md](../../deploy/SCANNERS.md)).
Older scanners that can only send plain DICOM use this encrypted site-to-site tunnel
instead: plain DICOM then travels only inside WireGuard (ChaCha20-Poly1305), and the
platform's plain DICOM port is bound to the tunnel address, never to the internet.

1. **Platform host** — `apt install wireguard`, create keys, copy
   `wg0.conf.template` to `/etc/wireguard/wg0.conf`, fill in the keys and one `[Peer]`
   per site; `bootstrap.sh --vpn` opens udp/51820; `systemctl enable --now wg-quick@wg0`.
   The tunnel address must exist before Docker binds the DICOM port to it:
   `systemctl edit mrcv-stack` → `[Unit]` `After=wg-quick@wg0.service` `Wants=wg-quick@wg0.service`.
2. **Platform settings** (`/etc/mrcv/mrcv.env`): `DICOM_TLS_ENABLED=false`,
   `DICOM_PORT=4242`, `DICOM_BIND=10.66.0.1`, `SCANNER_CIDRS=10.66.0.0/24,<scanner subnets>`;
   `ops/deploy/up.sh`. Preflight warns while DICOM TLS is off — expected here.
3. **Site gateway** — `site-peer.conf.template`; route the scanner subnet's traffic for
   `10.66.0.1` into the tunnel (or NAT it on the gateway).
4. **Scanner** — destination `10.66.0.1`, port `4242`, called AE `MRI_AI`; register the
   scanner in `ORTHANC_DICOM_MODALITIES` with its tunnel-side address.
5. Verify from the site: `echoscu 10.66.0.1 4242 -aec MRI_AI -aet <scanner AET>`.

Keys: the server and site private keys never leave their machines; the preshared key is
exchanged out of band. Record each peer (site, contact, date, public key) in
[ops/KEYS.md](../../KEYS.md); remove the `[Peer]` block to revoke a site.
