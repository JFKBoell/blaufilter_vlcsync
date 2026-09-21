#!/usr/bin/env bash
# Blaufilter installer: turns a fresh Raspberry Pi OS (64-bit, Bookworm) into a
# Blaufilter host (ID 1: WiFi AP + sync controller + web UI) or client.
#
# Usage:
#   sudo ./install.sh --id 1 --video /path/to/main.mp4            # host
#   sudo ./install.sh --id 2 --video /path/to/main.mp4            # client
#   sudo ./install.sh --id 3 --role client --ssid Blaufilter --psk geheim123
#   sudo ./install.sh --id 1 --open                                # open WiFi
#
# NOTE: leaving out --psk does NOT create an open network — it falls back to
# the default password. Use --open for that, on every device.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Please run as root: sudo $0 ..." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export BF_REPO_DIR="$(dirname "$SCRIPT_DIR")"

export BF_ID=""
export BF_ROLE=""
export BF_SSID="Blaufilter"
export BF_PSK="blaufilter"
export BF_VIDEO=""
export BF_SPLASH=""
export BF_PIN="1234"
export BF_OPEN=0
export BF_TXPOWER=""
export BF_USER="${SUDO_USER:-pi}"
WIFI_COUNTRY="DE"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --id)           BF_ID="$2"; shift 2 ;;
        --role)         BF_ROLE="$2"; shift 2 ;;
        --ssid)         BF_SSID="$2"; shift 2 ;;
        --psk)          BF_PSK="$2"; shift 2 ;;
        --video)        BF_VIDEO="$2"; shift 2 ;;
        --splash)       BF_SPLASH="$2"; shift 2 ;;
        --pin)          BF_PIN="$2"; shift 2 ;;
        --open)         BF_OPEN=1; shift ;;
        --txpower)      BF_TXPOWER="$2"; shift 2 ;;
        --user)         BF_USER="$2"; shift 2 ;;
        --wifi-country) WIFI_COUNTRY="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$BF_ID" || ! "$BF_ID" =~ ^[1-6]$ ]]; then
    echo "--id must be 1..6 (1 = host)" >&2
    exit 1
fi
if [[ -z "$BF_ROLE" ]]; then
    [[ "$BF_ID" == "1" ]] && BF_ROLE="host" || BF_ROLE="client"
fi
if [[ "$BF_ROLE" != "host" && "$BF_ROLE" != "client" ]]; then
    echo "--role must be 'host' or 'client'" >&2
    exit 1
fi
if [[ "$BF_OPEN" != "1" && ${#BF_PSK} -lt 8 ]]; then
    echo "--psk must be at least 8 characters (WPA requirement)" >&2
    exit 1
fi
if [[ -n "$BF_TXPOWER" && ! "$BF_TXPOWER" =~ ^([1-9]|1[0-9]|20)$ ]]; then
    echo "--txpower must be 1..20 (dBm); ~10 is a good value for one room" >&2
    exit 1
fi

echo "==> Blaufilter install: id=$BF_ID role=$BF_ROLE user=$BF_USER"

echo "==> Writing /etc/blaufilter/config"
install -d /etc/blaufilter
# ssid/open_wifi/txpower/repo_dir are not read by the controller — they let the
# setup tool re-run this installer with the settings already in use.
cat > /etc/blaufilter/config <<EOF
[blaufilter]
device_id = $BF_ID
role = $BF_ROLE
debug_pin = $BF_PIN
ssid = $BF_SSID
open_wifi = $BF_OPEN
txpower = $BF_TXPOWER
repo_dir = $BF_REPO_DIR
EOF

BOOT_DIR=/boot/firmware
[[ -d "$BOOT_DIR" ]] || BOOT_DIR=/boot
if [[ ! -f "$BOOT_DIR/blaufilter.txt" ]]; then
    echo "==> Writing $BOOT_DIR/blaufilter.txt (emergency override, editable from any PC)"
    cat > "$BOOT_DIR/blaufilter.txt" <<'EOF'
# Blaufilter Boot-Konfiguration (Notausstieg)
# Diese Datei liegt auf der FAT-Boot-Partition und kann an jedem Computer
# bearbeitet werden (SD-Karte einstecken). Aenderungen wirken ab dem
# naechsten Boot.
#
# fullscreen=no  -> Video im Fenster statt Vollbild (Desktop bleibt erreichbar)
# autostart=no   -> VLC startet gar nicht (System-Rettung)
fullscreen=yes
autostart=yes
EOF
fi

echo "==> Setting hostname blaufilter-$BF_ID"
hostnamectl set-hostname "blaufilter-$BF_ID"
sed -i "s/^127\.0\.1\.1.*/127.0.1.1\tblaufilter-$BF_ID/" /etc/hosts || true

echo "==> Setting WiFi country $WIFI_COUNTRY"
raspi-config nonint do_wifi_country "$WIFI_COUNTRY"

bash "$SCRIPT_DIR/steps/10-base.sh"
if [[ "$BF_ROLE" == "host" ]]; then
    bash "$SCRIPT_DIR/steps/20-network-host.sh"
else
    bash "$SCRIPT_DIR/steps/20-network-client.sh"
fi
bash "$SCRIPT_DIR/steps/25-firewall.sh"
if [[ -n "$BF_TXPOWER" ]]; then
    bash "$SCRIPT_DIR/steps/26-txpower.sh"
fi
bash "$SCRIPT_DIR/steps/30-vlc-autostart.sh"
bash "$SCRIPT_DIR/steps/35-agent.sh"
if [[ "$BF_ROLE" == "host" ]]; then
    bash "$SCRIPT_DIR/steps/40-controller.sh"
fi
if [[ -n "$BF_SPLASH" ]]; then
    bash "$SCRIPT_DIR/steps/50-splash.sh"
fi

echo
echo "==> Done. Reboot to start playback: sudo reboot"
if [[ "$BF_ROLE" == "host" ]]; then
    if [[ "$BF_OPEN" == "1" ]]; then
        echo "    Join WiFi '$BF_SSID' (open, no password) — the control page opens by itself"
    else
        echo "    Join WiFi '$BF_SSID' (password: the --psk value) — the control page opens by itself"
    fi
    echo "    Otherwise: http://blaufilter.local or http://192.168.4.1"
fi
if [[ -z "$BF_VIDEO" ]]; then
    echo "    NOTE: no --video given. Copy your video to /opt/blaufilter/video/main.mp4"
fi
