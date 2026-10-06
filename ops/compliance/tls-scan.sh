#!/usr/bin/env bash
# TLS evidence (checklist TEC-05): scan the public endpoint with testssl.sh and keep the
# report. Run after every deploy / certificate change and monthly; preflight runs it when
# PREFLIGHT_TLS_SCAN=1.
#
#   sudo ops/compliance/tls-scan.sh [host[:port]]      (default: PUBLIC_DOMAIN:443)
#
# Fails (exit 1) if the endpoint offers SSLv2/3, TLS 1.0/1.1, or TLS 1.2 while
# TLS_PROTOCOLS is TLS 1.3 only, or if testssl reports any vulnerability as VULNERABLE.
# Reports: ${EVIDENCE_DIR:-/srv/mrcv/evidence}/tls/<date>-<host>.{json,html}
set -Eeuo pipefail
shopt -s inherit_errexit

ENV_FILE="${MRCV_ENV_FILE:-/run/mrcv/.env}"
envval() { { grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | tail -1 | cut -d= -f2-; }
TARGET="${1:-$(envval PUBLIC_DOMAIN):443}"
[ "${TARGET%%:*}" != "" ] || { echo "no target (pass host:port or set PUBLIC_DOMAIN)" >&2; exit 2; }
OUT="${EVIDENCE_DIR:-/srv/mrcv/evidence}/tls"
IMAGE="${TESTSSL_IMAGE:-drwetter/testssl.sh:3.2}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
base="$stamp-${TARGET%%:*}"
install -d -m 0750 "$OUT"

docker run --rm --network host -v "$OUT:/out" "$IMAGE" \
  --quiet --protocols --server-defaults --vulnerable --severity LOW \
  --jsonfile "/out/$base.json" --htmlfile "/out/$base.html" "$TARGET" >/dev/null || true
[ -s "$OUT/$base.json" ] || { echo "tls-scan: testssl produced no report" >&2; exit 1; }

python3 - "$OUT/$base.json" "$(envval TLS_PROTOCOLS)" <<'PY'
import json, sys
findings = json.load(open(sys.argv[1]))
tls13_only = (sys.argv[2] or "TLSv1.3").strip() == "TLSv1.3"
bad = []
for f in findings:
    fid, sev, text = f.get("id", ""), f.get("severity", ""), f.get("finding", "")
    if fid in ("SSLv2", "SSLv3", "TLS1", "TLS1_1") and "not offered" not in text:
        bad.append(f"{fid}: {text}")
    if fid == "TLS1_2" and tls13_only and "not offered" not in text:
        bad.append(f"TLS1_2 offered although TLS_PROTOCOLS is TLS 1.3 only: {text}")
    if sev in ("HIGH", "CRITICAL") or "VULNERABLE" in text.upper() and "NOT VULNERABLE" not in text.upper():
        bad.append(f"{fid} [{sev}]: {text}")
for line in bad:
    print("  FAIL", line)
print(f"tls-scan: {len(findings)} checks, {len(bad)} problems")
sys.exit(1 if bad else 0)
PY
echo "tls-scan: report $OUT/$base.html"
