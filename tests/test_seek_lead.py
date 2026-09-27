"""Seek lead and picture-offset calibration.

Both come from filming the running installation. Two findings:

* "nach dem Springen sind die Videos ca. 2000ms versetzt" — a seek does not
  take effect when it is sent. The decoder flushes and refills, and playback
  then resumes from the commanded position without making up the time it spent
  doing so. Sending the master's *current* position therefore puts the corrected
  device a full seek behind, every single time.
* "bei 175ms im Webinterface echte 300-350ms Abweichung" — get_time reports
  where the input stands, not what is on the screen. The controller cannot
  observe the difference, so it is a measured calibration value.
"""
from __future__ import annotations

import time

import pytest

from vlcsync.vlc_state import PlayState, VlcId

from blaufilter import controller as controller_module
from blaufilter.config import BlaufilterConfig
from blaufilter.controller import (SEEK_LEAD_ALPHA, SEEK_LEAD_MAX_S,
                                   SEEK_LEAD_PLAUSIBLE_S, Controller, DeviceView)

from tests.rc_emulator import EmulatedPlayer
from tests.test_controller import slave_pairs, stack, tick_for, tick_until  # noqa: F401

MASTER_IP = "192.168.4.1"
SLAVE_IP = "192.168.4.12"      # device id 2


# --------------------------------------------------------------- unit harness

class FakeClock:
    def __init__(self, start: float):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeVlc:
    def __init__(self, vlc_id: VlcId):
        self.vlc_id = vlc_id
        self.seeks: list[int] = []
        self.rates: list[float] = []

    def seek(self, target):
        self.seeks.append(target)

    def set_rate(self, rate):
        self.rates.append(rate)


class FixedTracker:
    """Reports one position, ignoring time — the timing of the estimate is
    covered by test_drift_measurement.py, this is about what is done with it."""

    def __init__(self, position):
        self.position = position

    def est_position(self, _now, _rate):
        return self.position

    def reset(self):
        self.position = None


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock(start=time.time())
    monkeypatch.setattr(controller_module.time, "time", fake)
    return fake


def build(clock, master_pos=500.0, slave_pos=500.0, **cfg):
    cfg.setdefault("rate_nudge", False)
    cfg.setdefault("hysteresis_cycles", 1)
    cfg.setdefault("seek_lead_s", 0.0)
    controller = Controller(BlaufilterConfig(**cfg), env=None)
    devices = {}
    for ip, position in ((MASTER_IP, master_pos), (SLAVE_IP, slave_pos)):
        vlc_id = VlcId(ip, 4212)
        device = DeviceView(vlc=FakeVlc(vlc_id))
        device.length = 3600
        device.tracker = FixedTracker(position)
        device.play_state = PlayState.PLAYING
        controller.devices[vlc_id] = device
        devices[ip] = device
    return controller, devices[MASTER_IP], devices[SLAVE_IP]


# ------------------------------------------------------------- picture offset

def test_an_offset_makes_the_reported_drift_match_the_camera(clock):
    """Both players report the same position; the camera shows 300 ms. Once
    calibrated, the controller reports what the camera sees."""
    controller, _master, slave = build(clock, device_offsets_ms={2: 300})
    controller._correct_drift()
    assert slave.last_drift == pytest.approx(-0.3)


def test_without_the_offset_the_same_situation_looks_perfect(clock):
    controller, _master, slave = build(clock)
    controller._correct_drift()
    assert slave.last_drift == pytest.approx(0.0)


def test_the_offset_moves_the_seek_target(clock):
    """A device whose picture lags must be placed that much later, or every
    correction re-creates the offset it was meant to remove."""
    controller, _master, slave = build(clock, drift_threshold=0.2,
                                       device_offsets_ms={2: 600})
    controller._correct_drift()
    assert slave.vlc.seeks == [501], "500 + 0.6s lag, rounded to whole seconds"


def test_an_equal_offset_on_both_devices_cancels_out(clock):
    """Only the difference between the devices is visible on camera; a lag both
    share must not be corrected."""
    controller, _master, slave = build(clock, device_offsets_ms={1: 250, 2: 250})
    controller._correct_drift()
    assert slave.last_drift == pytest.approx(0.0)
    assert slave.vlc.seeks == []


def test_offsets_survive_a_round_trip_through_the_tuning_file(tmp_path):
    from blaufilter import config as bf_config

    path = str(tmp_path / "tuning")
    bf_config.write_tuning({"drift_threshold": 2.0,
                            "device_offsets_ms": {"2": 350, "3": -120}}, path)
    values = bf_config.read_tuning(path)
    assert values["device_offsets_ms"] == {2: 350, 3: -120}

    cfg = bf_config.load(str(tmp_path / "missing"), path)
    assert cfg.offset_s_for_ip("192.168.4.12") == pytest.approx(0.35)
    assert cfg.offset_s_for_ip("192.168.4.13") == pytest.approx(-0.12)
    assert cfg.offset_s_for_ip("192.168.4.1") == 0.0


def test_the_debug_page_offers_both_calibrations():
    import pathlib
    page = pathlib.Path("blaufilter/static/index.html").read_text()
    assert "device_offsets_ms" in page, "Eingabefelder für den Bildversatz"
    assert "t_lead" in page, "Eingabefeld für die Vorhaltezeit"
    assert "seek_lead_s" in page, "und beide gehen an /api/tuning"


def test_offsets_are_clamped_and_junk_is_dropped():
    from blaufilter.config import OFFSET_LIMIT_MS, normalize_offsets

    assert normalize_offsets({"2": 99999})[2] == OFFSET_LIMIT_MS
    assert normalize_offsets({"2": -99999})[2] == -OFFSET_LIMIT_MS
    assert normalize_offsets({"x": 10, "3": "viel"}) == {}
    assert normalize_offsets({"2": 0}) == {}, "der Standardwert wird nicht gespeichert"


# ----------------------------------------------------------------- seek lead

def test_the_seek_target_leads_the_master(clock):
    controller, _master, slave = build(clock, slave_pos=499.0,
                                       drift_threshold=0.5, seek_lead_s=1.4)
    controller._correct_drift()
    assert slave.vlc.seeks == [501], "500 + 1.4s seek time, rounded"


def test_landing_behind_increases_the_learned_lead(clock):
    controller, _master, slave = build(clock, seek_lead_s=1.0)
    slave.lead_commanded_s = 1.0
    slave.lead_seek_at = clock() - 3.0
    slave.awaiting_lead_residual = True

    controller._learn_seek_lead(slave, -1.0, clock())   # 1s behind after the seek

    needed = 1.0 - (-1.0)
    assert slave.seek_lead_s == pytest.approx(1.0 + SEEK_LEAD_ALPHA * (needed - 1.0))
    assert not slave.awaiting_lead_residual


def test_repeated_measurements_converge_on_the_real_seek_time(clock):
    """Every single sample carries up to half a second of whole-second rounding,
    so what matters is that the average lands on the truth."""
    true_seek_time = 1.6
    controller, _master, slave = build(clock, seek_lead_s=0.0)

    for _ in range(20):
        lead = controller._seek_lead(slave)
        slave.lead_commanded_s = lead
        slave.lead_seek_at = clock() - 3.0
        slave.awaiting_lead_residual = True
        # Landed as late as the seek took longer than the lead assumed
        controller._learn_seek_lead(slave, lead - true_seek_time, clock())

    assert slave.seek_lead_s == pytest.approx(true_seek_time, abs=0.05)


def test_the_lead_stays_inside_its_limits(clock):
    controller, _master, slave = build(clock, seek_lead_s=0.0)
    for residual in (-60.0, -3.0, -3.0, -3.0, -3.0, -3.0, -3.0, -3.0, -3.0, -3.0):
        slave.lead_commanded_s = controller._seek_lead(slave)
        slave.lead_seek_at = clock() - 3.0
        slave.awaiting_lead_residual = True
        controller._learn_seek_lead(slave, residual, clock())
    assert 0.0 <= slave.seek_lead_s <= SEEK_LEAD_MAX_S


def test_an_implausible_residual_is_not_learned_from(clock):
    """A device that is minutes off has a different problem — a lost stream, a
    wrong video — and must not poison the seek-time estimate."""
    controller, _master, slave = build(clock, seek_lead_s=1.0)
    slave.lead_commanded_s = 1.0
    slave.lead_seek_at = clock() - 3.0
    slave.awaiting_lead_residual = True

    controller._learn_seek_lead(slave, -(SEEK_LEAD_PLAUSIBLE_S + 10), clock())

    assert slave.seek_lead_s is None, "nichts gelernt"
    assert not slave.awaiting_lead_residual


def test_a_correction_is_left_alone_while_it_settles(clock):
    """Measuring the outcome of a seek requires not correcting in the meantime —
    a nudge would hide exactly the error we want to read off."""
    controller, _master, slave = build(clock, slave_pos=499.0, drift_threshold=0.5,
                                       rate_nudge=True, seek_lead_s=0.0)
    controller._correct_drift()
    assert slave.awaiting_lead_residual

    slave.tracker = FixedTracker(498.0)      # 2s behind after the seek
    controller._correct_drift()
    assert slave.vlc.rates == [], "kein Nudge, solange gemessen wird"
    assert slave.seek_lead_s is None, "und noch nichts gelernt"

    clock.advance(controller_module.SEEK_LEAD_SETTLE_S + 0.1)
    controller._correct_drift()
    assert slave.seek_lead_s is not None
    assert slave.seek_lead_s > 0, "zu spät gelandet -> mehr Vorhaltezeit"


def test_a_stuck_measurement_does_not_block_corrections_forever(clock):
    controller, _master, slave = build(clock, slave_pos=499.0, drift_threshold=0.5)
    controller._correct_drift()
    assert slave.awaiting_lead_residual

    slave.tracker = FixedTracker(None)       # device unreachable for a while
    clock.advance(controller_module.SEEK_LEAD_GIVE_UP_S + 1)
    controller._correct_drift()
    assert not slave.awaiting_lead_residual


def test_setting_the_lead_by_hand_replaces_what_was_learned(clock):
    controller, _master, slave = build(clock)
    slave.seek_lead_s = 2.5
    controller.set_tuning({"seek_lead_s": 0.8})
    assert slave.seek_lead_s is None
    assert controller._seek_lead(slave) == pytest.approx(0.8)


# ------------------------------------------------------- against a slow player

def test_a_slow_seek_lands_behind_without_a_lead(stack):
    """Reproduces the reported symptom: the correction itself creates the offset."""
    players = [EmulatedPlayer(length=3600, start_position=100, seek_latency=1.5),
               EmulatedPlayer(length=3600, start_position=100, seek_latency=1.5)]
    servers, controller = stack(players, drift_threshold=0.5, rate_nudge=False,
                                seek_lead_s=0.0, cooldown_s=30.0)

    assert tick_until(controller, lambda: len(controller.devices) == 2)
    assert tick_until(controller, lambda: all(
        d.last_position is not None for d in controller.devices.values()))

    (slave_server, slave_device), = slave_pairs(controller, servers)
    slave_server.player.apply_skew(2.0)
    assert tick_until(controller, lambda: len(slave_server.player.seeks_received()) > 0,
                      timeout=8.0)

    tick_for(controller, 4.0)      # let the seek finish and the tracker recover
    assert slave_device.last_drift is not None
    assert slave_device.last_drift < -0.9, \
        "ohne Vorhaltezeit landet das Gerät um die Seek-Dauer zu spät"


def test_the_lead_puts_a_slow_player_on_the_master(stack):
    players = [EmulatedPlayer(length=3600, start_position=100, seek_latency=1.5),
               EmulatedPlayer(length=3600, start_position=100, seek_latency=1.5)]
    servers, controller = stack(players, drift_threshold=0.5, rate_nudge=False,
                                seek_lead_s=1.5, cooldown_s=30.0)

    assert tick_until(controller, lambda: len(controller.devices) == 2)
    assert tick_until(controller, lambda: all(
        d.last_position is not None for d in controller.devices.values()))

    (slave_server, slave_device), = slave_pairs(controller, servers)
    slave_server.player.apply_skew(2.0)
    assert tick_until(controller, lambda: len(slave_server.player.seeks_received()) > 0,
                      timeout=8.0)

    tick_for(controller, 4.0)
    assert slave_device.last_drift is not None
    assert abs(slave_device.last_drift) < 0.6, \
        "nur noch die Rundung auf ganze Sekunden bleibt übrig"


def test_a_jump_puts_the_slower_player_ahead(stack):
    """A shared jump is sent at one moment but each device needs its own time to
    get there, so they must not be told the same number."""
    players = [EmulatedPlayer(length=3600, start_position=100),
               EmulatedPlayer(length=3600, start_position=100)]
    servers, controller = stack(players)
    assert tick_until(controller, lambda: len(controller.devices) == 2)

    (slave_server, slave_device), = slave_pairs(controller, servers)
    slave_device.seek_lead_s = 2.0          # as if measured: this one is slow
    master_id = controller._pick_master()
    controller.devices[master_id].seek_lead_s = 0.0

    target = controller.seek_random()
    assert target is not None
    master_server = next(s for s in servers if s.port == master_id.port)
    assert float(master_server.player.seeks_received()[-1].split()[1]) == pytest.approx(target)
    assert float(slave_server.player.seeks_received()[-1].split()[1]) == pytest.approx(target + 2)
