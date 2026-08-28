#!/usr/bin/env bash
# All devices: restrict the VLC RC port and the video agent to the host.
set -euo pipefail

echo "==> [25-firewall] Restricting ports 4212/4213 to the host controller"
install -d /etc/blaufilter
install -m 644 "$BF_REPO_DIR/deploy/blaufilter-firewall.nft" /etc/blaufilter/firewall.nft
install -m 644 "$BF_REPO_DIR/deploy/systemd/blaufilter-firewall.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable blaufilter-firewall
systemctl restart blaufilter-firewall
