#!/usr/bin/env bash
# Reading and writing /etc/blaufilter/config from the maintenance menu.
set -u
source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
trap cleanup_sandbox EXIT

make_sandbox
load_setup

describe "cfg_get"
assert_eq "$(cfg_get device_id '?')" "2" "liest einen vorhandenen Wert"
assert_eq "$(cfg_get gibtsnicht 'STANDARD')" "STANDARD" "fällt auf den Standard zurück"
assert_eq "$(cfg_get gibtsnicht)" "" "ohne Standard leer"

describe "cfg_set"
cfg_set device_id 5
assert_eq "$(cfg_get device_id '?')" "5" "ersetzt einen vorhandenen Wert"
assert_eq "$(grep -c '^device_id' "$CONFIG")" "1" "ohne die Zeile zu verdoppeln"
cfg_set drift_threshold 2.5
assert_eq "$(cfg_get drift_threshold '?')" "2.5" "hängt einen neuen Wert an"

describe "cfg_get verträgt Sonderzeichen"
cfg_set ssid 'Netz%mit&Zeichen'
assert_eq "$(cfg_get ssid '?')" 'Netz%mit&Zeichen' "Prozentzeichen bleiben erhalten"

describe "repo_version"
assert_contains "$(repo_version)" "unbekannt" "meldet 'unbekannt' ohne Git-Repository"
cfg_set repo_dir "$REPO_ROOT"
assert_not_contains "$(repo_version)" "unbekannt" "nennt Zweig und Commit im echten Repository"

describe "blaufilter_profile folgt der Rolle"
cfg_set role client
assert_eq "$(blaufilter_profile)" "blaufilter" "Client nutzt das Client-Profil"
cfg_set role host
assert_eq "$(blaufilter_profile)" "blaufilter-ap" "Host nutzt das AP-Profil"

finish
