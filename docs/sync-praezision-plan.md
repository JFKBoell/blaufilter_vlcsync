# Millisekundengenauer Sync — Plan und Phase-0-Ergebnisse

Ziel: die Abweichung zwischen den Geräten von heute ~300 ms auf **±20–35 ms**
bringen, also auf „immer dasselbe Bild, gelegentlich eines daneben". Weg dahin:
ein eigenes Lua-Steuerinterface für VLC 3, das die Position in Mikrosekunden
liefert statt in ganzen Sekunden.

Bindung an Raspberry Pi OS Bookworm und VLC 3 ist ausdrücklich akzeptiert.

---

## Warum überhaupt

VLCs RC-Interface — heute `cli.lua`, gestartet über `--extraintf lua --rc-host` —
rundet die Position auf ganze Sekunden:

```lua
client:append(math.floor(vlc.var.get(input, "time") / 1000000))
```

Der Mikrosekundenwert ist in Lua also längst vorhanden und wird nur
weggeworfen. Dasselbe Interface kann außerdem nur auf ganze Sekunden springen,
und sein `pause` ist ein Umschalter, was uns einen eigenen Beweismechanismus
über Positionsbewegung gekostet hat.

## Was physikalisch erreichbar ist

4K30 heißt **33,3 ms pro Bild**. Unter etwa 16 ms Abweichung zeigen beide Geräte
garantiert dasselbe Bild; darunter wird es sinnlos, weil zwei unabhängige Displays
ohne Genlock ihre Ausgabephase nicht teilen. **±33 ms ist der Boden**, nicht 0.

---

## Phase 0 — gemessen, nicht geschätzt

Aufbau: VLC **3.0.20** (Bookworm hat 3.0.21, gleiche API), zwei Instanzen
derselben Datei, die zweite absichtlich **genau 1 s später** gestartet, Dummy-Vout,
localhost. Gemessen mit `scripts/probe_vlc_precision.py`; das neue Interface ist
`deploy/vlc/blaufilter.lua`. Die wahre Abweichung ist damit bekannt: 1000 ms.

| | RC heute | Lua µs (neu) |
|---|---|---|
| Rauschen der Driftmessung (1σ) | **99,0 ms** | **0,4 ms** |
| Spitze-Spitze | 347,6 ms | 2,0 ms |
| Gemessener Mittelwert (wahr: 1000 ms) | 1038,8 ms | 1000,4 ms |
| Abfragen in 6 s bei 20 ms Intervall | 94 | 292 |

Das Rauschen sinkt um **Faktor 250**, der systematische Fehler von 39 ms auf
0,4 ms, und das Interface verkraftet dreimal so viele Abfragen — RCs
Prompt-Protokoll ist der langsamere Teil.

### Weitere bestätigte Punkte

- Ein eigenes Script aus `~/.local/share/vlc/lua/intf/` wird über
  `-I luaintf --lua-intf <name>` geladen; `--lua-config "<name>={host='0.0.0.0:4214'}"`
  kommt im Script als globales `config` an.
- `vlc.var.get(input, "time")` liefert **Mikrosekunden**.
- `vlc.var.set(input, "time", µs)` springt **exakt** dorthin (Rückgabe
  `12345678` für 12,345678 s) — die ±500 ms Rundung entfällt vollständig.
- `vlc.var.set(input, "state", 2|3)` ist play/pause **explizit und
  idempotent**: zweimal „pause" bleibt pausiert. Damit fällt die ganze
  Toggle-Absicherung im Controller weg.
- `vlc.misc.mdate()` gibt die monotone Uhr des Geräts in µs — jede Antwort kann
  auf der **Geräteuhr** datiert werden statt auf einer Schätzung aus dem Umlauf.
- `vlc.net.listen_tcp` / `poll` / `recv` / `send` / `close` sind vorhanden und
  tragen ein Zeilenprotokoll stabil.
- Der Korrekturtest (`--correction-test`) misst die **echte Seek-Dauer** eines
  Geräts: hier 69 ms für 360p H.264 auf x86. Genau dieses Kommando liefert auf
  dem Pi den richtigen Wert für `seek_lead_s`.

### Zwei Einschränkungen, die der Test auch gezeigt hat

1. **`time` springt in 250-ms-Schritten.** VLC frischt die Variable erst auf,
   wenn die Uhr des Demuxers um ~250 ms weiter ist (gemessen: Schritte von
   250,0 ms, gelegentlich 500 ms). Der *Wert* ist dabei µs-exakt. Das ist kein
   Problem, sondern das bessere Boundary-Sampling: viermal häufiger als heute,
   und der Wechsel lässt sich mit dichter Abfrage auf ±10 ms datieren. Es heißt
   aber: `PositionTracker` bleibt nötig, nur mit feinerem Raster.
2. **`time` folgt dem Demuxer, nicht dem Bild.** Der Vorlauf durch Puffer und
   Ausgabe bleibt unsichtbar. Er ist pro Gerät konstant und wird weiterhin über
   `offset_ms` eingemessen — kein Interface der Welt liefert ihn.

### Was hier *nicht* prüfbar war

4K-HEVC-Dekodierung auf dem Pi, echte Seek-Dauer bei langem GOP, WLAN-Jitter,
Bildschirmlatenz. Deshalb beginnt Phase 1 mit demselben Messkommando auf zwei
echten Geräten.

### Nebenbefund für die Rückfallebene

`--extraintf oldrc --rc-host …` lief in diesem Test **nicht** parallel an
(„no suitable interface module"). Beide Interfaces gleichzeitig zu betreiben ist
also nicht gesichert; der Rückfall auf RC bleibt ein Wechsel der Startparameter
(eine Zeile in `deploy/blaufilter-start-vlc.sh`, umschaltbar über das
Wartungsmenü) — das ist ohnehin robuster als zwei Kontrollwege gleichzeitig.

---

## Protokoll

Zeilenbasiert, eine Anfrage pro Zeile, eine Antwort pro Zeile, kein Prompt, mit
`nc` von Hand bedienbar.

| Anfrage | Antwort |
|---|---|
| `t` | `t <time_us> <length_us> <rate> <state> <mdate_us>` |
| `s <time_us>` | `ok` (absolut, Mikrosekunden) |
| `r <rate>` | `ok` |
| `play` / `pause` | `ok` (explizit, idempotent) |
| `ping` | `pong <mdate_us>` |
| sonst | `err …` |

Ein Umlauf pro Tick statt heute drei (`status`, `get_time`, `get_length`).

### Uhrenabgleich ohne NTP

Da jede Antwort `mdate_us` mitbringt, lässt sich der Versatz zwischen
Controller- und Geräteuhr wie bei NTP über das **Minimum** der beobachteten
Differenzen schätzen: die Antwort mit dem kürzesten Umlauf ist die am wenigsten
verfälschte. Damit fällt der WLAN-Jitter aus der Messung, ohne `chrony` zu
brauchen. Der Prototyp in `scripts/probe_vlc_precision.py` (`class Boundary`)
macht genau das.

---

## Phasen

**Phase 1 — Interface auf die Geräte, ~0,5 Tag.**
`deploy/vlc/blaufilter.lua` nach `~/.local/share/vlc/lua/intf/` installieren
(neuer Schritt bzw. Erweiterung von `30-vlc-autostart.sh`), Startparameter in
`blaufilter-start-vlc.sh` um `-I luaintf --lua-intf blaufilter --lua-config …`
ergänzen, Umschalter für die Rückfallebene. Danach auf zwei Pis:

```bash
python3 scripts/probe_vlc_precision.py \
    --ms 192.168.4.1:4214,192.168.4.12:4214 --correction-test
```

Ergebnis: reales Messrauschen über WLAN und die echte Seek-Dauer bei 4K HEVC.
**Prüfpunkt:** Rauschen < 20 ms, Seek-Dauer plausibel und reproduzierbar.

**Phase 2 — Python-Client, ~0,5 Tag.**
`blaufilter/msvlc.py` mit derselben Oberfläche wie `vlcsync.vlc.Vlc`
(`get_seek`, `seek`, `set_rate`, `play`, `pause`, `play_state`, `get_length`)
plus `sample()` für den vollständigen Zustand in einem Umlauf. Erkennung beim
Verbinden: Port 4214 erreichbar → präzise, sonst RC. `finder.py` probt dann
beide Ports. `tests/rc_emulator.py` um das neue Protokoll erweitern, damit die
bestehenden Controller-Tests beide Wege abdecken.

**Phase 3 — Controller nachziehen, ~1 Tag.**
Sprünge in µs (Rundung raus, damit auch der Hauptgrund für die langsame
Restangleichung), `PositionTracker` auf das 250-ms-Raster und die Geräteuhr
umstellen, Totband von 150/50 ms auf ~20/5 ms, `drift_threshold` Standard von
3 s auf ~0,3 s, Lernrate der Vorhaltezeit höher (ein Sample ist dann
aussagekräftig). Toggle-Absicherung für Pause entfernen — mit `play`/`pause`
explizit wird `PAUSE_RESEND_MIN_ADVANCE` und die Bewegungsbeweisführung
überflüssig. Tickrate von 100 ms auf ~25 ms, das Interface verkraftet es.
**Prüfpunkt:** Kameramessung gegen den Stand von heute.

**Phase 4 — optional: Uhren, ~0,5 Tag.**
Nur falls Phase 1 zeigt, dass der Minimum-Filter nicht reicht: `chrony` auf dem
host als lokale Referenz (`local stratum 10`), Clients synchronisieren gegen ihn,
kein Internet nötig. Nebeneffekt: die Debug-Seite könnte wieder echte Zeitstempel
zeigen.

**Phase 5 — optional, zeitlich begrenzt: `netsync`.**
VLCs eingebautes `--netsync-master` / `--netsync-master-ip` synchronisiert die
Input-Uhr statt zu springen — konzeptionell die richtige Lösung, aber schlecht
gepflegt. Erst nach Phase 3 bewertbar, weil dann die Messtechnik dafür existiert.

---

## Risiken

- Das Lua-Script läuft in VLCs Thread. `vlc.net.poll` **blockiert** bis zu einem
  Ereignis oder bis VLC beendet wird (dann Fehler „Interrupted." → Schleife
  verlassen); ein Timeout-Argument wird ignoriert, `vlc.misc.should_die()` gibt es
  in VLC 3 nicht. `listener:accept()` blockiert ebenfalls, darf also nur nach
  einem `POLLIN` auf dem Listener aufgerufen werden. Beides ist in
  `blaufilter.lua` so umgesetzt — Abweichungen davon hängen den Player auf.
- Die Lua-API hat sich in **VLC 4** geändert (`vlc.object.input()` →
  `vlc.player`). Das Script ist an VLC 3 gebunden; ein Distributionssprung ist
  eine Baustelle. Akzeptiert.
- Ein zusätzlicher offener Port (4214) muss in `blaufilter-firewall.nft` mit
  aufgenommen werden.
- Bildschirmlatenz bleibt Kalibrierwert, auch nach allen Phasen.
