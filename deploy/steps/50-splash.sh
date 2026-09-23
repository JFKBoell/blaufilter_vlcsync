#!/usr/bin/env bash
# Optional: custom boot splash. Replaces the Plymouth "pix" theme image.
# Recommended resolution: the display's native resolution (3840x2160 for 4K
# screens; 1920x1080 also works and gets scaled). PNG format.
set -euo pipefail

SPLASH_TARGET=/usr/share/plymouth/themes/pix/splash.png
BOOT_DIR=/boot/firmware
[[ -d $BOOT_DIR ]] || BOOT_DIR=/boot

if [[ ! -f "$BF_SPLASH" ]]; then
    echo "==> [50-splash] Splash file not found: $BF_SPLASH" >&2
    exit 1
fi

echo "==> [50-splash] Installing custom boot splash"
if [[ -f "$SPLASH_TARGET" && ! -f "$SPLASH_TARGET.orig" ]]; then
    cp "$SPLASH_TARGET" "$SPLASH_TARGET.orig"
fi
cp "$BF_SPLASH" "$SPLASH_TARGET"

# Plymouth reads the theme from the root filesystem, so the image above is
# already in place. An initramfs only carries a copy, and Raspberry Pi OS
# boots without one unless config.txt asks for it — so only rebuild when this
# system actually uses one, and never fail the step over it: update-initramfs
# is known to break on Pi OS with MODULES=dep ("failed to determine device
# for /"), which has nothing to do with the splash.
if grep -qE '^[[:space:]]*(auto_initramfs=1|initramfs[[:space:]])' \
       "$BOOT_DIR/config.txt" 2>/dev/null; then
    if command -v update-initramfs >/dev/null 2>&1; then
        echo "==> [50-splash] Rebuilding the initramfs (this system boots with one)"
        if ! update-initramfs -u; then
            echo
            echo "==> [50-splash] WARNUNG: initramfs konnte nicht neu gebaut werden." >&2
            echo "    Das Startbild selbst ist gesetzt und wird angezeigt." >&2
            echo "    Ursache ist meist MODULES=dep in /etc/initramfs-tools/initramfs.conf;" >&2
            echo "    Abhilfe: dort auf MODULES=most stellen und 'sudo update-initramfs -u'." >&2
        fi
    fi
else
    echo "==> [50-splash] No initramfs in use — nothing else to do"
fi
