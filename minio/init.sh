#!/bin/sh
# One-shot (service `minio-init` in docker-compose.prod.yml), idempotent.
#
#   * creates the buckets and turns on default server-side encryption (SSE-S3, keys from
#     MINIO_KMS_SECRET_KEY) so every object is encrypted at rest, whoever writes it;
#   * creates one least-privilege user per consumer instead of sharing the root user:
#       MINIO_APP_USER      -> the artifact bucket only   (backend, worker, beat)
#       MINIO_ORTHANC_USER  -> the DICOM bucket only      (Orthanc S3 storage plugin)
#       MINIO_BACKUP_USER   -> read-only, both buckets    (ops/backup.sh; optional)
#
# mc trusts the internal CA through SSL_CERT_FILE (set by the compose file).
set -eu
: "${MINIO_ROOT_USER:?}" "${MINIO_ROOT_PASSWORD:?}"
: "${MINIO_APP_USER:?}" "${MINIO_APP_PASSWORD:?}"
: "${MINIO_ORTHANC_USER:?}" "${MINIO_ORTHANC_PASSWORD:?}"
ARTIFACTS="${MINIO_BUCKET:-mri-artifacts}"
DICOM="${ORTHANC_S3_BUCKET:-orthanc-dicom}"
ENDPOINT="${MINIO_URL:-https://minio:9000}"

i=0
until mc alias set mrcv "$ENDPOINT" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1; do
  i=$((i+1)); [ "$i" -gt 60 ] && { echo "minio-init: MinIO not reachable at $ENDPOINT" >&2; exit 1; }
  sleep 2
done

# WORM bucket for the audit-log archive (PRV-06): Object Lock must be enabled when the
# bucket is created; COMPLIANCE mode means nobody can delete or shorten retention.
AUDIT="${AUDIT_ARCHIVE_BUCKET:-audit-archive}"
mc mb --ignore-existing --with-lock "mrcv/$AUDIT"
mc retention set --default COMPLIANCE "${AUDIT_ARCHIVE_RETENTION_DAYS:-2190}d" "mrcv/$AUDIT"
mc encrypt set sse-s3 "mrcv/$AUDIT"
mc anonymous set none "mrcv/$AUDIT"

for b in "$ARTIFACTS" "$DICOM"; do
  mc mb --ignore-existing "mrcv/$b"
  mc encrypt set sse-s3 "mrcv/$b"
  mc anonymous set none "mrcv/$b"
done

policy() {  # policy <name> <bucket>
  f="/tmp/$1.json"
  cat > "$f" <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:GetBucketLocation","s3:ListBucket","s3:ListBucketMultipartUploads"],
  "Resource":["arn:aws:s3:::$2"]},
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject",
  "s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],
  "Resource":["arn:aws:s3:::$2/*"]}]}
JSON
  mc admin policy create mrcv "$1" "$f" >/dev/null
}
# The application may add audit archives (and read them back), never delete them.
cat > /tmp/mrcv-app.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:GetBucketLocation","s3:ListBucket","s3:ListBucketMultipartUploads"],
  "Resource":["arn:aws:s3:::$ARTIFACTS","arn:aws:s3:::$AUDIT"]},
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject",
  "s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],
  "Resource":["arn:aws:s3:::$ARTIFACTS/*"]},
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:PutObjectRetention",
  "s3:GetObjectRetention"],"Resource":["arn:aws:s3:::$AUDIT/*"]}]}
JSON
mc admin policy create mrcv mrcv-app /tmp/mrcv-app.json >/dev/null
policy mrcv-orthanc "$DICOM"
cat > /tmp/mrcv-backup.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:GetBucketLocation","s3:ListBucket"],
  "Resource":["arn:aws:s3:::$ARTIFACTS","arn:aws:s3:::$DICOM"]},
 {"Effect":"Allow","Action":["s3:GetObject"],
  "Resource":["arn:aws:s3:::$ARTIFACTS/*","arn:aws:s3:::$DICOM/*"]}]}
JSON
mc admin policy create mrcv mrcv-backup /tmp/mrcv-backup.json >/dev/null

mc admin user add mrcv "$MINIO_APP_USER" "$MINIO_APP_PASSWORD" >/dev/null
mc admin user add mrcv "$MINIO_ORTHANC_USER" "$MINIO_ORTHANC_PASSWORD" >/dev/null
mc admin policy attach mrcv mrcv-app --user "$MINIO_APP_USER" >/dev/null 2>&1 || true
mc admin policy attach mrcv mrcv-orthanc --user "$MINIO_ORTHANC_USER" >/dev/null 2>&1 || true
if [ -n "${MINIO_BACKUP_USER:-}" ] && [ -n "${MINIO_BACKUP_PASSWORD:-}" ]; then
  mc admin user add mrcv "$MINIO_BACKUP_USER" "$MINIO_BACKUP_PASSWORD" >/dev/null
  mc admin policy attach mrcv mrcv-backup --user "$MINIO_BACKUP_USER" >/dev/null 2>&1 || true
fi

for b in "$ARTIFACTS" "$DICOM" "$AUDIT"; do
  mc encrypt info "mrcv/$b"
done
mc retention info --default "mrcv/$AUDIT"
echo "minio-init: buckets encrypted, users ready"
