#!/usr/bin/env bash
# VLC launcher for the blaufilter-vlc unit. Reads /boot/firmware/blaufilter.txt
# (FAT boot partition — editable from any computer by plugging in the SD card)
# as an emergency override: fullscreen=no plays in a window, autostart=no
# skips VLC entirely so the desktop stays usable.
set -u

CONF="${BLAUFILTER_BOOT_CONF:-}"
if [[ -z "$CONF" ]]; then
    for candidate in /boot/firmware/blaufilter.txt /boot/blaufilter.txt; do
        if [[ -f "$candidate" ]]; then
            CONF="$candidate"
            break
        fi
    done
fi

FULLSCREEN=yes
AUTOSTART=yes

if [[ -n "$CONF" && -f "$CONF" ]]; then
    # Tolerant parsing: strip CR (file may be edited on Windows), spaces, case
    while IFS='=' read -r key value; do
        key=$(printf '%s' "$key" | tr -d '\r[:space:]' | tr '[:upper:]' '[:lower:]')
        value=$(printf '%s' "$value" | tr -d '\r[:space:]' | tr '[:upper:]' '[:lower:]')
        case "$key" in
            fullscreen) FULLSCREEN="$value" ;;
            autostart)  AUTOSTART="$value" ;;
        esac
    done < "$CONF"
fi

is_off() { [[ "$1" == "no" || "$1" == "0" || "$1" == "false" || "$1" == "nein" ]]; }

if is_off "$AUTOSTART"; then
    echo "blaufilter: autostart disabled via $CONF — not starting VLC"
    exit 0
fi

ARGS=(--extraintf lua --rc-host 0.0.0.0:4212
      --loop --no-osd --no-video-title-show --no-random)
if ! is_off "$FULLSCREEN"; then
    ARGS+=(--fullscreen)
fi
ARGS+=(/opt/blaufilter/video/main.mp4)

if [[ -n "${BLAUFILTER_PRINT_ONLY:-}" ]]; then
    echo "cvlc ${ARGS[*]}"
    exit 0
fi
exec /usr/bin/cvlc "${ARGS[@]}"
