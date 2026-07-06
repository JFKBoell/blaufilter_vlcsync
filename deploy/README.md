# Blaufilter — Installation & Betrieb

Synchronisiertes 4K-Videoplayback auf 1–6 Raspberry Pi 4. Jeder Pi spielt das
gleiche, lokal gespeicherte Video (H.265/HEVC) im Vollbild in Endlosschleife.
Der Pi mit **ID 1 ist der Host**: Er spannt ein WLAN auf, überwacht und
korrigiert den Drift aller Geräte und stellt die Web-Bedienoberfläche bereit.

## Überblick

```
                 WLAN "Blaufilter" (Host = Access Point)
   ┌────────────────┬─────────────────┬─────────────────┐
   │ Pi ID 1 (Host) │ Pi ID 2 (Client)│ Pi ID 3 (Client)│  … bis ID 6
   │ 192.168.4.1    │ 192.168.4.12    │ 192.168.4.13    │
   │ VLC :4212      │ VLC :4212       │ VLC :4212       │
   │ Controller     │                 │                 │
   │ Web-UI :8080   │                 │                 │
   └────────────────┴─────────────────┴─────────────────┘
          ▲ Handy/Laptop im WLAN → http://192.168.4.1:8080
```

- **Geräteanzahl variabel:** Der Controller probt zyklisch alle 6 möglichen
  Adressen. Geräte, die ausgeschaltet sind, fehlen einfach; neu gestartete
  erscheinen innerhalb weniger Sekunden und werden auf Play-Zustand,
  Geschwindigkeit und Position gebracht.
- **Feste IDs:** Die ID steckt in `/etc/blaufilter/config` und im Hostnamen
  (`blaufilter-3`) — sie überlebt Reboots. IP-Schema: ID 1 → `.1`,
  ID n → `.1(0+n)` (ID 2 → `.12` … ID 6 → `.16`).
- **Warum keine ESP32-Fernbedienung:** Ein Web-UI auf dem Host ist deutlich
  unkomplizierter — keine zusätzliche Hardware, keine Firmware, kein
  UART-Protokoll. Jedes Handy im WLAN ist die Fernbedienung.

## Voraussetzungen

- Raspberry Pi 4 (2 GB reichen), **Raspberry Pi OS 64-bit Bookworm mit Desktop**
- Video als **H.265/HEVC** (der Pi 4 dekodiert 4K nur als HEVC in Hardware!)
- Für die Erstinstallation: Internetzugang (Ethernet oder anderes WLAN)

### Video vorbereiten

- Codec **H.265/HEVC**, Container mp4/mkv, max. 4K@30 empfohlen (4K@60 geht,
  lässt aber weniger Reserve).
- **Keyframe-Abstand (GOP) ≤ 2 s** — die Genauigkeit der Sync-Korrektur hängt
  daran, wie nah VLC an die Zielposition springen kann. Beispiel-Encoding:

  ```
  ffmpeg -i quelle.mov -c:v libx265 -preset slow -crf 22 \
         -x265-params keyint=48:min-keyint=48 -c:a aac main.mp4
  ```
  (48 = 2 s bei 24 fps; bei 30 fps entsprechend 60.)
- Alle Geräte bekommen **die gleiche Datei** nach `/opt/blaufilter/video/main.mp4`.

## Installation

Auf jedem Pi (frisches Raspberry Pi OS, per SSH oder Terminal):

```bash
git clone https://github.com/JFKBoell/blaufilter_vlcsync.git
cd blaufilter_vlcsync/deploy

# Host (baut das WLAN auf):
sudo ./install.sh --id 1 --psk MeinWlanPasswort --video /pfad/zum/video.mp4

# Clients:
sudo ./install.sh --id 2 --psk MeinWlanPasswort --video /pfad/zum/video.mp4
sudo ./install.sh --id 3 --psk MeinWlanPasswort --video /pfad/zum/video.mp4

sudo reboot
```

Optionen: `--ssid` (Standard `Blaufilter`), `--psk` (min. 8 Zeichen, Standard
`blaufilter` — **ändern!**), `--role host|client` (Standard: ID 1 = host),
`--user` (Standard: der aufrufende Benutzer), `--wifi-country` (Standard `DE`).
Ohne `--video` das Video später manuell nach `/opt/blaufilter/video/main.mp4`
kopieren.

Das Script richtet ein:

| Baustein | Host | Client |
|---|---|---|
| `/etc/blaufilter/config` (ID, Rolle) | ✓ | ✓ |
| VLC + Python-venv unter `/opt/blaufilter` | ✓ | ✓ |
| WLAN-AP `192.168.4.1/24` (NetworkManager, DHCP inklusive) | ✓ | — |
| WLAN-Client mit statischer IP | — | ✓ |
| VLC-Autostart (User-Unit `blaufilter-vlc`, Vollbild, Loop, RC auf :4212) | ✓ | ✓ |
| Controller + Web-UI (System-Unit `blaufilter-controller`) | ✓ | — |
| Desktop-Autologin, Bildschirm-Blanking aus | ✓ | ✓ |

## Bedienung

Mit dem WLAN `Blaufilter` verbinden und **http://192.168.4.1:8080** öffnen:

- **Play/Pause** — wirkt auf alle Geräte gleichzeitig.
- **Geschwindigkeit** 0,5×–2,0× in 0,05er-Schritten.
- **Gerätetabelle** — zeigt pro Gerät Position und aktuellen Drift (grün
  < 250 ms, orange < 500 ms, rot darüber). Das Master-Gerät ist markiert.
- **Jetzt neu synchronisieren** — erzwingt sofortigen Seek aller Geräte auf
  die Master-Position.

## Wie die Synchronisation funktioniert

- Der Controller pollt alle VLCs ~10× pro Sekunde über deren RC-Interface.
  Da VLC die Position nur in ganzen Sekunden meldet, wird der Sekundenwechsel
  abgepasst (Boundary-Sampling) und dazwischen mit der Abspielrate
  extrapoliert → Messgenauigkeit ~±150 ms.
- Weicht ein Gerät > 0,5 s vom Master ab (3 Zyklen in Folge, danach 5 s
  Abkühlphase), wird es per Seek korrigiert.
- Am Loop-Übergang (±3 s um Anfang/Ende) sind Korrekturen unterdrückt, damit
  der versetzte Umbruch der Geräte keinen Seek-Sturm auslöst.
- Optional (`--rate-nudge` in der Unit ergänzen): kleine Abweichungen
  (0,15–0,5 s) werden unsichtbar über ±3 % Abspielrate ausgeglichen statt
  per Seek.

Schwellen sind in `/etc/blaufilter/config` übersteuerbar
(`drift_threshold`, `hysteresis_cycles`, `cooldown_s`, `rate_nudge`, `web_port`).

## Fehlersuche

- **Läuft VLC?** `systemctl --user status blaufilter-vlc` (als Desktop-User).
- **Läuft der Controller?** (Host) `sudo systemctl status blaufilter-controller`
  bzw. `journalctl -u blaufilter-controller -f` — dort stehen Geräte-Joins
  und jede Driftkorrektur.
- **VLC startet nach Login nicht** (User-Unit bleibt `inactive`): Manche
  Sessions aktivieren `graphical-session.target` nicht zuverlässig. Dafür liegt
  ein Fallback in `~/.config/autostart/blaufilter-vlc.desktop`, der die Unit
  beim Desktop-Start anstößt. Prüfen mit `systemctl --user start blaufilter-vlc`.
- **Client taucht nicht auf:** WLAN prüfen (`nmcli device`), dann ob VLC lauscht:
  `nc -z 192.168.4.13 4212 && echo ok`.
- **RC-Interface von Hand testen:** `nc 192.168.4.12 4212`, dann z. B.
  `get_time`, `status`, `seek 120` eintippen.
- **2,4-GHz-WLAN überlastet:** In `deploy/steps/20-network-host.sh`
  `802-11-wireless.band bg` auf `a` (5 GHz) ändern und neu installieren —
  je nach Aufstellungsort/Reichweite abwägen.

## Hardware-Checkliste (vor der Ausstellung)

1. **Einzelgerät:** 4K-HEVC läuft flüssig im Vollbild (CPU deutlich unter
   100 % eines Kerns), Autostart nach Reboot, kein Bildschirm-Blanking über
   ≥ 30 min, Loop-Übergang ohne Hänger.
2. **Host:** WLAN sichtbar, Handy bekommt IP, Web-UI lädt und zeigt den Host
   in der Tabelle.
3. **Verbund (2–3 Geräte):** Alle Geräte in der Tabelle; Drift bleibt < 0,5 s
   über ≥ 1 h inklusive mindestens eines Loop-Übergangs (zum schnellen Testen
   ein kurzes Video verwenden); Geschwindigkeit 0,5× und 2,0× je 10 min stabil;
   einem Client den Strom ziehen → nach dem Boot innerhalb ~15 s wieder
   synchron.
4. **Generalprobe:** kompletter 3-h-Durchlauf über Nacht; im Journal des
   Controllers prüfen, wie oft korrigiert wurde (nach dem Einschwingen sollten
   Korrekturen selten sein).
