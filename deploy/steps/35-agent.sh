#!/usr/bin/env bash
# All devices: lightweight agent to receive video pushes and restart VLC.
set -euo pipefail

echo "==> [35-agent] Installing blaufilter-agent system unit for $BF_USER"
UID_NUM="$(id -u "$BF_USER")"
sed -e "s/@USER@/$BF_USER/g" -e "s/@UID@/$UID_NUM/g" \
    "$BF_REPO_DIR/deploy/systemd/blaufilter-agent.service" \
    > /etc/systemd/system/blaufilter-agent.service
systemctl daemon-reload
systemctl enable blaufilter-agent
systemctl restart blaufilter-agent
