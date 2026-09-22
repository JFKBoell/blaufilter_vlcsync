#!/usr/bin/env bash
# WiFi: listing known networks, joining them, and getting back.
# This is the one area where a mistake can strand a device outside its own
# network, so the safeguards get the most attention here.
set -u
source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
trap cleanup_sandbox EXIT

make_sandbox
load_setup

describe "Bekannte Netze"
profiles=$(known_wifi_profiles)
assert_contains "$profiles" "Werkstatt:no" "listet fremde Netze mit Autoconnect-Status"
assert_contains "$profiles" "Buero:yes" "kennzeichnet dauerhaft bevorzugte"
assert_not_contains "$profiles" "blaufilter" "blendet die eigenen Profile aus"
assert_not_contains "$profiles" "Wired" "blendet Kabelverbindungen aus"

describe "Beitritt nur für jetzt"
: > "$STUB_LOG"
join_network Werkstatt 0
log=$(stub_log)
assert_contains "$log" "connection modify Werkstatt connection.autoconnect no" "Autoconnect bleibt aus"
assert_contains "$log" "autoconnect-priority 0" "ohne Vorrang"
assert_contains "$log" "connection up Werkstatt" "aktiviert das Netz"
assert_contains "$log" "wieder im Blaufilter-WLAN" "weist auf den Neustart hin"

describe "Dauerhafter Beitritt"
: > "$STUB_LOG"
join_network Werkstatt 1
log=$(stub_log)
assert_contains "$log" "connection.autoconnect yes" "Autoconnect an"
assert_contains "$log" "autoconnect-priority 10" "mit Vorrang vor dem Blaufilter-Profil"
assert_contains "$log" "wird künftig bevorzugt" "sagt, was das bedeutet"

describe "Gescheiterter Beitritt kehrt ins eigene Netz zurück"
: > "$STUB_LOG"
cfg_set role client
NMCLI_UP_OK="blaufilter" join_network Werkstatt 0; rc=$?
log=$(stub_log)
assert_rc "$rc" "1" "meldet den Fehlschlag"
assert_contains "$log" "Beitritt fehlgeschlagen" "sagt es auch im Protokoll"
assert_contains "$log" "connection up blaufilter" "aktiviert das Blaufilter-Profil wieder"

describe "Rückkehr hebt die Bevorzugung auf"
: > "$STUB_LOG"
queue_answers YES YES        # zurückwechseln? ja; Bevorzugung aufheben? ja
menu_wifi_back
log=$(stub_log)
assert_contains "$log" "connection modify Buero connection.autoconnect no" "setzt bevorzugte Netze zurück"
assert_contains "$log" "connection up blaufilter" "wechselt zurück"

describe "Rückkehr ohne Aufheben lässt die Profile in Ruhe"
: > "$STUB_LOG"
queue_answers YES NO
menu_wifi_back
assert_not_contains "$(stub_log)" "modify Buero" "Bevorzugung bleibt, wenn man nein sagt"

describe "Abbruch wechselt nicht"
: > "$STUB_LOG"
queue_answers NO
menu_wifi_back
assert_not_contains "$(stub_log)" "connection up" "nichts aktiviert"

describe "Sendeleistung"
: > "$STUB_LOG"
apply_txpower "$SANDBOX/repo" 10
assert_contains "$(stub_log)" "STEP 26-txpower.sh" "ruft den Sendeleistungs-Schritt auf"
: > "$STUB_LOG"
apply_txpower "$SANDBOX/repo" ""
assert_contains "$(stub_log)" "iw dev wlan0 set txpower auto" "stellt auf Treiber-Maximum zurück"

finish
