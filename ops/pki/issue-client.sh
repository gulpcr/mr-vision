#!/usr/bin/env sh
# Issue a DICOM-TLS client certificate for one scanner / modality, signed by the
# internal CA (which orthanc/trusted-scanners.pem trusts by default).
#
#   ops/pki/issue-client.sh <name> [PKI_DIR default /srv/mrcv/pki]
#
# Writes <PKI_DIR>/scanners/<name>/{client.crt,client.key,ca.crt}. Hand those three files
# to the modality engineer over a secure channel (never e-mail the key), and configure
# the scanner to verify Orthanc against ca.crt. Revoke by removing the CA trust
# (re-issue) or by trusting per-scanner certificates only — see ops/deploy/SCANNERS.md.
set -eu
NAME="${1:?usage: issue-client.sh <name> [PKI_DIR]}"
PKI="${2:-/srv/mrcv/pki}"
DIR="$PKI/scanners/$NAME"
umask 077
[ -f "$PKI/ca.key" ] || { echo "no CA in $PKI (run ops/pki/make-certs.sh first)" >&2; exit 1; }
mkdir -p "$DIR"
openssl genrsa -out "$DIR/client.key" 2048 2>/dev/null
openssl req -new -key "$DIR/client.key" -subj "/O=MRCV Platform/OU=Modality/CN=$NAME" \
  -out "$DIR/client.csr"
printf 'basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=clientAuth\n' \
  > "$DIR/ext.cnf"
openssl x509 -req -in "$DIR/client.csr" -CA "$PKI/ca.crt" -CAkey "$PKI/ca.key" -CAcreateserial \
  -days 825 -sha256 -extfile "$DIR/ext.cnf" -out "$DIR/client.crt" 2>/dev/null
rm -f "$DIR/client.csr" "$DIR/ext.cnf"
cp "$PKI/ca.crt" "$DIR/ca.crt"
echo "issued $DIR/client.crt (expires $(openssl x509 -enddate -noout -in "$DIR/client.crt" | cut -d= -f2))"
