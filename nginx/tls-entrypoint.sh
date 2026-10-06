#!/bin/sh
# nginx entrypoint wrapper for the production TLS stack (docker-compose.prod.yml).
#
#  1. Chooses which template directory the official image's envsubst step renders:
#       certificate present -> /etc/nginx/templates-tls        (full platform over HTTPS)
#       certificate missing -> /etc/nginx/templates-bootstrap  (ACME challenge only)
#  2. Starts a background watcher that polls the certificate every CERT_CHECK_INTERVAL
#     seconds (default 300):
#       bootstrap mode + certificate appeared -> `nginx -s quit`; the container restarts
#                                                 (restart: unless-stopped) in TLS mode
#       TLS mode + certificate changed        -> `nginx -s reload` (picks up renewals)
#     This replaces a certbot deploy hook, so the certbot container needs no Docker socket.
#  3. Renders the real_ip directives for TRUSTED_PROXY_CIDRS (comma-separated CIDRs of
#     a load balancer in front of nginx) into /tmp/nginx-gen/real-ip.conf, so audit
#     entries and per-IP login limits see the real client, never a spoofed header.
#  4. Hands over to the image's /docker-entrypoint.sh (envsubst + exec nginx).
#
# Run with `sh`, not executed directly, so it needs no executable bit on the host checkout.
set -eu

: "${PUBLIC_DOMAIN:?PUBLIC_DOMAIN must be set for the TLS nginx}"
# Let's Encrypt paths unless the operator supplies their own certificate.
LIVE="/etc/letsencrypt/live/${PUBLIC_DOMAIN}"
export TLS_CERT_FILE="${TLS_CERT_FILE:-${LIVE}/fullchain.pem}"
export TLS_KEY_FILE="${TLS_KEY_FILE:-${LIVE}/privkey.pem}"
# Public TLS versions (checklist TEC-05: TLS 1.3).
export TLS_PROTOCOLS="${TLS_PROTOCOLS:-TLSv1.3}"
INTERVAL="${CERT_CHECK_INTERVAL:-300}"

have_cert() {
    [ -s "${TLS_CERT_FILE}" ] && [ -s "${TLS_KEY_FILE}" ]
}

fingerprint() {
    cat "${TLS_CERT_FILE}" "${TLS_KEY_FILE}" 2>/dev/null | md5sum | cut -d' ' -f1
}

mkdir -p /tmp/nginx-gen
: > /tmp/nginx-gen/real-ip.conf
if [ -n "${TRUSTED_PROXY_CIDRS:-}" ]; then
    for cidr in $(echo "${TRUSTED_PROXY_CIDRS}" | tr ',' ' '); do
        echo "set_real_ip_from ${cidr};" >> /tmp/nginx-gen/real-ip.conf
    done
    echo 'real_ip_header X-Forwarded-For;' >> /tmp/nginx-gen/real-ip.conf
    echo 'real_ip_recursive on;' >> /tmp/nginx-gen/real-ip.conf
    echo "tls-entrypoint: trusting client addresses from ${TRUSTED_PROXY_CIDRS}" >&2
fi

if have_cert; then
    MODE=tls
    export NGINX_ENVSUBST_TEMPLATE_DIR=/etc/nginx/templates-tls
else
    MODE=bootstrap
    export NGINX_ENVSUBST_TEMPLATE_DIR=/etc/nginx/templates-bootstrap
    echo "tls-entrypoint: no certificate at ${TLS_CERT_FILE} yet - serving the ACME challenge only" >&2
fi
echo "tls-entrypoint: mode=${MODE} templates=${NGINX_ENVSUBST_TEMPLATE_DIR}" >&2

(
    set +e
    last="$(fingerprint)"
    while :; do
        sleep "${INTERVAL}"
        if [ "${MODE}" = bootstrap ]; then
            if have_cert; then
                echo "tls-entrypoint: certificate issued - restarting nginx into TLS mode" >&2
                nginx -s quit
                exit 0
            fi
        else
            cur="$(fingerprint)"
            if [ -n "${cur}" ] && [ "${cur}" != "${last}" ]; then
                if nginx -t >/dev/null 2>&1; then
                    echo "tls-entrypoint: certificate changed - reloading nginx" >&2
                    nginx -s reload
                    last="${cur}"
                else
                    echo "tls-entrypoint: certificate changed but nginx -t failed; not reloading" >&2
                fi
            fi
        fi
    done
) &

exec /docker-entrypoint.sh "$@"
