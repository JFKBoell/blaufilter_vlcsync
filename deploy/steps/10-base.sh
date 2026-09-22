#!/usr/bin/env bash
# Base packages, /opt/blaufilter layout, Python venv with this repo installed.
set -euo pipefail

echo "==> [10-base] Installing packages"

# Re-runs happen on devices that only live in the (uplink-less) AP network —
# the setup tool re-invokes this installer for role and WiFi changes. apt then
# cannot refresh or fetch, which is fine as long as everything is already
# installed; only genuinely missing packages are a hard error.
require_packages() {
    if apt-get install -y --no-install-recommends "$@"; then
        return 0
    fi
    echo "==> [10-base] apt failed (no internet?) — checking what is present"
    local missing=()
    command -v vlc >/dev/null || missing+=(vlc)
    command -v nft >/dev/null || missing+=(nftables)
    command -v iw  >/dev/null || missing+=(iw)
    python3 -c "import venv" 2>/dev/null || missing+=(python3-venv)
    if [[ "${BF_ROLE:-}" == "host" ]]; then
        command -v avahi-publish >/dev/null || missing+=(avahi-utils)
        [[ -x /usr/sbin/dnsmasq ]] || missing+=(dnsmasq-base)
    fi
    if (( ${#missing[@]} )); then
        echo "Missing and not installable offline: ${missing[*]}" >&2
        echo "Connect this device to the internet once, then run the installer again." >&2
        return 1
    fi
    echo "    everything already installed — continuing"
}

apt-get update || echo "    (package lists could not be refreshed — using cached ones)"
PACKAGES=(vlc python3-venv python3-pip nftables iw)
if [[ "${BF_ROLE:-}" == "host" ]]; then
    # Host network packages MUST install here: step 20 switches wlan0 to AP
    # mode, after which there is no internet connectivity for apt anymore
    PACKAGES+=(dnsmasq-base avahi-daemon avahi-utils)
fi
require_packages "${PACKAGES[@]}"

echo "==> [10-base] Enabling SSH"
# Devices end up mounted somewhere without a keyboard, so remote access is not
# optional in practice. raspi-config is preferred because it also generates the
# host keys (a fresh image ships without them and sshd then refuses to start).
if command -v raspi-config >/dev/null 2>&1 && raspi-config nonint do_ssh 0; then
    :
else
    ssh-keygen -A >/dev/null 2>&1 || true
    systemctl enable --now ssh 2>/dev/null \
        || systemctl enable --now sshd 2>/dev/null \
        || echo "    (SSH konnte nicht aktiviert werden — ist openssh-server installiert?)" >&2
fi

echo "==> [10-base] Installing the setup/maintenance tool"
# Symlink, not a copy: a copy silently keeps running an old menu after a
# git pull, and a re-install from an older checkout even downgrades it.
# This way the tool is always exactly what the checkout says it is.
ln -sfn "$BF_REPO_DIR/deploy/blaufilter-setup.sh" /usr/local/sbin/blaufilter-setup
chmod 755 "$BF_REPO_DIR/deploy/blaufilter-setup.sh"

echo "==> [10-base] Creating /opt/blaufilter"
install -d /opt/blaufilter/video
# Runtime state the controller writes itself (drift settings from the web UI).
# The directory must belong to the service user: writing a file atomically
# creates a temporary one beside it, which needs write access to the directory.
install -d /opt/blaufilter/state

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
chown -R "$BF_USER:$BF_USER" /opt/blaufilter/video /opt/blaufilter/state
