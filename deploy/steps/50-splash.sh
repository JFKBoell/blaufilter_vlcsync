#!/usr/bin/env bash
# Optional: custom boot splash. Replaces the Plymouth "pix" theme image.
# Recommended resolution: the display's native resolution (3840x2160 for 4K
# screens; 1920x1080 also works and gets scaled). PNG format.
set -euo pipefail

SPLASH_TARGET=/usr/share/plymouth/themes/pix/splash.png

if [[ ! -f "$BF_SPLASH" ]]; then
    echo "==> [50-splash] Splash file not found: $BF_SPLASH" >&2
    exit 1
fi

echo "==> [50-splash] Installing custom boot splash"
if [[ -f "$SPLASH_TARGET" && ! -f "$SPLASH_TARGET.orig" ]]; then
    cp "$SPLASH_TARGET" "$SPLASH_TARGET.orig"
fi
cp "$BF_SPLASH" "$SPLASH_TARGET"

# Bookworm boots Plymouth from the initramfs — rebuild it so the new image shows
if command -v update-initramfs >/dev/null 2>&1; then
    update-initramfs -u
fi
