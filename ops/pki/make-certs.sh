#!/usr/bin/env sh
# Internal PKI for TLS between the platform's own services (HIPAA 164.312(e)).
#
# Creates a private CA and one server certificate per service, with the compose service
# name as the certificate name (that is the hostname clients connect to). Idempotent: an
# existing CA is reused, so re-running only issues missing/expiring certificates.
#
#   ops/pki/make-certs.sh [OUT_DIR]          (default /srv/mrcv/pki)
#
# Output (keys are 0600 and never leave the host — do not commit them):
#   ca.crt ca.key
#   postgres/server.{crt,key}   (owned by uid 70: postgres:16-alpine)
#   redis/server.{crt,key}      (uid 999)
#   minio/public.crt minio/private.key minio/CAs/ca.crt
#   orthanc/orthanc.pem         (key + cert, Orthanc SslCertificate)  orthanc/dicom.{crt,key}
#   backend/server.{crt,key}    (uvicorn --ssl-*)
set -eu

OUT="${1:-/srv/mrcv/pki}"
DAYS_CA=3650
DAYS_LEAF=825
umask 077
mkdir -p "$OUT"
cd "$OUT"

if [ ! -f ca.key ]; then
  openssl genrsa -out ca.key 4096 2>/dev/null
  openssl req -x509 -new -key ca.key -sha256 -days "$DAYS_CA" \
    -subj "/O=MRCV Platform/CN=MRCV Internal CA" -out ca.crt
  echo "created CA"
fi

issue() {  # issue <name> <dir> <SANs comma-separated>
  name="$1"; dir="$2"; sans="$3"
  mkdir -p "$dir"
  crt="$dir/server.crt"; key="$dir/server.key"
  if [ -f "$crt" ] && openssl x509 -checkend $((30*86400)) -noout -in "$crt" >/dev/null 2>&1; then
    echo "keep $name (valid > 30 days)"; return
  fi
  openssl genrsa -out "$key" 2048 2>/dev/null
  san=""
  old_ifs="$IFS"; IFS=","
  for s in $sans; do san="${san:+$san,}DNS:$s"; done
  IFS="$old_ifs"
  cat > "$dir/ext.cnf" <<EOF
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth,clientAuth
subjectAltName=$san,DNS:localhost,IP:127.0.0.1
EOF
  openssl req -new -key "$key" -subj "/O=MRCV Platform/CN=$name" -out "$dir/server.csr"
  openssl x509 -req -in "$dir/server.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
    -days "$DAYS_LEAF" -sha256 -extfile "$dir/ext.cnf" -out "$crt" 2>/dev/null
  rm -f "$dir/server.csr" "$dir/ext.cnf"
  echo "issued $name"
}

issue postgres postgres postgres
issue redis    redis    redis
issue minio    minio    minio
issue orthanc  orthanc  orthanc
issue backend  backend  backend

# Service-specific layouts.
mkdir -p minio/CAs
cp minio/server.crt minio/public.crt
cp minio/server.key minio/private.key
cp ca.crt minio/CAs/ca.crt
cat orthanc/server.key orthanc/server.crt > orthanc/orthanc.pem
cp orthanc/server.crt orthanc/dicom.crt
cp orthanc/server.key orthanc/dicom.key
# Scanners must present a certificate signed by one of these CAs (mutual DICOM TLS).
# Starts as the internal CA (ops/pki/issue-client.sh issues scanner certificates);
# append a hospital CA to accept certificates the scanner vendor installed.
[ -f orthanc/trusted-scanners.pem ] || cp ca.crt orthanc/trusted-scanners.pem

# Directories traversable by the (non-root) service users; keys stay 0600.
chmod 0755 . postgres redis minio minio/CAs orthanc backend
chmod 0644 ca.crt minio/public.crt minio/CAs/ca.crt postgres/server.crt redis/server.crt \
  orthanc/server.crt orthanc/dicom.crt orthanc/trusted-scanners.pem backend/server.crt
# Container users must be able to read their own key (only when run as root on the host).
if [ "$(id -u)" = "0" ]; then
  chown 70:70 postgres/server.key        # postgres:16-alpine
  chown 999:999 redis/server.key         # redis:7-alpine
fi
echo "PKI ready in $OUT (CA fingerprint: $(openssl x509 -noout -fingerprint -sha256 -in ca.crt | cut -d= -f2))"
