#!/bin/sh
# BEFORE_ORTHANC_STARTUP_SCRIPT (docker-compose.prod.yml): trust the internal CA
# system-wide so the AWS S3 SDK (libcurl, system bundle) verifies MinIO's certificate.
# Orthanc's own HTTP client uses HttpsCACertificates instead.
set -eu
cp /run/pki/ca.crt /usr/local/share/ca-certificates/mrcv-internal-ca.crt
update-ca-certificates >/dev/null 2>&1
echo "prod-startup: internal CA trusted"
