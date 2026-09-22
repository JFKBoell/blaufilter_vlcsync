#!/usr/bin/env bash
# The real deploy steps, run against a sandbox instead of a Pi.
set -u
source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
trap cleanup_sandbox EXIT

make_sandbox

# ------------------------------------------------------- 05-config.sh

run_config_step() {  # id role ... via environment
    BF_CONFIG_PATH="$CONFIG" BF_ID=$1 BF_ROLE=$2 BF_PIN=${3:-1234} \
    BF_SSID=${4:-Blaufilter} BF_OPEN=${5:-0} BF_TXPOWER=${6:-} BF_REPO_DIR=${7:-/repo} \
        bash "$REPO_ROOT/deploy/steps/05-config.sh" >/dev/null
}

describe "05-config: verwaltete Werte schreiben"
run_config_step 3 client
assert_file_contains "$CONFIG" "device_id = 3" "Geräte-ID"
assert_file_contains "$CONFIG" "role = client" "Rolle"

describe "05-config: handgepflegte Werte überleben"
printf 'drift_threshold = 1.8\ncooldown_s = 25\nrandom_start = no\n' >> "$CONFIG"
run_config_step 1 host 9999 MeinNetz 1 10 /pfad
assert_file_contains "$CONFIG" "drift_threshold = 1.8" "drift_threshold bleibt"
assert_file_contains "$CONFIG" "cooldown_s = 25" "cooldown_s bleibt"
assert_file_contains "$CONFIG" "random_start = no" "random_start bleibt"
assert_file_contains "$CONFIG" "device_id = 1" "verwaltete Werte sind neu"
assert_file_contains "$CONFIG" "ssid = MeinNetz" "SSID ist neu"

describe "05-config: wiederholte Läufe verdoppeln nichts"
run_config_step 1 host
run_config_step 1 host
assert_eq "$(grep -c 'drift_threshold' "$CONFIG")" "1" "drift_threshold genau einmal"
assert_eq "$(grep -c 'device_id' "$CONFIG")" "1" "device_id genau einmal"
assert_eq "$(grep -c '^\[blaufilter\]' "$CONFIG")" "1" "eine Abschnittsüberschrift"

describe "05-config: die Datei bleibt für den Controller lesbar"
python3 - "$CONFIG" <<'PY' && ok "configparser liest sie" || fail "configparser liest sie"
import configparser, sys
p = configparser.ConfigParser(interpolation=None)
p.read(sys.argv[1])
assert p.has_section("blaufilter"), "Abschnitt fehlt"
assert p["blaufilter"]["device_id"] == "1"
PY

# ------------------------------------------------------- 50-splash.sh

# The step writes to a fixed theme path, so it runs against a copy with the
# paths bent into the sandbox.
splash_step="$SANDBOX/50-splash.sh"
python3 - "$REPO_ROOT/deploy/steps/50-splash.sh" "$splash_step" "$SANDBOX" <<'PY'
import sys
src, dst, sandbox = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(src).read()
text = text.replace("SPLASH_TARGET=/usr/share/plymouth/themes/pix/splash.png",
                    f"SPLASH_TARGET={sandbox}/theme/splash.png")
text = text.replace("BOOT_DIR=/boot/firmware\n[[ -d $BOOT_DIR ]] || BOOT_DIR=/boot",
                    f"BOOT_DIR={sandbox}/boot")
open(dst, "w").write(text)
PY
printf 'ALT\n' > "$SPLASH_TARGET"
printf 'NEU-BILD-INHALT\n' > "$SANDBOX/neu.png"

describe "50-splash: ohne initramfs"
printf 'dtoverlay=vc4-kms-v3d\n' > "$BOOT_DIR/config.txt"
out=$(BF_SPLASH="$SANDBOX/neu.png" bash "$splash_step" 2>&1); rc=$?
assert_rc "$rc" "0" "meldet Erfolg"
assert_contains "$out" "No initramfs in use" "baut gar kein initramfs"
assert_eq "$(cat "$SPLASH_TARGET")" "NEU-BILD-INHALT" "Startbild ist gesetzt"
assert_eq "$(cat "$SPLASH_TARGET.orig")" "ALT" "Original ist gesichert"

describe "50-splash: initramfs schlägt fehl (der Fall vom Gerät)"
printf 'auto_initramfs=1\n' > "$BOOT_DIR/config.txt"
printf 'ALT2\n' > "$SPLASH_TARGET"
cat > "$SANDBOX/bin/update-initramfs" <<'EOF'
#!/usr/bin/env bash
echo "mkinitramfs: failed to determine device for /" >&2
exit 1
EOF
chmod +x "$SANDBOX/bin/update-initramfs"
out=$(BF_SPLASH="$SANDBOX/neu.png" bash "$splash_step" 2>&1); rc=$?
assert_rc "$rc" "0" "gilt trotzdem als erfolgreich"
assert_contains "$out" "WARNUNG" "warnt statt zu scheitern"
assert_contains "$out" "MODULES=most" "nennt die Abhilfe"
assert_eq "$(cat "$SPLASH_TARGET")" "NEU-BILD-INHALT" "Startbild ist gesetzt"

describe "50-splash: fehlende Datei ist ein echter Fehler"
out=$(BF_SPLASH="$SANDBOX/gibtsnicht.png" bash "$splash_step" 2>&1); rc=$?
assert_rc "$rc" "1" "bricht ab"

finish
