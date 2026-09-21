#!/usr/bin/env bash
# Guided installation. Collects the settings in a dialog and then hands them to
# install.sh — the installer stays the single source of truth, this is only the
# front end. Everything here can still be done with install.sh directly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TITLE="Blaufilter — Einrichtung"

if [[ $EUID -ne 0 ]]; then
    echo "Bitte mit sudo starten: sudo ./setup.sh" >&2
    exit 1
fi
if ! command -v whiptail >/dev/null; then
    echo "whiptail fehlt. Alternative: ./install.sh --id N ... (siehe README)" >&2
    exit 1
fi

cancelled() { whiptail --title "$TITLE" --msgbox "Abgebrochen — es wurde nichts verändert." 8 70; exit 0; }

whiptail --title "$TITLE" --msgbox \
"Dieser Assistent richtet einen Raspberry Pi als Blaufilter-Gerät ein.

Es gibt genau EIN Host-Gerät: Es spannt das WLAN auf, steuert alle
anderen und stellt die Bedienoberfläche bereit. Alle weiteren Geräte
sind Clients.

Jedes Gerät wird einzeln eingerichtet — alle mit denselben
WLAN-Einstellungen." 16 74

# ------------------------------------------------------------- role & id

ROLE=$(whiptail --title "$TITLE — Rolle" --menu \
"Rolle dieses Geräts:" 13 74 2 \
"host"   "Host — WLAN, Steuerung, Web-Oberfläche" \
"client" "Client — spielt nur ab" 3>&1 1>&2 2>&3) || cancelled

if [[ $ROLE == host ]]; then
    ID=1
    whiptail --title "$TITLE" --msgbox "Der Host bekommt immer die Geräte-ID 1 und die Adresse 192.168.4.1." 9 74
else
    entries=()
    for n in 2 3 4 5 6; do entries+=("$n" "192.168.4.1$n"); done
    ID=$(whiptail --title "$TITLE — Geräte-ID" --menu \
"Geräte-ID dieses Clients (bestimmt seine feste Adresse).

Jede ID darf im Verbund nur einmal vergeben werden." 16 74 5 \
        "${entries[@]}" 3>&1 1>&2 2>&3) || cancelled
fi

# ----------------------------------------------------------------- wifi

SSID=$(whiptail --title "$TITLE — WLAN" --inputbox \
"Name des WLAN (SSID), das der Host aufspannt:" 10 74 "Blaufilter" 3>&1 1>&2 2>&3) || cancelled

ARGS=(--id "$ID" --role "$ROLE" --ssid "$SSID")

if whiptail --title "$TITLE — WLAN" --yesno \
"WLAN ohne Passwort betreiben?

Ja   = offen. Besucher verbinden sich mit einem Tipp und landen
       direkt auf der Steuerseite.
Nein = WPA2 mit Passwort.

Die Steuerports der Geräte sind in beiden Fällen abgesichert." 16 74; then
    ARGS+=(--open)
    OPEN_LABEL="offen (kein Passwort)"
else
    while true; do
        PSK=$(whiptail --title "$TITLE — WLAN" --passwordbox \
"WLAN-Passwort (mindestens 8 Zeichen).

Auf allen Geräten dasselbe eintragen." 12 74 3>&1 1>&2 2>&3) || cancelled
        [[ ${#PSK} -ge 8 ]] && break
        whiptail --title "$TITLE" --msgbox "Zu kurz — mindestens 8 Zeichen." 8 70
    done
    ARGS+=(--psk "$PSK")
    OPEN_LABEL="WPA2 mit Passwort"
fi

TXPOWER=$(whiptail --title "$TITLE — Sendeleistung" --menu \
"WLAN-Sendeleistung.

Stehen die Geräte dicht beieinander, stören sich volle
Sendeleistungen gegenseitig mehr, als sie nutzen." 17 74 4 \
    ""   "unverändert (Maximum des Treibers)" \
    "15" "15 dBm — großer Raum" \
    "10" "10 dBm — ein Raum (empfohlen)" \
    "6"  "6 dBm — sehr dicht beieinander" 3>&1 1>&2 2>&3) || cancelled
[[ -n $TXPOWER ]] && ARGS+=(--txpower "$TXPOWER")

# ---------------------------------------------------------------- video

VIDEO=""
found=()
while IFS= read -r f; do
    found+=("$f" "$(du -h "$f" 2>/dev/null | cut -f1)")
done < <(find /home /media /mnt -maxdepth 4 -type f \
         \( -iname '*.mp4' -o -iname '*.mkv' -o -iname '*.mov' \) 2>/dev/null | head -20)
found+=("MANUELL" "Pfad selbst eingeben")
found+=("SPAETER" "Video später selbst kopieren")

VIDEO=$(whiptail --title "$TITLE — Video" --menu \
"Video, das dieses Gerät abspielt.

Empfohlen: H.265/HEVC, Keyframe-Abstand ≤ 2 s (siehe README)." 20 76 9 \
    "${found[@]}" 3>&1 1>&2 2>&3) || cancelled

case $VIDEO in
    SPAETER) VIDEO="" ;;
    MANUELL)
        VIDEO=$(whiptail --title "$TITLE — Video" --inputbox "Pfad zur Videodatei:" 10 74 \
                "/home/${SUDO_USER:-pi}/" 3>&1 1>&2 2>&3) || cancelled
        ;;
esac
if [[ -n $VIDEO && ! -f $VIDEO ]]; then
    whiptail --title "$TITLE" --msgbox "Datei nicht gefunden:\n$VIDEO\n\nDas Video kann später nach\n/opt/blaufilter/video/main.mp4 kopiert werden." 12 74
    VIDEO=""
fi
[[ -n $VIDEO ]] && ARGS+=(--video "$VIDEO")

# ------------------------------------------------------- host-only extras

PIN="1234"
if [[ $ROLE == host ]]; then
    PIN=$(whiptail --title "$TITLE — Debug-PIN" --inputbox \
"Vierstellige PIN für die Debug-Seite der Weboberfläche.

Dahinter liegen Gerätestatus, Video-Verteilung und
Wartungsfunktionen. Die normale Steuerung bleibt frei zugänglich." 14 74 \
        "1234" 3>&1 1>&2 2>&3) || cancelled
    if [[ -n $PIN && ! $PIN =~ ^[0-9]{4}$ ]]; then
        whiptail --title "$TITLE" --msgbox "Keine vierstellige Zahl — es bleibt bei 1234." 8 70
        PIN="1234"
    fi
fi
ARGS+=(--pin "$PIN")

SPLASH=""
if whiptail --title "$TITLE — Bootscreen" --yesno \
"Eigenes Startbild einrichten?

Empfohlene Auflösung: die native Auflösung des Displays
(bei 4K-Bildschirmen 3840×2160), Format PNG." 12 74; then
    SPLASH=$(whiptail --title "$TITLE — Bootscreen" --inputbox "Pfad zur PNG-Datei:" 10 74 \
             "/home/${SUDO_USER:-pi}/" 3>&1 1>&2 2>&3) || SPLASH=""
    if [[ -n $SPLASH && ! -f $SPLASH ]]; then
        whiptail --title "$TITLE" --msgbox "Datei nicht gefunden — Startbild bleibt unverändert." 8 70
        SPLASH=""
    fi
    [[ -n $SPLASH ]] && ARGS+=(--splash "$SPLASH")
fi

# -------------------------------------------------------------- confirm

SUMMARY="Rolle:           $ROLE (Geräte-ID $ID)
Adresse:         $([[ $ROLE == host ]] && echo '192.168.4.1' || echo "192.168.4.1$ID")
WLAN:            $SSID
Verschlüsselung: $OPEN_LABEL
Sendeleistung:   ${TXPOWER:-unverändert}${TXPOWER:+ dBm}
Video:           ${VIDEO:-später kopieren}
Bootscreen:      ${SPLASH:-unverändert}"
[[ $ROLE == host ]] && SUMMARY+="
Debug-PIN:       ${PIN:-keine Abfrage}"

whiptail --title "$TITLE — Zusammenfassung" --yesno \
"$SUMMARY

Jetzt installieren? Dauert einige Minuten." 20 74 || cancelled

clear
echo "== Blaufilter wird eingerichtet =="
echo
bash "$SCRIPT_DIR/install.sh" "${ARGS[@]}"

echo
echo "Wartung und spätere Änderungen: sudo blaufilter-setup"
read -rp "Jetzt neu starten? [j/N] " answer
[[ ${answer,,} == j ]] && reboot
