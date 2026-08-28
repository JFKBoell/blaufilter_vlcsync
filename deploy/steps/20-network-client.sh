#!/usr/bin/env bash
# Client (ID >= 2): join the host's WiFi AP with a static IP derived from the
# device ID (192.168.4.(10+ID)), so the controller can probe a fixed address set.
set -euo pipefail

CLIENT_IP="192.168.4.$((10 + BF_ID))"

echo "==> [20-network-client] Removing host-role leftovers (cloned SD / role switch)"
# A leftover AP profile would make this client broadcast its own 'Blaufilter'
# network instead of joining the master's — seen with cloned SD cards.
nmcli connection delete blaufilter-ap 2>/dev/null || true
systemctl disable --now blaufilter-controller 2>/dev/null || true
systemctl disable --now blaufilter-mdns-alias 2>/dev/null || true
rm -f /etc/NetworkManager/dnsmasq-shared.d/blaufilter.conf

if [[ "${BF_OPEN:-0}" == "1" ]]; then
    # Open network: no wireless-security setting at all (key-mgmt=none = WEP).
    # Must match the host — install every device with the same --open flag.
    SEC_ARGS=()
else
    SEC_ARGS=(wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$BF_PSK")
fi

echo "==> [20-network-client] Joining '$BF_SSID' as $CLIENT_IP"
nmcli connection delete blaufilter 2>/dev/null || true
nmcli connection add type wifi ifname wlan0 con-name blaufilter autoconnect yes \
    connection.autoconnect-retries 0 \
    ssid "$BF_SSID" \
    "${SEC_ARGS[@]}" \
    ipv4.method manual \
    ipv4.addresses "$CLIENT_IP/24" \
    ipv4.gateway 192.168.4.1 \
    ipv4.dns 192.168.4.1 \
    ipv6.method disabled

# May fail while the host AP is not up yet; autoconnect retries forever.
nmcli connection up blaufilter || echo "    (AP not reachable yet — will connect automatically)"
