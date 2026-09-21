#!/usr/bin/env bash
# All devices: cap the WiFi transmit power. With the Pis close together in one
# room, full power mostly adds mutual interference (and desensitizes the
# receivers) instead of range. Applied via a NetworkManager dispatcher script
# so it survives reconnects and reboots — the driver resets it otherwise.
set -euo pipefail

DISPATCHER=/etc/NetworkManager/dispatcher.d/50-blaufilter-txpower
MBM=$((BF_TXPOWER * 100))   # iw expects mBm

echo "==> [26-txpower] Capping WiFi TX power at ${BF_TXPOWER} dBm"
install -d "$(dirname "$DISPATCHER")"
cat > "$DISPATCHER" <<EOF
#!/bin/sh
# Blaufilter: re-apply the TX power cap whenever wlan0 comes up.
[ "\$1" = "wlan0" ] || exit 0
case "\$2" in
    up|connectivity-change) iw dev wlan0 set txpower fixed $MBM || true ;;
esac
EOF
# Dispatcher scripts are ignored unless root-owned and not group/world writable
chown root:root "$DISPATCHER"
chmod 755 "$DISPATCHER"

# Apply right away too, so the effect can be checked without a reboot
iw dev wlan0 set txpower fixed "$MBM" \
    || echo "    (could not set TX power now — will retry when wlan0 comes up)"
iw dev wlan0 info | grep -i txpower || true
