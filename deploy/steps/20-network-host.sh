#!/usr/bin/env bash
# Host (ID 1): WiFi access point via NetworkManager.
# ipv4.method=shared starts NetworkManager's built-in dnsmasq (DHCP for the
# clients and any phone/laptop that joins to use the web UI) — no hostapd needed.
set -euo pipefail

echo "==> [20-network-host] Configuring WiFi AP '$BF_SSID' on 192.168.4.1/24"
# NOTE: all apt packages (dnsmasq-base, avahi) are installed in 10-base.sh —
# once the AP is up, this device has no internet connectivity anymore.
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

echo "==> [20-network-host] Name resolution: blaufilter.local -> 192.168.4.1"
# Windows/Android resolve via the DHCP-provided DNS (NM's shared dnsmasq)...
install -d /etc/NetworkManager/dnsmasq-shared.d
cat > /etc/NetworkManager/dnsmasq-shared.d/blaufilter.conf <<'EOF'
address=/blaufilter.local/192.168.4.1
EOF
systemctl restart NetworkManager
nmcli connection up blaufilter-ap || true

# ...while Apple devices resolve .local exclusively via mDNS (avahi alias,
# packages installed in 10-base.sh)
install -m 644 "$BF_REPO_DIR/deploy/systemd/blaufilter-mdns-alias.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable blaufilter-mdns-alias
systemctl restart blaufilter-mdns-alias
