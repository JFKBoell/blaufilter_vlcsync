#!/usr/bin/env bash
# Host (ID 1): WiFi access point via NetworkManager.
# ipv4.method=shared starts NetworkManager's built-in dnsmasq (DHCP for the
# clients and any phone/laptop that joins to use the web UI) — no hostapd needed.
set -euo pipefail

echo "==> [20-network-host] Ensuring dnsmasq-base (NetworkManager's DHCP backend)"
apt-get install -y --no-install-recommends dnsmasq-base

echo "==> [20-network-host] Configuring WiFi AP '$BF_SSID' on 192.168.4.1/24"
# Security is pinned to plain WPA2/CCMP with PMF off and a fixed channel:
# without this, NetworkManager may negotiate WPA1/TKIP mixed mode or
# WPA3-transition/PMF, which many clients fail to join. WPS must be off,
# otherwise the beacon advertises it and Windows asks for a PIN instead of
# the password while some phones misreport "wrong password".
nmcli connection delete blaufilter-ap 2>/dev/null || true
nmcli connection add type wifi ifname wlan0 con-name blaufilter-ap autoconnect yes \
    ssid "$BF_SSID" \
    802-11-wireless.mode ap \
    802-11-wireless.band bg \
    802-11-wireless.channel 6 \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$BF_PSK" \
    wifi-sec.proto rsn \
    wifi-sec.pairwise ccmp \
    wifi-sec.group ccmp \
    wifi-sec.pmf disable \
    wifi-sec.wps-method disabled \
    ipv4.method shared \
    ipv4.addresses 192.168.4.1/24 \
    ipv6.method disabled
nmcli connection up blaufilter-ap
