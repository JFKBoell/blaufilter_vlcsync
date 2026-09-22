#!/usr/bin/env bash
# Screen resolution: reading modes from the driver and rewriting cmdline.txt.
# cmdline.txt decides whether the Pi boots at all, so this is the file where a
# mistake hurts most.
set -u
source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
trap cleanup_sandbox EXIT

make_sandbox
load_setup

describe "Bildschirme und Modi aus dem Treiber"
assert_eq "$(connected_outputs)" "HDMI-A-1" "listet nur angeschlossene Ausgänge"
printf 'disconnected\n' > "$DRM_ROOT/card1-HDMI-A-1/status"
assert_eq "$(connected_outputs)" "" "nicht angeschlossen wird ausgelassen"
printf 'connected\n' > "$DRM_ROOT/card1-HDMI-A-1/status"
assert_contains "$(drm_dir_for HDMI-A-1)" "card1-HDMI-A-1" "findet das Verzeichnis"
assert_rc "$(drm_dir_for HDMI-X-9 >/dev/null; echo $?)" "1" "unbekannter Ausgang schlägt fehl"

describe "current_video_setting ohne vorhandenen Eintrag"
# Dieser Fall hat das Menü früher beendet: grep findet nichts, Exitcode 1,
# pipefail plus set -e killten das Script.
result=$(current_video_setting HDMI-A-1); rc=$?
assert_rc "$rc" "0" "bricht nicht ab, wenn nichts gesetzt ist"
assert_eq "$result" "" "liefert leer"

describe "set_video_setting"
set_video_setting HDMI-A-1 "3840x2160@30"
assert_file_contains "$CMDLINE" "video=HDMI-A-1:3840x2160@30" "trägt den Modus ein"
assert_eq "$(wc -l < "$CMDLINE")" "1" "bleibt einzeilig"
assert_eq "$(current_video_setting HDMI-A-1)" "3840x2160@30" "liest ihn wieder aus"

set_video_setting HDMI-A-1 "1920x1080@60"
assert_eq "$(grep -o 'video=' "$CMDLINE" | wc -l)" "1" "ersetzt statt zu verdoppeln"
assert_file_contains "$CMDLINE" "1920x1080@60" "mit dem neuen Wert"

set_video_setting HDMI-A-2 "1280x720"
assert_file_contains "$CMDLINE" "video=HDMI-A-1:1920x1080@60" "erster Anschluss bleibt"
assert_file_contains "$CMDLINE" "video=HDMI-A-2:1280x720" "zweiter kommt hinzu"

set_video_setting HDMI-A-1 ""
assert_not_contains "$(cat "$CMDLINE")" "HDMI-A-1" "entfernt den gewählten Anschluss"
assert_file_contains "$CMDLINE" "video=HDMI-A-2:1280x720" "und nur diesen"
assert_file_contains "$CMDLINE" "root=PARTUUID" "Boot-Parameter bleiben unangetastet"

describe "Schutz vor einer unbootbaren Boot-Zeile"
printf 'quiet splash\n' > "$CMDLINE"
set_video_setting HDMI-A-1 "1920x1080"; rc=$?
assert_rc "$rc" "1" "verweigert eine Zeile ohne root="
assert_eq "$(cat "$CMDLINE")" "quiet splash" "Datei bleibt unverändert"

describe "Menü: Voreinstellung wählen"
printf 'console=tty1 root=PARTUUID=ab12-02 rootwait\n' > "$CMDLINE"
queue_answers "3840x2160@30" YES YES NO
menu_resolution
assert_file_contains "$CMDLINE" "video=HDMI-A-1:3840x2160@30" "4K-Voreinstellung landet in cmdline.txt"
assert_file_contains "$SANDBOX/boot/config.txt" "dtoverlay" "config.txt unangetastet bei 30 Hz"

describe "Menü: 4K mit 60 Hz bietet hdmi_enable_4kp60 an"
queue_answers "MEHR" "3840x2160" "60" YES YES NO
menu_resolution
assert_file_contains "$SANDBOX/boot/config.txt" "hdmi_enable_4kp60=1" "Schalter wird eingetragen"

describe "Menü: automatisch entfernt die Festlegung"
queue_answers "" YES NO
menu_resolution
assert_not_contains "$(cat "$CMDLINE")" "video=" "kein erzwungener Modus mehr"

describe "Menü: Abbruch ändert nichts"
before=$(cat "$CMDLINE")
queue_answers CANCEL
menu_resolution
assert_eq "$(cat "$CMDLINE")" "$before" "Escape lässt die Boot-Zeile in Ruhe"

finish
