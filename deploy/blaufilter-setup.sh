#!/usr/bin/env bash
# Blaufilter maintenance menu. Installed as /usr/local/sbin/blaufilter-setup.
#
# Everything that changes role or WiFi re-runs the installer instead of poking
# at NetworkManager here: the installer already knows how to clean up leftovers
# (a cloned SD card carrying the host's AP profile, for instance), and one
# code path means the two can never drift apart.
set -euo pipefail

CONFIG=/etc/blaufilter/config
VIDEO=/opt/blaufilter/video/main.mp4
TITLE="Blaufilter"

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

reinstall() {  # extra install.sh arguments
    local repo; repo=$(cfg_get repo_dir "")
    if [[ -z $repo || ! -x $repo/deploy/install.sh ]]; then
        if ! repo=$(whiptail --title "$TITLE" --inputbox \
              "Pfad zum blaufilter_vlcsync-Repository:" 10 74 "/home/$BF_USER/blaufilter_vlcsync" \
              3>&1 1>&2 2>&3); then return 1; fi
        [[ -x $repo/deploy/install.sh ]] || { msg "Kein install.sh unter:\n$repo"; return 1; }
    fi

    local psk open txp args
    psk=$(current_psk)
    open=$(cfg_get open_wifi 0)
    txp=$(cfg_get txpower "")
    args=(--ssid "$(cfg_get ssid Blaufilter)" --user "$BF_USER" --pin "$(cfg_get debug_pin 1234)")
    [[ $open == 1 ]] && args+=(--open) || args+=(--psk "$psk")
    [[ -n $txp ]] && args+=(--txpower "$txp")
    args+=("$@")

    clear
    echo "== Installer läuft: ${args[*]//$psk/******} =="
    echo
    if bash "$repo/deploy/install.sh" "${args[@]}"; then
        echo; read -rp "Fertig. Enter drücken…" _
        return 0
    fi
    echo; read -rp "FEHLGESCHLAGEN — Ausgabe oben prüfen. Enter drücken…" _
    return 1
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

menu_wifi() {
    local ssid open psk txp
    ssid=$(whiptail --title "$TITLE — WLAN" --inputbox "Netzwerkname (SSID):" 10 74 \
           "$(cfg_get ssid Blaufilter)" 3>&1 1>&2 2>&3) || return 0

    if yes_no "WLAN ohne Passwort betreiben?\n\nJa  = offen, Besucher verbinden sich mit einem Tipp.\nNein = WPA2 mit Passwort.\n\nDie Steuerports der Geräte sind in beiden Fällen\ndurch die Port-Sperre geschützt." 14; then
        open=1
    else
        open=0
        psk=$(whiptail --title "$TITLE — WLAN" --passwordbox \
              "WLAN-Passwort (mindestens 8 Zeichen):" 10 74 3>&1 1>&2 2>&3) || return 0
        if [[ ${#psk} -lt 8 ]]; then msg "Das Passwort muss mindestens 8 Zeichen haben."; return 0; fi
    fi

    txp=$(whiptail --title "$TITLE — Sendeleistung" --menu \
        "Sendeleistung des WLAN.\n\nStehen die Geräte dicht beieinander, stören sich\nvolle Sendeleistungen gegenseitig." 16 74 4 \
        ""   "unverändert (Maximum des Treibers)" \
        "15" "15 dBm — großer Raum" \
        "10" "10 dBm — ein Raum (empfohlen)" \
        "6"  "6 dBm — sehr dicht beieinander" 3>&1 1>&2 2>&3) || return 0

    yes_no "WLAN neu einrichten?\n\nSSID: $ssid\nVerschlüsselung: $([[ $open == 1 ]] && echo 'offen' || echo 'WPA2')\nSendeleistung: ${txp:-unverändert}\n\nACHTUNG: Alle anderen Geräte müssen mit denselben\nEinstellungen neu eingerichtet werden, sonst finden\nsie den Host nicht mehr." 16 || return 0

    cfg_set ssid "$ssid"
    cfg_set open_wifi "$open"
    cfg_set txpower "$txp"
    local args=(--id "$(cfg_get device_id 1)" --role "$(cfg_get role host)")
    [[ $open == 1 ]] || args+=(--psk "$psk")
    reinstall "${args[@]}" || true
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

menu_services() {
    local choice
    choice=$(whiptail --title "$TITLE — Dienste" --menu "Neu starten:" 17 74 6 \
        "vlc"        "Videowiedergabe (VLC)" \
        "agent"      "Video-Agent" \
        "controller" "Controller + Web-UI (nur Host)" \
        "firewall"   "Port-Sperre" \
        "alle"       "alle oben genannten" 3>&1 1>&2 2>&3) || return 0

    local out=""
    restart_one() {
        case $1 in
            vlc) user_systemctl restart blaufilter-vlc 2>&1 ;;
            *)   systemctl restart "blaufilter-$1" 2>&1 ;;
        esac
    }
    if [[ $choice == alle ]]; then
        for s in vlc agent controller firewall; do
            out+="$s: $(restart_one "$s" >/dev/null 2>&1 && echo ok || echo 'fehlgeschlagen/nicht vorhanden')\n"
        done
    else
        out="$choice: $(restart_one "$choice" >/dev/null 2>&1 && echo ok || echo 'fehlgeschlagen/nicht vorhanden')\n"
    fi
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
    choice=$(whiptail --title "$TITLE — Wartung" --menu "$header" 20 74 9 \
        "status"   "Status anzeigen" \
        "dienste"  "Dienste neu starten" \
        "rolle"    "Rolle und Geräte-ID ändern" \
        "wlan"     "WLAN und Sendeleistung" \
        "pin"      "Debug-PIN ändern" \
        "video"    "Video dieses Geräts austauschen" \
        "logs"     "Protokolle ansehen" \
        "power"    "Neu starten / Herunterfahren" \
        "ende"     "Beenden" 3>&1 1>&2 2>&3) || break

    case $choice in
        status)  status_report ;;
        dienste) menu_services ;;
        rolle)   menu_role ;;
        wlan)    menu_wifi ;;
        pin)     menu_pin ;;
        video)   menu_video ;;
        logs)    menu_logs ;;
        power)   menu_power ;;
        ende)    break ;;
    esac
done
clear
