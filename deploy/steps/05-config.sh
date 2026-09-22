#!/usr/bin/env bash
# Writes /etc/blaufilter/config from the installer's settings.
#
# Only the keys this script owns are rewritten. Everything else in the file —
# drift_threshold, cooldown_s, random_start and friends, tuned on the device —
# is carried over, so running the installer again does not quietly reset it.
set -euo pipefail

CONFIG_PATH=${BF_CONFIG_PATH:-/etc/blaufilter/config}
MANAGED_KEYS="device_id role debug_pin ssid open_wifi txpower repo_dir"

echo "==> [05-config] Writing $CONFIG_PATH"
install -d "$(dirname "$CONFIG_PATH")"

PRESERVED=""
if [[ -f $CONFIG_PATH ]]; then
    PRESERVED=$(awk -v keys="$MANAGED_KEYS" '
        BEGIN { split(keys, k, " "); for (i in k) managed[k[i]] = 1 }
        /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=/ {
            key = $0
            sub(/[[:space:]]*=.*/, "", key)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
            if (!(key in managed)) print
        }
    ' "$CONFIG_PATH")
    if [[ -n $PRESERVED ]]; then
        echo "    keeping: $(echo "$PRESERVED" | tr '\n' ' ')"
    fi
fi

# ssid/open_wifi/txpower/repo_dir are not read by the controller — they let the
# setup tool work with the settings already in use.
{
    echo "[blaufilter]"
    echo "device_id = $BF_ID"
    echo "role = $BF_ROLE"
    echo "debug_pin = ${BF_PIN:-}"
    echo "ssid = ${BF_SSID:-Blaufilter}"
    echo "open_wifi = ${BF_OPEN:-0}"
    echo "txpower = ${BF_TXPOWER:-}"
    echo "repo_dir = ${BF_REPO_DIR:-}"
    [[ -n $PRESERVED ]] && printf '%s\n' "$PRESERVED"
} > "$CONFIG_PATH"
