#!/usr/bin/env bash
# All devices: desktop autologin, no screen blanking, VLC systemd user unit
# bound to the graphical session (video output needs the Wayland session).
set -euo pipefail

echo "==> [30-vlc-autostart] Desktop autologin + disable screen blanking"
raspi-config nonint do_boot_behaviour B4
raspi-config nonint do_blanking 1

echo "==> [30-vlc-autostart] Installing blaufilter-vlc user unit for $BF_USER"
USER_HOME="$(getent passwd "$BF_USER" | cut -d: -f6)"
UNIT_DIR="$USER_HOME/.config/systemd/user"
install -d "$UNIT_DIR/graphical-session.target.wants"
install -m 644 "$BF_REPO_DIR/deploy/systemd/blaufilter-vlc.service" "$UNIT_DIR/"
# Manual enable (symlink) — works without a running user session bus
ln -sf ../blaufilter-vlc.service "$UNIT_DIR/graphical-session.target.wants/blaufilter-vlc.service"
chown -R "$BF_USER:$BF_USER" "$USER_HOME/.config"

# Keep the user manager alive so the unit can be inspected over SSH
loginctl enable-linger "$BF_USER"

# Fallback for sessions that never activate graphical-session.target
# (see deploy/README.md troubleshooting): desktop autostart starts the unit.
AUTOSTART_DIR="$USER_HOME/.config/autostart"
install -d "$AUTOSTART_DIR"
install -m 644 "$BF_REPO_DIR/deploy/autostart/blaufilter-vlc.desktop" "$AUTOSTART_DIR/"
chown -R "$BF_USER:$BF_USER" "$AUTOSTART_DIR"
