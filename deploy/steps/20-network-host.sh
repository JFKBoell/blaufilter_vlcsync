#!/usr/bin/env bash
# Host (ID 1): WiFi access point via NetworkManager.
# ipv4.method=shared starts NetworkManager's built-in dnsmasq (DHCP for the
# clients and any phone/laptop that joins to use the web UI) — no hostapd needed.
set -euo pipefail

echo "==> [20-network-host] Name resolution config: blaufilter.local -> 192.168.4.1"
# Written BEFORE the AP comes up: NM's shared dnsmasq reads this directory
# when the shared connection activates — no NetworkManager restart needed.
# Serves Windows/Android via the DHCP-provided DNS.
install -d /etc/NetworkManager/dnsmasq-shared.d
cat > /etc/NetworkManager/dnsmasq-shared.d/blaufilter.conf <<'EOF'
address=/blaufilter.local/192.168.4.1
# Captive portal: every name resolves to the host, so a joining phone's
# connectivity check reaches the controller and the page opens by itself.
# (This network has no uplink anyway — nothing else could be resolved.)
address=/#/192.168.4.1
EOF

if [[ "${BF_OPEN:-0}" == "1" ]]; then
    echo "==> [20-network-host] Configuring OPEN WiFi AP '$BF_SSID' on 192.168.4.1/24"
    # An open network must carry NO wireless-security setting at all.
    # (wifi-sec.key-mgmt=none would mean WEP in NetworkManager, not "open".)
    SEC_ARGS=()
else
    echo "==> [20-network-host] Configuring WiFi AP '$BF_SSID' on 192.168.4.1/24"
    # Security is pinned to plain WPA2/CCMP with PMF off: without this,
    # NetworkManager may negotiate WPA1/TKIP mixed mode or WPA3-transition/PMF,
    # which many clients fail to join. WPS must be off, otherwise the beacon
    # advertises it and Windows asks for a PIN instead of the password while
    # some phones misreport "wrong password".
    SEC_ARGS=(
        wifi-sec.key-mgmt wpa-psk
        wifi-sec.psk "$BF_PSK"
        wifi-sec.proto rsn
        wifi-sec.pairwise ccmp
        wifi-sec.group ccmp
        wifi-sec.pmf disable
        wifi-sec.wps-method disabled
    )
fi

# NOTE: all apt packages (dnsmasq-base, avahi) are installed in 10-base.sh —
# once the AP is up, this device has no internet connectivity anymore.
nmcli connection delete blaufilter-ap 2>/dev/null || true
# Role switch / cloned SD card: a leftover CLIENT profile must not compete
nmcli connection delete blaufilter 2>/dev/null || true
nmcli connection add type wifi ifname wlan0 con-name blaufilter-ap autoconnect yes \
    ssid "$BF_SSID" \
    802-11-wireless.mode ap \
    802-11-wireless.band bg \
    802-11-wireless.channel 6 \
    "${SEC_ARGS[@]}" \
    ipv4.method shared \
    ipv4.addresses 192.168.4.1/24 \
    ipv6.method disabled
nmcli connection up blaufilter-ap

# Apple devices resolve .local exclusively via mDNS (avahi alias,
# packages installed in 10-base.sh)
install -m 644 "$BF_REPO_DIR/deploy/systemd/blaufilter-mdns-alias.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable blaufilter-mdns-alias
systemctl restart blaufilter-mdns-alias
