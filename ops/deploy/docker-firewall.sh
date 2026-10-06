#!/usr/bin/env bash
# Restrict the published DICOM port to the scanner networks.
#
# Docker publishes container ports through its own iptables chains, BEFORE ufw sees the
# packet, so `ufw allow from <scanner> to any port 2762` alone does not close the port to
# everyone else. DOCKER-USER is the chain Docker evaluates first for forwarded traffic;
# rules there survive container restarts. Matching on the ORIGINAL destination port
# (conntrack) is required because the packet has already been DNAT-ed to the container.
#
#   sudo ops/deploy/docker-firewall.sh            (reads /etc/mrcv/mrcv.env)
#
# Uses SCANNER_CIDRS (comma-separated) and DICOM_PORT (default 2762). Idempotent; run at
# boot by ops/systemd/mrcv-docker-firewall.service after docker.service.
set -Eeuo pipefail

ENV="${MRCV_ETC:-/etc/mrcv}/mrcv.env"
val() { grep -E "^$1=" "$ENV" 2>/dev/null | tail -1 | cut -d= -f2-; }
PORT="$(val DICOM_PORT)"; PORT="${PORT:-2762}"
CIDRS="$(val SCANNER_CIDRS)"
TAG="mrcv-dicom"

iptables -N DOCKER-USER 2>/dev/null || true
# Remove our previous rules (identified by the comment), keep everyone else's.
while read -r rule; do
  [ -n "$rule" ] && eval "iptables ${rule/-A/-D}"
done < <(iptables -S DOCKER-USER | grep -- "--comment $TAG" || true)

if [ -z "$CIDRS" ]; then
  # No scanner networks configured: the DICOM port is closed to everyone.
  iptables -I DOCKER-USER -p tcp -m conntrack --ctorigdstport "$PORT" --ctdir ORIGINAL \
    -m comment --comment "$TAG" -j DROP
  echo "docker-firewall: SCANNER_CIDRS empty - DICOM port $PORT closed"
  exit 0
fi
iptables -I DOCKER-USER -p tcp -m conntrack --ctorigdstport "$PORT" --ctdir ORIGINAL \
  -m comment --comment "$TAG" -j DROP
for cidr in $(echo "$CIDRS" | tr ',' ' '); do
  iptables -I DOCKER-USER -p tcp -s "$cidr" -m conntrack --ctorigdstport "$PORT" --ctdir ORIGINAL \
    -m comment --comment "$TAG" -j RETURN
done
echo "docker-firewall: DICOM port $PORT open to $CIDRS only"
