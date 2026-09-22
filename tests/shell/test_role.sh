#!/usr/bin/env bash
# Role changes: the clone case. Only what a role consists of may be touched.
set -u
source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
trap cleanup_sandbox EXIT

make_sandbox
load_setup

describe "Wechsel zum Host"
apply_role 1 host
log=$(stub_log)
assert_eq "$(cfg_get device_id '?')" "1" "Geräte-ID in der Konfiguration"
assert_eq "$(cfg_get role '?')" "host" "Rolle in der Konfiguration"
assert_contains "$log" "hostnamectl set-hostname blaufilter-1" "setzt den Rechnernamen"
assert_contains "$log" "STEP 20-network-host.sh" "führt den Host-Netzwerkschritt aus"
assert_contains "$log" "STEP 40-controller.sh" "installiert den Controller"
assert_not_contains "$log" "STEP 20-network-client.sh" "nicht den Client-Schritt"
assert_contains "$log" "BF_SSID=Blaufilter" "reicht die WLAN-Einstellungen durch"
assert_contains "$log" "BF_PSK=geheim123" "reicht das gespeicherte Passwort durch"

describe "Wechsel zum Client"
: > "$STUB_LOG"
apply_role 3 client
log=$(stub_log)
assert_eq "$(cfg_get device_id '?')" "3" "neue Geräte-ID"
assert_eq "$(cfg_get role '?')" "client" "neue Rolle"
assert_contains "$log" "STEP 20-network-client.sh" "führt den Client-Netzwerkschritt aus"
assert_not_contains "$log" "STEP 40-controller.sh" "installiert KEINEN Controller"
assert_contains "$log" "BF_ID=3" "reicht die Geräte-ID durch"

describe "Nichts Rollenfremdes wird angefasst"
assert_not_contains "$log" "STEP 26-txpower.sh" "keine Sendeleistung"
assert_not_contains "$log" "STEP 50-splash.sh" "kein Startbild"
assert_not_contains "$log" "install.sh" "nicht das Installationsscript"

describe "Handgepflegte Einstellungen überleben den Rollenwechsel"
cfg_set drift_threshold 1.8
apply_role 1 host
assert_eq "$(cfg_get drift_threshold '?')" "1.8" "drift_threshold bleibt"

describe "WPA2 ohne lesbares Passwort bricht ab, bevor etwas passiert"
: > "$STUB_LOG"
queue_answers CANCEL          # Nutzer bricht die Passwortabfrage ab
export NMCLI_PSK=""           # kein Passwort in den Profilen hinterlegt
apply_role 2 client; rc=$?
unset NMCLI_PSK
assert_rc "$rc" "1" "meldet Abbruch"
assert_not_contains "$(stub_log)" "STEP " "kein Schritt wurde ausgeführt"
assert_eq "$(cfg_get role '?')" "host" "Rolle unverändert"

describe "Offenes WLAN braucht kein Passwort"
cfg_set open_wifi 1
: > "$STUB_LOG"
NMCLI_PSK="" apply_role 4 client; rc=$?
assert_rc "$rc" "0" "läuft durch"
assert_contains "$(stub_log)" "STEP 20-network-client.sh" "Netzwerkschritt lief"

finish
