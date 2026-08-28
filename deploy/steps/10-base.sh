#!/usr/bin/env bash
# Base packages, /opt/blaufilter layout, Python venv with this repo installed.
set -euo pipefail

echo "==> [10-base] Installing packages"
apt-get update
apt-get install -y --no-install-recommends vlc python3-venv python3-pip nftables iw
if [[ "${BF_ROLE:-}" == "host" ]]; then
    # Host network packages MUST install here: step 20 switches wlan0 to AP
    # mode, after which there is no internet connectivity for apt anymore
    apt-get install -y --no-install-recommends dnsmasq-base avahi-daemon avahi-utils
fi

echo "==> [10-base] Creating /opt/blaufilter"
install -d /opt/blaufilter/video

if [[ ! -d /opt/blaufilter/venv ]]; then
    python3 -m venv /opt/blaufilter/venv
fi
/opt/blaufilter/venv/bin/pip install --upgrade pip || true
if ! /opt/blaufilter/venv/bin/pip install "$BF_REPO_DIR"; then
    # Re-installs on devices already living in the (uplink-less) AP network:
    # install from the local repo without touching PyPI. NOTE: new
    # dependencies cannot be resolved offline — those updates need internet.
    echo "==> [10-base] PyPI unreachable — offline install (existing deps only)"
    /opt/blaufilter/venv/bin/pip install --no-index --no-deps "$BF_REPO_DIR"
fi

if [[ -n "${BF_VIDEO:-}" ]]; then
    echo "==> [10-base] Copying video to /opt/blaufilter/video/main.mp4"
    cp "$BF_VIDEO" /opt/blaufilter/video/main.mp4
fi
chown -R "$BF_USER:$BF_USER" /opt/blaufilter/video
