#!/usr/bin/env bash
# Blaufilter maintenance menu. Installed as /usr/local/sbin/blaufilter-setup.
#
# Everything that changes role or WiFi re-runs the installer instead of poking
# at NetworkManager here: the installer already knows how to clean up leftovers
# (a cloned SD card carrying the host's AP profile, for instance), and one
# code path means the two can never drift apart.
# Deliberately no 'set -e' / 'pipefail': in an interactive menu a command that
# legitimately finds nothing (a grep for a setting that is not there yet) must
# not tear the whole tool down mid-session. Every action checks its own result.
set -u

CONFIG=/etc/blaufilter/config
VIDEO=/opt/blaufilter/video/main.mp4
SPLASH_TARGET=/usr/share/plymouth/themes/pix/splash.png
TITLE="Blaufilter"

BOOT_DIR=/boot/firmware
[[ -d $BOOT_DIR ]] || BOOT_DIR=/boot
CMDLINE="$BOOT_DIR/cmdline.txt"

if [[ $EUID -ne 0 ]]; then
    echo "Bitte mit sudo starten: sudo blaufilter-setup" >&2
    exit 1
fi
if ! command -v whiptail >/dev/null; then
    echo "whiptail fehlt (Paket 'whiptail')." >&2
    exit 1
fi

# --------------------------------------------------------------- config i/o

cfg_get() {  # key [default]
    local v=""
    [[ -f $CONFIG ]] && v=$(sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$CONFIG" | head -1)
    echo "${v:-${2-}}"
}

cfg_set() {  # key value
    [[ -f $CONFIG ]] || { install -d /etc/blaufilter; printf '[blaufilter]\n' > "$CONFIG"; }
    if grep -qE "^[[:space:]]*$1[[:space:]]*=" "$CONFIG"; then
        sed -i "s|^[[:space:]]*$1[[:space:]]*=.*|$1 = $2|" "$CONFIG"
    else
        printf '%s = %s\n' "$1" "$2" >> "$CONFIG"
    fi
}

BF_USER=$(cfg_get user "${SUDO_USER:-pi}")

msg()  { whiptail --title "$TITLE" --msgbox "$1" "${2:-12}" 74; }
yes_no() { whiptail --title "$TITLE" --yesno "$1" "${2:-12}" 74; }

# VLC runs as a --user unit, so it needs the owning user's session bus
user_systemctl() {
    local uid; uid=$(id -u "$BF_USER")
    sudo -u "$BF_USER" XDG_RUNTIME_DIR="/run/user/$uid" systemctl --user "$@"
}

current_psk() {
    local psk=""
    for profile in blaufilter-ap blaufilter; do
        psk=$(nmcli --show-secrets -g 802-11-wireless-security.psk \
              connection show "$profile" 2>/dev/null || true)
        [[ -n $psk ]] && break
    done
    echo "$psk"
}

# ------------------------------------------------------------------- status

svc_state() {  # unit -> "aktiv" / "GESTOPPT" / "nicht installiert"
    if ! systemctl cat "$1" >/dev/null 2>&1; then echo "nicht installiert"; return; fi
    if systemctl is-active --quiet "$1"; then echo "aktiv"; else echo "GESTOPPT"; fi
}

status_report() {
    local id role ssid ip wifi txp report devices
    id=$(cfg_get device_id "?")
    role=$(cfg_get role "?")
    ssid=$(cfg_get ssid "?")
    ip=$(ip -4 -o addr show wlan0 2>/dev/null | awk '{print $4}' | head -1)
    wifi=$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null \
           | awk -F: '$2=="wlan0"{print $1}' | head -1)
    txp=$(iw dev wlan0 info 2>/dev/null | awk '/txpower/{print $2, $3}')

    report="Gerät ${id}  ·  Rolle: ${role}\n"
    report+="WLAN '${ssid}' · Profil: ${wifi:-keins aktiv}\n"
    report+="IP: ${ip:-keine}   Sendeleistung: ${txp:-unbekannt}\n\n"
    report+="Dienste:\n"
    report+="  VLC (User-Unit):   $(user_systemctl is-active blaufilter-vlc 2>/dev/null || echo GESTOPPT)\n"
    report+="  Video-Agent:       $(svc_state blaufilter-agent)\n"
    report+="  Port-Sperre:       $(svc_state blaufilter-firewall)\n"
    if [[ $role == host ]]; then
        report+="  Controller:        $(svc_state blaufilter-controller)\n"
        report+="  mDNS-Alias:        $(svc_state blaufilter-mdns-alias)\n\n"
        # Quoted heredoc: the Python source reaches the interpreter untouched,
        # so it can use its own quoting freely.
        devices=$(python3 - "$(cfg_get web_port 80)" <<'PY' 2>/dev/null
import json, sys, urllib.request
try:
    url = "http://127.0.0.1:%s/api/status" % sys.argv[1]
    with urllib.request.urlopen(url, timeout=2) as resp:
        d = json.load(resp)
except Exception:
    print("  Controller antwortet nicht")
    raise SystemExit
rows = [x for x in d.get("devices", []) if x.get("connected")]
print("  %d verbunden · Zustand: %s" % (len(rows), d.get("health")))
for r in rows:
    drift = r.get("drift_ms")
    mark = " (Master)" if r.get("is_master") else ""
    tail = "" if drift is None else " · Drift %+d ms" % drift
    print("  Gerät %s%s: %s%s" % (r.get("id"), mark, r.get("address"), tail))
for issue in d.get("issues", []):
    print("  ! " + issue)
PY
)
        report+="Synchronisation:\n${devices:-  Controller antwortet nicht}\n"
    fi
    report+="\nVideo: "
    if [[ -f $VIDEO ]]; then
        report+="$(du -h "$VIDEO" | cut -f1) · $(date -r "$VIDEO" '+%d.%m.%Y %H:%M')"
    else
        report+="FEHLT ($VIDEO)"
    fi
    whiptail --title "$TITLE — Status" --msgbox "$report" 24 76
}

# ------------------------------------------------------------------ actions

# Runs a command detached from this terminal and streams its log.
#
# Reconfiguring the AP drops the very SSH connection this menu may be running
# over. Without setsid the step would die of SIGHUP — possibly between deleting
# and recreating the WiFi profile, which would leave the device unreachable.
# Detached it always runs to completion; reconnect and the log shows the result.
run_detached() {  # headline command...
    local headline=$1; shift
    local log=/var/log/blaufilter-setup.log
    : > "$log"
    rm -f "$log.done"

    clear
    echo "== $headline =="
    echo "   Läuft unabhängig von dieser Sitzung weiter (Protokoll: $log)"
    echo
    setsid --fork bash -c "$(printf '%q ' "$@") >>'$log' 2>&1; echo \$? >'$log.done'"

    tail -n +1 -f "$log" 2>/dev/null &
    local tailpid=$!
    while [[ ! -f $log.done ]]; do sleep 1; done
    sleep 1
    kill "$tailpid" 2>/dev/null || true

    local rc; rc=$(cat "$log.done" 2>/dev/null || echo 1)
    echo
    if [[ $rc == 0 ]]; then
        read -rp "Fertig. Enter drücken…" _
    else
        read -rp "FEHLGESCHLAGEN (Code $rc) — Ausgabe oben prüfen. Enter drücken…" _
    fi
    return "$rc"
}

# Prints the repository path on stdout, asking for it if the config has none.
repo_path() {
    local repo; repo=$(cfg_get repo_dir "")
    if [[ -z $repo || ! -x $repo/deploy/install.sh ]]; then
        repo=$(whiptail --title "$TITLE" --inputbox \
              "Pfad zum blaufilter_vlcsync-Repository:" 10 74 "/home/$BF_USER/blaufilter_vlcsync" \
              3>&1 1>&2 2>&3) || return 1
        [[ -x $repo/deploy/install.sh ]] || { msg "Kein install.sh unter:\n$repo"; return 1; }
        cfg_set repo_dir "$repo"
    fi
    echo "$repo"
}

reinstall() {  # extra install.sh arguments
    local repo; repo=$(repo_path) || return 1

    local psk open txp args
    psk=$(current_psk)
    open=$(cfg_get open_wifi 0)
    txp=$(cfg_get txpower "")
    args=(--ssid "$(cfg_get ssid Blaufilter)" --user "$BF_USER" --pin "$(cfg_get debug_pin 1234)")
    [[ $open == 1 ]] && args+=(--open) || args+=(--psk "$psk")
    [[ -n $txp ]] && args+=(--txpower "$txp")
    args+=("$@")

    run_detached "Installationsscript läuft — das dauert einige Minuten" \
        bash "$repo/deploy/install.sh" "${args[@]}"
}

menu_role() {
    local role id
    role=$(whiptail --title "$TITLE — Rolle" --menu \
        "Rolle dieses Geräts.\n\nDer Host spannt das WLAN auf und steuert alle anderen.\nEs darf genau EINEN Host geben." 15 74 2 \
        "host"   "Host (Gerät 1, WLAN + Steuerung)" \
        "client" "Client (spielt nur ab)" 3>&1 1>&2 2>&3) || return 0

    local entries=()
    if [[ $role == host ]]; then
        entries=(1 "192.168.4.1")
    else
        for n in 2 3 4 5 6; do entries+=("$n" "192.168.4.1$n"); done
    fi
    id=$(whiptail --title "$TITLE — Geräte-ID" --menu \
        "Geräte-ID (bestimmt die feste IP).\nJede ID darf nur einmal vergeben sein." 16 74 6 \
        "${entries[@]}" 3>&1 1>&2 2>&3) || return 0

    yes_no "Gerät als '$role' mit ID $id einrichten?\n\nDas Installationsscript läuft erneut durch und räumt\nEinstellungen der bisherigen Rolle auf. Dauert 1–2 Minuten." || return 0
    reinstall --id "$id" --role "$role" || true
}

apply_txpower() {  # repo value ("" = back to the driver maximum)
    local repo=$1 txp=$2
    if [[ -n $txp ]]; then
        BF_TXPOWER="$txp" bash "$repo/deploy/steps/26-txpower.sh" >/dev/null 2>&1 \
            || msg "Die Sendeleistung konnte nicht gesetzt werden."
    else
        rm -f /etc/NetworkManager/dispatcher.d/50-blaufilter-txpower
        iw dev wlan0 set txpower auto >/dev/null 2>&1 || true
    fi
}

# Only the network step runs here — not the whole installer. Changing an SSID
# has no business reinstalling packages, systemd units and the Python package.
# (A role change does go through the installer: that one really does rearrange
# the whole device.)
menu_wifi() {
    local repo role id ssid open psk txp step
    repo=$(repo_path) || return 0
    role=$(cfg_get role host)
    id=$(cfg_get device_id 1)

    ssid=$(whiptail --title "$TITLE — WLAN" --inputbox "Netzwerkname (SSID):" 10 74 \
           "$(cfg_get ssid Blaufilter)" 3>&1 1>&2 2>&3) || return 0

    if yes_no "WLAN ohne Passwort betreiben?\n\nJa  = offen, Besucher verbinden sich mit einem Tipp.\nNein = WPA2 mit Passwort.\n\nDie Steuerports der Geräte sind in beiden Fällen\ndurch die Port-Sperre geschützt." 14; then
        open=1
        psk=""
    else
        open=0
        psk=$(current_psk)
        if [[ -z $psk ]] || yes_no "Passwort ändern?\n\nNein = bisheriges Passwort beibehalten." 10; then
            while true; do
                psk=$(whiptail --title "$TITLE — WLAN" --passwordbox \
                      "WLAN-Passwort (mindestens 8 Zeichen):" 10 74 3>&1 1>&2 2>&3) || return 0
                [[ ${#psk} -ge 8 ]] && break
                msg "Das Passwort muss mindestens 8 Zeichen haben."
            done
        fi
    fi

    txp=$(whiptail --title "$TITLE — Sendeleistung" --menu \
        "Sendeleistung des WLAN.\n\nStehen die Geräte dicht beieinander, stören sich\nvolle Sendeleistungen gegenseitig." 16 74 4 \
        ""   "unverändert (Maximum des Treibers)" \
        "15" "15 dBm — großer Raum" \
        "10" "10 dBm — ein Raum (empfohlen)" \
        "6"  "6 dBm — sehr dicht beieinander" 3>&1 1>&2 2>&3) || return 0

    yes_no "WLAN jetzt neu einrichten?\n\nSSID: $ssid\nVerschlüsselung: $([[ $open == 1 ]] && echo 'offen' || echo 'WPA2')\nSendeleistung: ${txp:-unverändert}\n\nDie WLAN-Verbindung bricht dabei kurz ab. Alle anderen\nGeräte müssen dieselben Einstellungen bekommen, sonst\nfinden sie den Host nicht mehr." 17 || return 0

    cfg_set ssid "$ssid"
    cfg_set open_wifi "$open"
    cfg_set txpower "$txp"

    [[ $role == host ]] && step=20-network-host.sh || step=20-network-client.sh
    export BF_REPO_DIR="$repo" BF_ID="$id" BF_SSID="$ssid" BF_PSK="$psk" BF_OPEN="$open"
    run_detached "WLAN wird neu eingerichtet" bash "$repo/deploy/steps/$step" || return 0
    apply_txpower "$repo" "$txp"
}

menu_pin() {
    local pin
    pin=$(whiptail --title "$TITLE — Debug-PIN" --inputbox \
        "Vierstellige PIN für die Debug-Seite.\nLeer lassen schaltet die Abfrage ab." 11 74 \
        "$(cfg_get debug_pin 1234)" 3>&1 1>&2 2>&3) || return 0
    if [[ -n $pin && ! $pin =~ ^[0-9]{4}$ ]]; then
        msg "Die PIN muss aus genau vier Ziffern bestehen (oder leer sein)."
        return 0
    fi
    cfg_set debug_pin "$pin"
    systemctl restart blaufilter-controller 2>/dev/null || true
    msg "PIN gespeichert.${pin:+\n\nNeue PIN: $pin}"
}

# VLC is a --user unit, the rest are system units
svc_action() {  # service action
    case $1 in
        vlc) user_systemctl "$2" blaufilter-vlc ;;
        *)   systemctl "$2" "blaufilter-$1" ;;
    esac
}

svc_label() {
    case $1 in
        vlc)        echo "Videowiedergabe (VLC)" ;;
        agent)      echo "Video-Agent" ;;
        controller) echo "Controller + Web-UI" ;;
        firewall)   echo "Port-Sperre" ;;
    esac
}

# What the user loses while a given service is stopped
stop_warning() {
    case $1 in
        vlc)        echo "Die Wiedergabe endet auf diesem Gerät." ;;
        agent)      echo "Dieses Gerät nimmt keine Videos mehr entgegen." ;;
        controller) echo "Web-UI und Synchronisation fallen aus — die Geräte\nspielen weiter, driften aber unkorrigiert auseinander." ;;
        firewall)   echo "Die Steuerports 4212/4213 stehen dann allen im WLAN offen." ;;
        alle)       echo "Wiedergabe, Steuerung und Port-Sperre werden beendet." ;;
    esac
}

menu_services() {
    local svc action verb services out
    svc=$(whiptail --title "$TITLE — Dienste" --menu "Welcher Dienst?" 17 74 5 \
        "vlc"        "$(svc_label vlc)" \
        "agent"      "$(svc_label agent)" \
        "controller" "$(svc_label controller) (nur Host)" \
        "firewall"   "$(svc_label firewall)" \
        "alle"       "alle oben genannten" 3>&1 1>&2 2>&3) || return 0

    action=$(whiptail --title "$TITLE — Dienste" --menu \
        "Aktion für: $([[ $svc == alle ]] && echo 'alle Dienste' || svc_label "$svc")" 14 74 3 \
        "restart" "Neu starten" \
        "stop"    "Stoppen" \
        "start"   "Starten" 3>&1 1>&2 2>&3) || return 0

    if [[ $action == stop ]]; then
        yes_no "Wirklich stoppen?\n\n$(stop_warning "$svc")\n\nNach einem Neustart des Geräts laufen die Dienste\nwieder von selbst." 14 || return 0
    fi

    [[ $svc == alle ]] && services=(vlc agent controller firewall) || services=("$svc")
    case $action in
        start) verb="gestartet" ;;
        stop)  verb="gestoppt" ;;
        *)     verb="neu gestartet" ;;
    esac

    out=""
    for s in "${services[@]}"; do
        if svc_action "$s" "$action" >/dev/null 2>&1; then
            out+="$(svc_label "$s"): $verb\n"
        else
            out+="$(svc_label "$s"): fehlgeschlagen / nicht vorhanden\n"
        fi
    done
    msg "$out"
}

menu_video() {
    local found=() path
    while IFS= read -r f; do
        found+=("$f" "$(du -h "$f" 2>/dev/null | cut -f1)")
    done < <(find /home /media /mnt -maxdepth 4 -type f \
             \( -iname '*.mp4' -o -iname '*.mkv' -o -iname '*.mov' \) 2>/dev/null | head -20)
    found+=("MANUELL" "Pfad selbst eingeben")

    path=$(whiptail --title "$TITLE — Video" --menu \
        "Video für dieses Gerät auswählen:" 20 76 10 "${found[@]}" 3>&1 1>&2 2>&3) || return 0
    if [[ $path == MANUELL ]]; then
        path=$(whiptail --title "$TITLE — Video" --inputbox "Pfad zur Videodatei:" 10 74 \
               "/home/$BF_USER/" 3>&1 1>&2 2>&3) || return 0
    fi
    [[ -f $path ]] || { msg "Datei nicht gefunden:\n$path"; return 0; }

    yes_no "Video dieses Geräts ersetzen?\n\n$path\n\nAuf den anderen Geräten ändert sich nichts — dafür\ndie Verteilung im Web-UI benutzen." || return 0
    install -d /opt/blaufilter/video
    cp "$path" "$VIDEO.uploading" && mv "$VIDEO.uploading" "$VIDEO"
    chown "$BF_USER:$BF_USER" "$VIDEO"
    user_systemctl restart blaufilter-vlc >/dev/null 2>&1 || true
    msg "Video ersetzt und VLC neu gestartet."
}

png_size() {  # path -> "1920x1080", empty if not a readable PNG
    python3 - "$1" <<'PY' 2>/dev/null
import struct, sys
with open(sys.argv[1], "rb") as fh:
    head = fh.read(24)
if head[:8] == b"\x89PNG\r\n\x1a\n":
    print("%dx%d" % struct.unpack(">II", head[16:24]))
PY
}

menu_splash() {
    local repo id entries=() path size
    repo=$(repo_path) || return 0
    id=$(cfg_get device_id "")

    # The repository's own images first, the one matching this device on top
    if [[ -n $id && -f $repo/deploy/Blaufilter_$id.png ]]; then
        entries+=("$repo/deploy/Blaufilter_$id.png" "aus dem Repository — für Gerät $id")
    fi
    for f in "$repo"/deploy/Blaufilter_*.png; do
        [[ -f $f ]] || continue
        [[ -n $id && $f == "$repo/deploy/Blaufilter_$id.png" ]] && continue
        entries+=("$f" "aus dem Repository")
    done
    while IFS= read -r f; do
        entries+=("$f" "$(du -h "$f" 2>/dev/null | cut -f1)")
    done < <(find /home /media /mnt -maxdepth 4 -type f -iname '*.png' 2>/dev/null | head -15)
    entries+=("MANUELL" "Pfad selbst eingeben")
    [[ -f $SPLASH_TARGET.orig ]] && entries+=("ORIGINAL" "Ursprüngliches Startbild wiederherstellen")

    path=$(whiptail --title "$TITLE — Startbild" --menu \
        "Bild, das beim Hochfahren angezeigt wird:" 20 78 10 \
        "${entries[@]}" 3>&1 1>&2 2>&3) || return 0

    local confirmed=0
    if [[ $path == ORIGINAL ]]; then
        yes_no "Ursprüngliches Startbild wiederherstellen?\n\nWirkt ab dem nächsten Neustart." 10 || return 0
        # Same step as for any other image — the backup is simply the source
        # now, and it stays: the step only creates .orig when none exists yet.
        path="$SPLASH_TARGET.orig"
        confirmed=1
    fi

    if [[ $path == MANUELL ]]; then
        path=$(whiptail --title "$TITLE — Startbild" --inputbox "Pfad zur PNG-Datei:" 10 74 \
               "/home/$BF_USER/" 3>&1 1>&2 2>&3) || return 0
    fi
    [[ -f $path ]] || { msg "Datei nicht gefunden:\n$path"; return 0; }

    if (( ! confirmed )); then
        size=$(png_size "$path")
        if [[ -z $size ]]; then
            yes_no "Das scheint keine PNG-Datei zu sein:\n$path\n\nTrotzdem verwenden?" 11 || return 0
        fi
        yes_no "Startbild ersetzen?\n\n$path\nAuflösung: ${size:-unbekannt}\n\nAm besten passt die native Auflösung des Displays;\nAbweichendes wird skaliert. Wirkt ab dem nächsten\nNeustart." 15 || return 0
    fi

    clear
    echo "== Startbild wird gesetzt =="
    echo
    if BF_SPLASH="$path" bash "$repo/deploy/steps/50-splash.sh"; then
        echo; read -rp "Fertig — wirkt ab dem nächsten Neustart. Enter drücken…" _
    else
        echo; read -rp "FEHLGESCHLAGEN — Ausgabe oben prüfen. Enter drücken…" _
    fi
}

# ------------------------------------------------------------- resolution
#
# The screen mode is pinned through the kernel's video= parameter rather than a
# desktop tool: it is read from /sys (no graphical session needed, so this also
# works over SSH) and applies to console, boot splash and desktop alike.

DRM_ROOT=${DRM_ROOT:-/sys/class/drm}   # overridable so this can be tested

drm_dir_for() {  # connector name -> <DRM_ROOT>/cardX-<connector>
    local d
    for d in "$DRM_ROOT"/card*-*; do
        [[ -d $d && ${d##*/} == *-"$1" ]] && { echo "$d"; return 0; }
    done
    return 1
}

connected_outputs() {
    local d name
    for d in "$DRM_ROOT"/card*-*; do
        [[ -r $d/status ]] || continue
        [[ $(cat "$d/status") == connected ]] || continue
        name=${d##*/}
        echo "${name#*-}"
    done
}

mode_label() {
    case $1 in
        3840x2160) echo "4K UHD" ;;
        2560x1440) echo "WQHD" ;;
        1920x1080) echo "Full HD" ;;
        1280x720)  echo "HD" ;;
        *)         echo "—" ;;
    esac
}

current_video_setting() {  # connector -> "1920x1080@60" or empty
    grep -oE "video=$1:[^[:space:]]+" "$CMDLINE" 2>/dev/null | head -1 | cut -d: -f2- || true
}

set_video_setting() {  # connector value ("" = remove, back to automatic)
    local conn=$1 value=$2 line
    line=$(tr '\n' ' ' < "$CMDLINE")
    line=$(sed -E "s/[[:space:]]*video=${conn}:[^[:space:]]+//g" <<<"$line")
    [[ -n $value ]] && line="$line video=${conn}:${value}"
    line=$(tr -s ' ' <<<"$line" | sed 's/^ *//; s/ *$//')

    # A broken cmdline.txt means the Pi does not boot at all — refuse anything
    # that no longer looks like a kernel command line.
    if [[ -z $line || $line != *root=* ]]; then
        msg "Abgebrochen: die erzeugte Boot-Zeile sieht nicht stimmig aus.\nEs wurde nichts verändert."
        return 1
    fi
    cp "$CMDLINE" "$CMDLINE.bak"
    printf '%s\n' "$line" > "$CMDLINE"
}

# 4Kp60 is off by default on the Pi 4 and silently falls back without this
# switch — and it only works on the HDMI port next to the power connector.
offer_4kp60() {  # mode rate
    local config_txt="$BOOT_DIR/config.txt"
    [[ $1 == 3840x2160 && $2 == 60 ]] || return 0
    [[ -f $config_txt ]] || return 0
    grep -qE '^[[:space:]]*hdmi_enable_4kp60=1' "$config_txt" && return 0
    yes_no "4K mit 60 Hz braucht auf dem Pi 4 zusätzlich den Schalter\nhdmi_enable_4kp60=1 in der config.txt — und das Kabel\nmuss im HDMI-Anschluss neben dem Stromanschluss stecken.\n\nSchalter jetzt eintragen?" 14 || return 0
    cp "$config_txt" "$config_txt.bak"
    printf '\nhdmi_enable_4kp60=1\n' >> "$config_txt"
}

# Lists the modes the display itself reports — the escape hatch behind "weitere".
pick_edid_mode() {  # drm-dir -> prints "WxH" or nothing
    local dir=$1 entries=() first=1 m
    while read -r m; do
        [[ -n $m ]] || continue
        if (( first )); then
            entries+=("$m" "$(mode_label "$m") — vom Bildschirm bevorzugt"); first=0
        else
            entries+=("$m" "$(mode_label "$m")")
        fi
    done < <(awk '!seen[$0]++' "$dir/modes" 2>/dev/null)
    (( ${#entries[@]} )) || return 1
    whiptail --title "$TITLE — Auflösung" --menu \
        "Vom Bildschirm gemeldete Auflösungen:" 20 74 9 "${entries[@]}" 3>&1 1>&2 2>&3
}

menu_resolution() {
    local outs=() out dir m mode value current splash_size hint=""
    local rate=""

    if [[ ! -f $CMDLINE ]]; then
        msg "Boot-Konfiguration nicht gefunden:\n$CMDLINE"
        return 0
    fi

    while read -r m; do
        [[ -n $m ]] && outs+=("$m" "angeschlossen")
    done < <(connected_outputs)

    if (( ${#outs[@]} == 0 )); then
        msg "Kein angeschlossener Bildschirm erkannt.\n\nSteckt das HDMI-Kabel? Die Liste kommt direkt vom\nGrafiktreiber des Systems."
        return 0
    fi
    if (( ${#outs[@]} > 2 )); then
        out=$(whiptail --title "$TITLE — Auflösung" --menu "Welcher Anschluss?" 14 74 4 \
              "${outs[@]}" 3>&1 1>&2 2>&3) || return 0
    else
        out=${outs[0]}
    fi
    dir=$(drm_dir_for "$out") || { msg "Anschluss $out nicht gefunden."; return 0; }
    current=$(current_video_setting "$out")

    # The two settings this installation actually uses, plus escape hatches
    value=$(whiptail --title "$TITLE — Auflösung" --menu \
        "Auflösung für $out\n\nAktuell fest eingestellt: ${current:-automatisch}" 17 74 4 \
        "1920x1080@60" "Full HD — Einrichten und Entwickeln" \
        "3840x2160@30" "4K UHD — Installation" \
        ""             "automatisch — was der Bildschirm meldet" \
        "MEHR"         "weitere Auflösungen des Bildschirms…" 3>&1 1>&2 2>&3) || return 0

    if [[ $value == MEHR ]]; then
        mode=$(pick_edid_mode "$dir") || {
            msg "Der Bildschirm meldet keine Auflösungen.\n\nDas passiert, wenn kein Bildschirm angeschlossen ist\noder er kein EDID liefert."
            return 0
        }
        [[ -n $mode ]] || return 0
        rate=$(whiptail --title "$TITLE — Bildwiederholrate" --menu \
            "Bildwiederholrate für $mode:" 16 74 4 \
            ""   "automatisch" \
            "60" "60 Hz" \
            "50" "50 Hz" \
            "30" "30 Hz" 3>&1 1>&2 2>&3) || return 0
        value="$mode${rate:+@$rate}"
    else
        mode=${value%@*}
        rate=${value#*@}
        [[ $rate == "$value" ]] && rate=""
    fi

    if [[ -n $value && -f $SPLASH_TARGET ]]; then
        splash_size=$(png_size "$SPLASH_TARGET")
        if [[ -n $splash_size && $splash_size != "$mode" ]]; then
            hint="\n\nHinweis: das Startbild hat $splash_size und wird skaliert."
        fi
    fi

    yes_no "Auflösung fest einstellen?\n\nAnschluss: $out\nAuflösung: ${value:-automatisch}\n\nWirkt nach einem Neustart — auf Konsole, Startbild und\nWiedergabe. Die bisherige Boot-Zeile wird als\ncmdline.txt.bak gesichert.$hint" 18 || return 0

    set_video_setting "$out" "$value" || return 0
    if [[ -n $value ]]; then
        offer_4kp60 "$mode" "$rate"
    fi
    if yes_no "Gespeichert.\n\nJetzt neu starten, damit die Auflösung wirkt?" 10; then
        systemctl reboot
    fi
}

menu_logs() {
    local unit
    unit=$(whiptail --title "$TITLE — Protokolle" --menu "Welches Protokoll?" 16 74 4 \
        "blaufilter-controller" "Controller + Synchronisation" \
        "blaufilter-agent"      "Video-Agent" \
        "NetworkManager"        "WLAN" \
        "VLC"                   "Videowiedergabe" 3>&1 1>&2 2>&3) || return 0
    local text
    if [[ $unit == VLC ]]; then
        text=$(user_systemctl status blaufilter-vlc --no-pager -n 60 2>&1 || true)
    else
        text=$(journalctl -u "$unit" -n 60 --no-pager 2>&1 || true)
    fi
    whiptail --title "$unit" --scrolltext --msgbox "${text:-keine Einträge}" 24 78
}

menu_power() {
    local choice
    choice=$(whiptail --title "$TITLE" --menu "Gerät:" 13 74 2 \
        "reboot"   "Neu starten" \
        "poweroff" "Herunterfahren" 3>&1 1>&2 2>&3) || return 0
    local label="herunterfahren"
    [[ $choice == reboot ]] && label="neu starten"
    yes_no "Gerät jetzt $label?" 8 || return 0
    systemctl "$choice"
}

# --------------------------------------------------------------------- main

while true; do
    header="Gerät $(cfg_get device_id '?') · $(cfg_get role '?') · $(ip -4 -o addr show wlan0 2>/dev/null | awk '{print $4}' | head -1)"
    choice=$(whiptail --title "$TITLE — Wartung" --menu "$header" 22 74 11 \
        "status"     "Status anzeigen" \
        "dienste"    "Dienste starten / stoppen / neu starten" \
        "rolle"      "Rolle und Geräte-ID ändern" \
        "wlan"       "WLAN und Sendeleistung" \
        "aufloesung" "Bildschirmauflösung" \
        "pin"        "Debug-PIN ändern" \
        "video"      "Video dieses Geräts austauschen" \
        "splash"     "Startbild ändern" \
        "logs"       "Protokolle ansehen" \
        "power"      "Neu starten / Herunterfahren" \
        "ende"       "Beenden" 3>&1 1>&2 2>&3) || break

    case $choice in
        status)     status_report ;;
        dienste)    menu_services ;;
        rolle)      menu_role ;;
        wlan)       menu_wifi ;;
        aufloesung) menu_resolution ;;
        pin)        menu_pin ;;
        video)      menu_video ;;
        splash)     menu_splash ;;
        logs)       menu_logs ;;
        power)      menu_power ;;
        ende)       break ;;
    esac
done
clear
