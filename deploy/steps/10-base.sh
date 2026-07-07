#!/usr/bin/env bash
# Base packages, /opt/blaufilter layout, Python venv with this repo installed.
set -euo pipefail

echo "==> [10-base] Installing packages"
apt-get update
apt-get install -y --no-install-recommends vlc python3-venv python3-pip

echo "==> [10-base] Creating /opt/blaufilter"
install -d /opt/blaufilter/video

if [[ ! -d /opt/blaufilter/venv ]]; then
    python3 -m venv /opt/blaufilter/venv
fi
/opt/blaufilter/venv/bin/pip install --upgrade pip
/opt/blaufilter/venv/bin/pip install "$BF_REPO_DIR"

if [[ -n "${BF_VIDEO:-}" ]]; then
    echo "==> [10-base] Copying video to /opt/blaufilter/video/main.mp4"
    cp "$BF_VIDEO" /opt/blaufilter/video/main.mp4
fi
chown -R "$BF_USER:$BF_USER" /opt/blaufilter/video
