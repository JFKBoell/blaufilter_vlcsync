#!/usr/bin/env bash
# Host only: sync controller + web UI as a system service.
set -euo pipefail

echo "==> [40-controller] Installing blaufilter-controller system unit"
sed "s/@USER@/$BF_USER/" "$BF_REPO_DIR/deploy/systemd/blaufilter-controller.service" \
    > /etc/systemd/system/blaufilter-controller.service
systemctl daemon-reload
systemctl enable blaufilter-controller
systemctl restart blaufilter-controller
