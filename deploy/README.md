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
   │ Web-UI :80     │                 │                 │
   └────────────────┴─────────────────┴─────────────────┘
          ▲ Handy/Laptop im WLAN → http://blaufilter.local
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
`--user` (Standard: der aufrufende Benutzer), `--wifi-country` (Standard `DE`),
`--splash bild.png` (eigener Bootscreen, siehe unten), `--pin` (PIN der
Debug-Seite, Standard `1234`), `--open` (WLAN ohne Passwort, siehe unten),
`--txpower 10` (Sendeleistung in dBm drosseln, siehe unten).

> **`--psk` weglassen ergibt kein offenes WLAN** — dann greift das
> Standardpasswort `blaufilter`. Für ein offenes Netz `--open` verwenden,
> und zwar **auf allen Geräten**: Host und Clients müssen zur selben
> Verschlüsselung passen, sonst finden die Clients den AP nicht.

### Offenes WLAN (`--open`)

Besucher verbinden sich mit einem Tipp, ohne Passwort — zusammen mit dem
Captive Portal (siehe unten) ist die Steuerung damit in zwei Schritten
erreichbar. Preis: **Jeder in Funkreichweite kann Play/Pause, Tempo und
Zufallssprung bedienen.** Die Debug-Seite bleibt durch die PIN geschützt,
die Steuerports der Geräte sind durch die Port-Sperre abgesichert (siehe
unten). Wer das nicht will, lässt das WLAN passwortgeschützt (Passwort z. B.
auf einem Schild neben der Installation).

### Port-Sperre (immer aktiv)

Zwei Dienste auf jedem Gerät kennen prinzipbedingt keine eigene
Zugangskontrolle: **VLCs RC-Interface (Port 4212)** — das Protokoll hat kein
Passwort — und der **Video-Agent (Port 4213)**, der Uploads und VLC-Neustarts
entgegennimmt. Ohne Schutz könnte jeder im WLAN die Wiedergabe stoppen oder
das Video austauschen, an der PIN der Debug-Seite vorbei: die schützt nur den
Controller auf Port 80, nicht diese beiden Dienste.

Die Installation richtet deshalb eine nftables-Regel ein
(`blaufilter-firewall.service`), die beide Ports **nur für den Host
192.168.4.1 und localhost** freigibt und alles andere verwirft. Sie betrifft
ausschließlich diese zwei Ports — SSH und alles Übrige bleiben unberührt.

```bash
sudo nft list table inet blaufilter        # aktive Regeln ansehen
sudo systemctl status blaufilter-firewall
```

### Sendeleistung drosseln (`--txpower`)

Hängen die Pis dicht beieinander im selben Raum, bringt volle Sendeleistung
keine Reichweite, sondern vor allem gegenseitige Störung — die Empfänger
werden von den starken Nachbarsignalen unempfindlicher. `--txpower 10`
(dBm, gültig 1–20) begrenzt die Leistung; ~10 dBm ist für einen Raum ein
guter Startwert.

Die Einstellung wird über ein NetworkManager-Dispatcher-Skript nach jedem
Verbindungsaufbau neu gesetzt, da der Treiber sie sonst zurücksetzt.
Kontrolle: `iw dev wlan0 info | grep txpower`.

**Am Host mit Bedacht wählen:** Dessen Sendeleistung bestimmt auch, aus
welcher Entfernung Besucher den AP noch sehen. Die Clients dürfen ruhig
niedriger liegen als der Host.
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
| Video-Agent (System-Unit `blaufilter-agent`, Port 4213) | ✓ | ✓ |
| Port-Sperre 4212/4213 (System-Unit `blaufilter-firewall`) | ✓ | ✓ |
| Controller + Web-UI (System-Unit `blaufilter-controller`, Waitress :80) | ✓ | — |
| Namensauflösung `blaufilter.local` (mDNS + DHCP-DNS) | ✓ | — |
| Desktop-Autologin, Bildschirm-Blanking aus | ✓ | ✓ |
| Eigener Bootscreen (nur mit `--splash`) | ✓ | ✓ |

### Eigener Bootscreen

Mit `--splash bild.png` ersetzt die Installation das Boot-Splash-Bild
(Plymouth-Theme „pix"). **Empfohlene Auflösung: die native Auflösung des
Displays** — bei 4K-Bildschirmen 3840×2160; 1920×1080 funktioniert ebenfalls
und wird skaliert. Format: PNG. Das Original wird als
`/usr/share/plymouth/themes/pix/splash.png.orig` gesichert (zum
Wiederherstellen zurückkopieren und `sudo update-initramfs -u` ausführen).

### Namensauflösung `blaufilter.local`

Der Host beantwortet den Namen auf zwei Wegen: per **mDNS/Avahi**
(Apple-Geräte lösen `.local` ausschließlich so auf) und per **DNS im
DHCP-Server** des APs (Windows/Android). Das Web-UI lauscht auf Port 80,
darum reicht `http://blaufilter.local` ohne Portangabe; `http://192.168.4.1`
geht immer.

### Captive Portal

Nach dem Verbinden mit dem WLAN öffnet sich die Steuerseite **von selbst** —
wie im Hotel-WLAN. Dahinter stecken zwei Bausteine: Der DHCP-DNS beantwortet
*jeden* Namen mit `192.168.4.1`, und der Controller leitet jeden unbekannten
Pfad auf die Steuerseite um. Die Verbindungsprüfung, die Handys nach dem
Beitritt automatisch durchführen, landet damit beim Controller und das Gerät
zeigt die Seite an.

Öffnet sich nichts (manche Geräte unterdrücken die Abfrage), führt weiterhin
jede beliebige Adresse im Browser zum Ziel — auch `blaufilter.local` oder
`192.168.4.1`. In der Mini-Ansicht mancher Handys ist der Funktionsumfang
eingeschränkt; „Im Browser öffnen“ zeigt die vollständige Seite.

## Bedienung

Mit dem WLAN `Blaufilter` verbinden und **http://blaufilter.local** öffnen
(Fallback, falls die Namensauflösung am Gerät klemmt: `http://192.168.4.1`):

### Hauptseite

Bewusst auf drei Bedienelemente reduziert — geeignet für Touch-Panels:

- **Play/Pause** — wirkt auf alle Geräte gleichzeitig.
- **Geschwindigkeit** 0,1×–3,0× in 0,05er-Schritten. Der Balken zeigt den Wert
  als Füllstand; hineintippen oder ziehen, die Marken darunter sind Schnellwahl.
  Ab etwa 2× kann der Pi bei 4K-Material Einzelbilder auslassen — die Geräte
  bleiben trotzdem synchron.
- **Zufallsposition** — alle Geräte springen gemeinsam an dieselbe zufällige
  Stelle. Beim Hochfahren passiert das automatisch, sobald das erste Gerät eine
  Videolänge meldet (abschaltbar mit `random_start = no`).

### Debug-Seite

Über das Zahnrad unten rechts, geschützt durch eine vierstellige PIN
(Standard **1234**, änderbar über `debug_pin` in `/etc/blaufilter/config`;
leerer Wert schaltet die Abfrage ab). Die PIN hält Unbefugte von den
gefährlichen Funktionen fern, ist aber **keine Transportverschlüsselung** —
das Web-UI läuft über einfaches HTTP im geschlossenen WLAN.

- **Statuspanel** — Zustand, verbundene Geräte, letzte Korrektur, Videolänge,
  Laufzeit des Controllers und die aktuelle Videodatei.
- **Gerätetabelle** — zeigt pro Gerät Position und aktuellen Drift (grün
  < 250 ms, orange < 500 ms, rot darüber). Das Master-Gerät ist markiert.
  Offline-Kandidaten erscheinen grau.
- **Jetzt neu synchronisieren** — erzwingt sofortigen Seek aller Geräte auf
  die Master-Position.
- **Wiedergabe von vorn** — setzt alle Geräte auf Position 0.
- **Video austauschen** — Datei im Web-UI wählen, Ziel bestimmen und „Video
  verteilen“: Bei **„Alle Geräte“** wird `/opt/blaufilter/video/main.mp4` auf
  dem Host ersetzt und per WLAN an alle erreichbaren Clients kopiert (Agent
  auf Port 4213); Offline-Geräte werden übersprungen. Bei einem **einzelnen
  Ziel** bekommt nur dieses Gerät die Datei — so kann jedes Gerät ein eigenes
  Video zeigen. **Alle Videos müssen gleich lang sein**, sonst passt die
  Drift-Synchronisation nicht (das Status-Panel warnt bei abweichenden
  Längen). Optional wird VLC nach dem Upload neu gestartet; das Gerät steigt
  dann bei 0 ein und wird vom Controller auf die Master-Position gezogen.
  Große 4K-Dateien über 2,4‑GHz-WLAN können mehrere Minuten dauern; auf dem
  Host wird während des Uploads kurzzeitig etwa der doppelte Speicherplatz
  der Datei benötigt (Empfangspuffer + Zieldatei).

## Wie die Synchronisation funktioniert

- Der Controller pollt alle VLCs ~10× pro Sekunde über deren RC-Interface.
  Da VLC die Position nur in ganzen Sekunden meldet, wird der Sekundenwechsel
  abgepasst (Boundary-Sampling) und dazwischen mit der Abspielrate
  extrapoliert → Messgenauigkeit ~±150 ms.
- **Sanfte Korrektur (Standard):** Abweichungen von 0,15–3 s werden unsichtbar
  über eine temporär um 2–8 % verstellte Abspielrate ausgeglichen — kein
  Ruckeln, das Gerät „schwimmt" zurück auf die Master-Position.
- **Seek nur als Notbremse:** Erst ab > 3 s Drift (3 Zyklen in Folge) wird
  gesprungen — ein Seek stoppt die 4K-Dekodierung sichtbar und landet je nach
  Keyframe-Abstand nicht exakt. Springt ein Gerät wiederholt kurz
  hintereinander, verdoppelt sich seine Abkühlphase automatisch (10 s → … →
  60 s), damit keine Ruckel-Schleife entsteht.
- Am Loop-Übergang (±3 s um Anfang/Ende) sind Korrekturen unterdrückt, damit
  der versetzte Umbruch der Geräte keinen Seek-Sturm auslöst.
- **WLAN-Toleranz:** Einzelne verzögerte/verlorene RC-Antworten (normal im
  2,4-GHz-WLAN) werden toleriert; erst drei Fehler in Folge gelten als
  Verbindungsabriss. So setzt der Sync bei kurzen Funkstörungen nicht aus.

Einstellbar in `/etc/blaufilter/config`: `drift_threshold`,
`hysteresis_cycles`, `cooldown_s`, `rate_nudge`, `web_port`, `random_start`,
`debug_pin`.
Ruckelt es trotzdem periodisch: prüfen, ob das Video mit kurzem
Keyframe-Abstand (GOP ≤ 2 s) kodiert ist — Seeks landen sonst weit daneben
und provozieren Folgekorrekturen.

### Updates einspielen

Auf jedem Gerät: `git pull` im Repo, dann das Install-Script **mit denselben
Argumenten wie bei der Erstinstallation** erneut ausführen. Auf Geräten, die
nur noch im Blaufilter-WLAN hängen (kein Internet), installiert das Script
offline aus dem lokalen Repo — **neue Python-Abhängigkeiten können dabei
nicht nachgeladen werden**. Bringt ein Update neue Abhängigkeiten mit (z. B.
`waitress` für den Video-Agent), das Gerät vorübergehend per Ethernet oder
anderem WLAN ans Internet hängen.

## Notausstieg: `blaufilter.txt` auf der Boot-Partition

Auf der FAT-Boot-Partition der SD-Karte (unter Linux `/boot/firmware/`) liegt
`blaufilter.txt` — die Datei ist **an jedem Computer** editierbar: SD-Karte
einstecken, Boot-Laufwerk öffnen, ändern, wieder booten.

```
fullscreen=yes   # no -> Video läuft im Fenster, Desktop bleibt bedienbar
autostart=yes    # no -> VLC startet gar nicht (System-Rettung)
```

Änderungen wirken ab dem nächsten Boot (oder sofort per
`systemctl --user restart blaufilter-vlc`). Damit kommt man immer wieder ins
System, selbst wenn Tastatur/SSH nicht helfen.

## Fehlersuche

- **Direkt am Gerät arbeiten, aber VLC ist im Vollbild:** `Esc` oder `f`
  beendet das Vollbild (`Leertaste` pausiert), danach über die Taskleiste ein
  Terminal öffnen. VLC stoppen: `systemctl --user stop blaufilter-vlc`
  (ohne sudo; wegen `Restart=always` hilft killen/schließen allein nicht).
  Empfehlung: einmalig `sudo systemctl enable --now ssh` — dann geht die
  Administration jederzeit per SSH (`ssh <user>@192.168.4.1` im
  Blaufilter-WLAN), egal was auf dem Bildschirm läuft.
- **Läuft VLC?** `systemctl --user status blaufilter-vlc` (als Desktop-User).
- **Läuft der Controller?** (Host) `sudo systemctl status blaufilter-controller`
  bzw. `journalctl -u blaufilter-controller -f` — dort stehen Geräte-Joins
  und jede Driftkorrektur.
- **Läuft der Video-Agent?** (alle Pis) `sudo systemctl status blaufilter-agent`
  — ohne Agent schlägt die Video-Verteilung aus dem Web-UI fehl.
- **VLC startet nach Login nicht** (User-Unit bleibt `inactive`): Manche
  Sessions aktivieren `graphical-session.target` nicht zuverlässig. Dafür liegt
  ein Fallback in `~/.config/autostart/blaufilter-vlc.desktop`, der die Unit
  beim Desktop-Start anstößt. Prüfen mit `systemctl --user start blaufilter-vlc`.
- **WLAN sichtbar, aber Verbinden schlägt fehl:** Bei bestehenden
  Installationen die AP-Verschlüsselung auf reines WPA2/CCMP festnageln und
  den DHCP-Unterbau sicherstellen (ab Script-Stand mit Kanal/CCMP-Pinning ist
  das bereits Teil der Installation):

  ```bash
  sudo nmcli connection modify blaufilter-ap \
      wifi-sec.wps-method disabled \
      802-11-wireless.channel 6 \
      wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp \
      wifi-sec.pmf disable
  sudo apt-get install -y dnsmasq-base
  sudo nmcli connection down blaufilter-ap; sudo nmcli connection up blaufilter-ap
  sudo nmcli -s connection show blaufilter-ap | grep psk   # PSK-Tippfehler ausschließen?
  ```

  Typische Symptome: **Windows fragt nach einer PIN** statt nach dem
  Passwort → der AP kündigt WPS an (`wps-method disabled` behebt das; im
  Windows-Dialog funktioniert auch der Link „Stattdessen mit
  Sicherheitsschlüssel verbinden"). Handys melden dann oft fälschlich
  „falsches Passwort". Nach der Korrektur am Client das gespeicherte Netz
  erst „vergessen", dann neu verbinden.

  Verbindungsversuche live beobachten: auf dem Host
  `sudo journalctl -u wpa_supplicant -u NetworkManager -f` laufen lassen,
  während sich ein Client verbindet — „invalid MIC" heißt: das Passwort
  kommt tatsächlich falsch an.
- **`blaufilter.local` wird nicht aufgelöst:** `http://192.168.4.1` geht
  immer. Auf dem Host prüfen: `systemctl status blaufilter-mdns-alias`
  (mDNS-Alias für Apple-Geräte) und ob
  `/etc/NetworkManager/dnsmasq-shared.d/blaufilter.conf` existiert
  (DNS für Windows/Android; greift erst nach `systemctl restart NetworkManager`
  und neuem DHCP-Lease am Client).
- **Ein Client sendet selbst ein „Blaufilter“-WLAN**, statt dem Master
  beizutreten: Auf dem Gerät sind Host-Reste aktiv — typisch bei geklonten
  SD-Karten oder wenn versehentlich mit `--id 1` installiert wurde. Das
  aktuelle Install-Script räumt das bei einer Client-Installation automatisch
  auf; von Hand:

  ```bash
  sudo nmcli connection delete blaufilter-ap
  sudo systemctl disable --now blaufilter-controller blaufilter-mdns-alias
  sudo nmcli connection up blaufilter    # dem Master-WLAN beitreten
  cat /etc/blaufilter/config             # device_id kontrollieren (muss != 1 sein)
  ```
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
