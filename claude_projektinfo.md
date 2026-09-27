# Blaufilter — Projektinfo für Claude

Orientierungsdatei für neue Sessions: was das System ist, wie es aufgebaut ist,
welche Mechanismen tragen, und wo die Fallen liegen. Bewusst dicht geschrieben.
Sie ersetzt kein Codelesen, soll aber verhindern, dass jede Session dieselben
Dinge neu herausfindet.

*Letzter Stand: Commit nach „Phase 0" der Präzisions-Untersuchung
(`docs/sync-praezision-plan.md`).*

---

## 1. Worum es geht

Kunstinstallation: **1–6 Raspberry Pi 4** spielen je ein 4K-Video in
Endlosschleife im Vollbild, **bildsynchron**. Gerät 1 („host", ID 1) spannt einen
offenen WLAN-Access-Point auf, betreibt den Sync-Controller und das Web-Interface;
die übrigen Geräte („client", ID ≥ 2) verbinden sich dorthin. Besucher und
Betreiber steuern über ein Handy oder einen Touchscreen: Play/Pause,
Geschwindigkeit, Sprung an eine Zufallsstelle. Die Videos pro Gerät sind
**unterschiedlich im Inhalt, aber gleich lang**.

Das Repository ist ein Fork von [`vlcsync`](https://github.com/mrkeuz/vlcsync).
Das Original (`vlcsync/`) bleibt unangetastet und funktionsfähig; alles Neue liegt
in `blaufilter/` und `deploy/`. vlcsyncs eigenes Modell („letzte Änderung
gewinnt", Peer-to-Peer) ist für diesen Fall unbrauchbar, weil VLCs RC-Interface
die Abspielrate **setzen, aber nicht lesen** kann — eine Ratenänderung könnte sich
also nie über Zustandsvergleich verbreiten. Daher ein zentraler Controller mit
gewolltem Soll-Zustand.

### Arbeitsregeln in diesem Repo

- Entwicklung **ausschließlich** auf Branch `claude/raspi-video-sync-y45isr`.
- **Kein** Pull Request, solange der Nutzer nicht ausdrücklich darum bittet.
- **Keine** Modellbezeichnungen in Commits, PRs, Code oder Kommentaren.
- Commit-Nachrichten: englisch, erklären *warum*, nicht *was*. Code-Kommentare:
  englisch. Nutzerdokumentation (`deploy/README.md`) und UI: deutsch.
- Tests gehören zu jeder Verhaltensänderung; die Suite ist der Ort, an dem die
  gefundenen Hardware-Bugs festgehalten sind.

---

## 2. Repo-Landkarte

```
blaufilter/
  main.py          Einstiegspunkt Controller (click); startet VlcProcs,
                   Controller-Thread und waitress-Webserver
  controller.py    Kernstück: Tick-Loop, Master-Wahl, Driftmessung,
                   Rate-Nudge, Seek-Korrektur, Vorhaltezeit, Status-Snapshot
  tracker.py       PositionTracker (Boundary-Sampling) + modular_diff()
  config.py        BlaufilterConfig-Dataclass, Laden/Schreiben, Tuning-Grenzen
  web.py           Flask-App: Steuer- und Debug-Endpunkte, PIN-Gate,
                   Captive-Portal-Redirect
  static/index.html  komplettes UI (eine Datei: Haupt-, PIN- und Debug-Ansicht)
  finder.py        StaticCandidateFinder: TCP-Probe der festen Kandidatenliste
  agent.py         Mini-HTTP-Agent je Gerät: /health, PUT /video, /vlc/restart
  agent_main.py    Einstiegspunkt des Agents
  distribute.py    Video an Peers schieben, Fingerprints abfragen, VLC starten
  video_ops.py     atomarer Dateiersatz, inhaltsbasierter Fingerprint

deploy/
  install.sh       Orchestrierung der Schritte, alle Optionen
  steps/*.sh       05-config, 10-base, 20-network-{host,client}, 25-firewall,
                   26-txpower, 30-vlc-autostart, 35-agent, 40-controller, 50-splash
  blaufilter-setup.sh   Wartungsmenü (whiptail) — auf dem Gerät als
                        /usr/local/bin/blaufilter-setup verlinkt
  blaufilter-start-vlc.sh   VLC-Starter, liest Not-Override von der Boot-Partition
  systemd/, autostart/, blaufilter-firewall.nft, splash.png
  vlc/blaufilter.lua    (neu, Phase 1) µs-genaues Steuerinterface für VLC 3
  README.md        Betriebshandbuch, deutsch — die Nutzerdokumentation

scripts/probe_vlc_precision.py   Messwerkzeug für Interface-Präzision (Phase 0)
docs/sync-praezision-plan.md     Plan für ms-genauen Sync
tests/                           siehe Abschnitt 9
```

---

## 3. Was auf dem Gerät läuft

| Einheit | Wo | Aufgabe |
|---|---|---|
| `blaufilter-vlc.service` (systemd **--user**) | alle | VLC im Vollbild, gebunden an `graphical-session.target`, Fallback über Desktop-Autostart |
| `blaufilter-controller.service` (system) | nur host | Controller + Web-UI, `CAP_NET_BIND_SERVICE` für Port 80 |
| `blaufilter-agent.service` (system) | alle | HTTP-Agent Port 4213 für Videoverteilung |
| `blaufilter-firewall.service` | alle | lädt `nftables`-Tabelle beim Boot |
| `blaufilter-mdns-alias.service` | nur host | `blaufilter.local` |

Ports: **4212** VLC-RC (heute), **4213** Agent, **80** Web-UI,
**4214** vorgesehen für das neue Lua-Interface.

Pfade: `/etc/blaufilter/config` (root), `/opt/blaufilter/state/tuning`
(Service-Nutzer, gewinnt gegen die Config), `/opt/blaufilter/video/main.mp4`,
`/opt/blaufilter/start-vlc.sh`, `/boot/firmware/blaufilter.txt` (Not-Override:
`fullscreen=no`, `autostart=no` — von jedem Rechner per SD-Karte editierbar).

IP-Schema: host `192.168.4.1`, Clients `192.168.4.{10+ID}`, also ID 2 →
`.12`. Siehe `BlaufilterConfig.ip_for_id()` / `id_for_ip()`.

---

## 4. Der Sync-Mechanismus

**Tick-Loop** (`Controller._tick`, alle `TICK_INTERVAL = 0.1 s`):
Geräteliste abgleichen → evtl. Zufallsstart → Positionen abfragen → jeden
10. Tick Play-Zustand erzwingen → Drift korrigieren.

**Positionsmessung.** VLCs RC `get_time` liefert nur ganze Sekunden. Der
`PositionTracker` wartet auf den Sekundenwechsel („Boundary-Sampling"): springt
der Wert von *n* auf *n+1*, ist die Position zu diesem Zeitpunkt exakt *n+1*.
Der Wechsel wird auf die **Mitte zwischen den beiden Abfragen** datiert,
dazwischen wird mit der Abspielrate extrapoliert. Jede Antwort bekommt den
Zeitstempel ihres **eigenen** Umlaufs (`(sent + received) / 2`) — ein einziger
Zeitstempel vor der Schleife ließ ein langsam antwortendes Gerät alle
nachfolgenden verfälschen.

**Master.** `_pick_master()` nimmt das Gerät auf `ip_for_id(1)`, sonst das
niedrigste `(addr, port)`. Der Master wird nie korrigiert.

**Zwei Korrekturwege**, in dieser Reihenfolge:

1. **Rate-Nudge** (unsichtbar): Abweichung ab `NUDGE_MIN_DRIFT = 0.15 s` wird
   über eine temporär verstellte Abspielrate ausgeglichen, Stärke 2–15 %
   (`_nudge_skew`, Ziel ≈ `NUDGE_TARGET_S = 10 s` Angleichzeit). Ende bei
   `NUDGE_DONE_DRIFT = 0.05 s`. Nachjustiert wird höchstens alle
   `NUDGE_RETARGET_S = 3 s` und nur bei ≥ 2 % Änderung, weil jede
   Ratenänderung in VLC eine kleine Taktkorrektur ist. Der Nudge greift
   **auch über** der Sprungschwelle — sonst passiert während Hysterese und
   Abkühlphase gar nichts.
2. **Seek** (sichtbarer Stotterer, Notbremse): erst wenn die Abweichung
   `cfg.drift_threshold` (Standard 3 s) über `cfg.hysteresis_cycles` (3) Ticks
   hält und die Abkühlphase vorbei ist. Wiederholte Korrekturen **ohne
   Verbesserung** verdoppeln die Abkühlphase bis `SEEK_COOLDOWN_MAX_S = 60`;
   eine konvergierende Folge wird nicht gebremst (`SEEK_PROGRESS_S`).

**Vorhaltezeit (`seek_lead_s`).** Ein Seek wirkt nicht sofort: VLC leert und
füllt den Dekoder (4K HEVC ≈ 1 s) und spielt **erst dann** von der
angeforderten Stelle weiter, ohne die Zeit nachzuholen. Ziel ist deshalb die
Position, an der der Master *nach* dem Seek stehen wird. Die reale Dauer wird
pro Gerät aus der Restabweichung gelernt (EMA, `SEEK_LEAD_ALPHA = 0.3`,
Messung erst nach `SEEK_LEAD_SETTLE_S = 2 s`, Aufgabe nach
`SEEK_LEAD_GIVE_UP_S = 20 s`, unplausible Werte > `SEEK_LEAD_PLAUSIBLE_S`
werden verworfen). Während der Messung wird das Gerät nicht angefasst, sonst
verdeckt der Nudge genau den Fehler, den wir lesen wollen.

**Bildversatz (`offset_ms_<ID>`).** `get_time` meldet den Stand des *Inputs*,
nicht das Bild; dazwischen liegen Dekoder, Compositor, Display. Kameramessung
ergab 300–350 ms, wo der Controller 175 ms zeigte. Nicht beobachtbar, daher
Kalibrierwert: positiv = das Bild läuft so weit hinter der Meldung her. Alle
Vergleiche laufen danach in „Bildzeit" (`master_ref`, `_offset_s`).

**Weitere Regeln.** Am Loop-Übergang (±`LOOP_BOUNDARY_GRACE_S = 3 s`) sind
Korrekturen gesperrt, weil die Geräte den Umbruch zu unterschiedlichen
Zeitpunkten überqueren und `get_time` dort unstet ist. `_seek_all()`
(Zufallsstelle, „von vorn") verschiebt jedes Gerät um seinen eigenen Vorlauf
relativ zum schnellsten. WLAN-Toleranz: erst
`CONN_FAIL_DROP_AFTER = 3` Fehler in Folge lassen ein Gerät fallen.

**Pause ist ein Toggle.** RC `pause` schaltet um, und VLCs Statusmeldung hängt
der Wirklichkeit um Sekunden nach. Ein erneutes `pause` wird deshalb **nur**
gesendet, wenn die Position nach dem letzten Befehl beweisbar weitergelaufen ist
(`PAUSE_RESEND_MIN_ADVANCE`), niemals wegen einer Statusmeldung. Umgekehrt gilt
eine stehende Position bei Soll-Zustand PLAYING als Hänger → `play`.
`_note_commanded_seek()` sorgt dafür, dass ein von uns befohlener Sprung nicht
als „läuft ja" gezählt wird.

---

## 5. Konfiguration

Zwei Dateien, `tuning` gewinnt:

- `/etc/blaufilter/config` — vom Installer geschrieben, aber nur die von ihm
  **verwalteten** Schlüssel (`device_id role debug_pin ssid open_wifi txpower
  repo_dir`, siehe `steps/05-config.sh`). Handgepflegte Werte überleben ein
  erneutes Install.
- `/opt/blaufilter/state/tuning` — vom Web-UI geschrieben. **Nicht** in
  `/etc/blaufilter`: das Verzeichnis gehört root, und ein atomarer Schreibvorgang
  legt erst eine temporäre Datei *im Verzeichnis* an — die Zieldatei zu
  übereignen reicht nicht.

`TUNING_LIMITS` (web-änderbar, wird geklemmt statt abgelehnt):
`drift_threshold` 0.2–10, `hysteresis_cycles` 1–20, `cooldown_s` 1–120,
`seek_lead_s` 0–5. Dazu `device_offsets_ms` (± `OFFSET_LIMIT_MS = 2000`,
gespeichert als `offset_ms_<ID>`, Nullwerte werden verworfen).
Weiteres in der Config: `rate_nudge`, `web_port`, `random_start`, `debug_pin`,
`video_path`, `vlc_unit`, `max_devices`, `subnet`, `rc_port`, `agent_port`.
Rate 0.1–3.0 (`RATE_MIN/MAX`). `dev_hosts` bzw. `BLAUFILTER_HOSTS` überschreibt
die Kandidatenliste für lokale Entwicklung.

---

## 6. Web-UI

Eine Datei, `blaufilter/static/index.html`, drei Ansichten: **Haupt** (Wortmarke,
Play/Pause mit blauem Farbverlauf und nur Glyphe, Block-Slider 0.1–3.0,
Zufallsposition, Zahnrad), **PIN-Pad**, **Debug** (Status, Gerätetabelle,
Driftkorrektur, Bildversatz, Videoaustausch). Schwarz/Weiß, Tokens in `:root`,
keine erklärenden Texte im UI — das ist ausdrücklicher Wunsch.

Offene Endpunkte: `GET /api/status`, `POST /api/play|pause|rate|seek_random`,
`GET /api/tuning`, `POST /api/debug/unlock`.
PIN-geschützt (`X-Debug-Pin`, `hmac.compare_digest`): `POST /api/resync`,
`/api/restart_playback`, `/api/tuning`, `/api/video` (POST/PUT),
`/api/video/activate`, `GET /api/video/peers`.
Der 404-Handler leitet alles außerhalb von `/api/` auf `/` — zusammen mit dem
Wildcard-DNS-Eintrag in dnsmasq ist das das Captive Portal.

**Zeiten werden als Alter in Sekunden ausgeliefert**, nicht als Zeitstempel: der
Pi hat keine RTC und in seinem eigenen Netz kein NTP, seine Uhr steht irgendwo.
Ein Browser, der davon `Date.now()` abzieht, zeigt „vor 40 h" bei 6 h Laufzeit.

---

## 7. Videoverteilung

Jedes Gerät betreibt `agent.py` auf 4213: `GET /health`, `PUT /video` (Rohbody,
atomar ersetzt), `POST /vlc/restart` (startet die **User**-Unit über
`systemd-run --machine=…` bzw. `sudo -u`, siehe `restart_vlc_unit`). Der
Controller schiebt entweder an **alle** Geräte oder an **eines** (`?target=<ID>`)
— letzteres ist der Weg zu unterschiedlichen Videos pro Gerät. Abwesende Geräte
werden per `/health` erkannt und als `skipped` gemeldet, nicht als Fehler.
`video_ops.content_fingerprint()` hasht Größe + erstes/letztes MiB (mit Cache auf
`(size, mtime_ns)`) — Mtime allein taugt nicht, weil eine Kopie sie ändert.

---

## 8. Netzwerk & Installation

AP über NetworkManager (`ipv4.method shared`, eingebautes dnsmasq),
`wifi-sec.wps-method disabled` (ohne das assoziieren Clients nicht), offen heißt
**wifi-sec komplett weglassen** — `key-mgmt none` wäre WEP. Clients löschen das
`blaufilter-ap`-Profil und deaktivieren Controller und mDNS (geklonte SD-Karten
haben sonst beides). `nftables` schützt in eigener Tabelle mit `policy accept`
nur 4212/4213. Sendeleistung reduzierbar (`26-txpower.sh`).

`install.sh` ruft die Schritte in Nummernreihenfolge; Pakete werden in
`10-base.sh` installiert, **bevor** der AP hochkommt (danach gibt es kein
Internet mehr). `50-splash.sh` baut die initramfs nur neu, wenn überhaupt eine
konfiguriert ist — sonst meldete es Fehler, obwohl der Splash korrekt gesetzt war.
SSH wird über `raspi-config nonint do_ssh 0` aktiviert.

`deploy/blaufilter-setup.sh` ist das Wartungsmenü (Status, Dienste
start/stop/restart, Rolle, WLAN inkl. Beitritt zu bekannten Netzen temporär oder
dauerhaft, Auflösung über `cmdline.txt`/`video=`, Schreibschutz per Overlay-FS,
PIN, Video, Splash, Update, Logs, Power). Es editiert die Config **direkt** statt
`install.sh` aufzurufen. `set -u` ohne `-e`/`pipefail` — siehe Fallen.

---

## 9. Tests

```bash
python3 -m venv .venv
.venv/bin/pip install -q loguru flask waitress requests psutil click pytest
.venv/bin/pytest -q tests/ --ignore=tests/integration --ignore=tests/test_main.py \
    --ignore=tests/test_utils.py --ignore=tests/test_bench_copy_dicts.py
bash tests/shell/run.sh
```

Die vier ausgenommenen Dateien sind **vorbestehende** Upstream-/Umgebungsfehler
(fehlendes `ffmpeg`, `click`-Version, geänderte `LocalProcessFinderProvider`,
`pytest-benchmark`) und haben nichts mit Blaufilter zu tun.

- `tests/rc_emulator.py` — Fake-VLC über TCP, spricht genug RC. Kennt
  `apply_skew()` für künstliche Drift und `seek_latency` für langsame Seeks.
- `tests/test_controller.py` — Sync-Verhalten gegen echte Sockets, mit
  `tick_until`/`tick_for`-Helfern (die auch andere Dateien importieren).
- `tests/test_drift_measurement.py`, `test_status_times.py`,
  `test_seek_lead.py` — deterministisch mit gefälschter Uhr
  (`monkeypatch.setattr(controller_module.time, "time", fake)`).
- `tests/shell/` — eigene Mini-Harness (`harness.sh`) mit Sandbox, gefälschtem
  DRM-Baum und Stubs für `nmcli`, `systemctl`, `raspi-config`, `whiptail`, `ip`,
  `hostnamectl`; Antworten werden über eine dateibasierte Warteschlange
  (`queue_answers`) vorgegeben, weil whiptail in einer Subshell läuft.

---

## 10. Fallen, die schon Zeit gekostet haben

- `PlayState.UNKNOWN.value` ist `None`; ein `__repr__`, das `None` zurückgibt,
  ließ die `DeviceView`-Dataclass **beim Import** abstürzen (Python 3.11.2 ruft
  `repr()` auf Feld-Defaults für den Docstring). Deshalb `str(self.value)` und
  ein expliziter Docstring.
- `sed 's/…/&…/'` — `&` in der Ersetzung heißt „das ganze Match" und zerstörte
  Werte mit `&`. `cfg_set` nutzt jetzt `awk` mit `ENVIRON`.
- `set -e` + `pipefail` + fehlschlagende Kommandosubstitution in einer Zuweisung
  = stiller Skriptabbruch. Das Wartungsmenü verzichtet daher auf beides.
- `${VAR:-default}` behandelt *leer* wie *ungesetzt* — in Stubs `${VAR-default}`.
- Bash cacht Kommandopfade; nach dem Anlegen eines Stubs `hash -r`.
- Default-Argumente binden zur Definitionszeit: Pfade, die Tests ersetzen, müssen
  `None` als Default haben und zur Laufzeit aufgelöst werden.
- Als root laufen Rechte-Tests ins Leere → `pytest.mark.skipif(os.geteuid() == 0)`.
- VLC startet **nicht als root**. Für Experimente hier einen Nutzer anlegen.
- Der Pi wird stromlos abgeschaltet; SD-Korruption ist ein realer Fehlerfall
  (ein Git-Cache ist schon zerstört worden). Deshalb Overlay-FS im Menü.
- Das Wartungswerkzeug muss ein **Symlink** ins Repo sein, keine Kopie, sonst
  laufen Gerät und Branch auseinander; die Kopfzeile zeigt `branch @ commit`.

---

## 11. Offene Punkte

- `docs/sync-praezision-plan.md`: Weg zu ms-genauem Sync über ein eigenes
  Lua-Interface für VLC 3. **Phase 0 ist gemessen und positiv** (Messrauschen
  99 ms → 0,4 ms, µs-genauer Seek, explizites Play/Pause). Phase 1 ist
  `deploy/vlc/blaufilter.lua` — geschrieben und gegen echtes VLC 3.0.20
  getestet, aber noch nicht auf einem Pi und nicht in den Controller
  integriert. Bindung an Bookworm/VLC 3 ist ausdrücklich in Ordnung.
- Angeboten, aber nicht bestellt: Ereignisprotokoll auf der Debug-Seite,
  Platzprüfung vor dem Upload, Merge des Feature-Branches nach `main`.
