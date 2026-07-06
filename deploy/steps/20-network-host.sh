#!/usr/bin/env bash
# Host (ID 1): WiFi access point via NetworkManager.
# ipv4.method=shared starts NetworkManager's built-in dnsmasq (DHCP for the
# clients and any phone/laptop that joins to use the web UI) — no hostapd needed.
set -euo pipefail

echo "==> [20-network-host] Configuring WiFi AP '$BF_SSID' on 192.168.4.1/24"
nmcli connection delete blaufilter-ap 2>/dev/null || true
nmcli connection add type wifi ifname wlan0 con-name blaufilter-ap autoconnect yes \
    ssid "$BF_SSID" \
    802-11-wireless.mode ap \
    802-11-wireless.band bg \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$BF_PSK" \
    ipv4.method shared \
    ipv4.addresses 192.168.4.1/24 \
    ipv6.method disabled
nmcli connection up blaufilter-ap
