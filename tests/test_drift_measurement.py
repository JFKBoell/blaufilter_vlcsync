"""How accurately the drift between devices is measured.

Reported from the installation: drift values spiking towards a second and
falling back to zero moments later, while the devices themselves looked fine.
That is a measurement artefact, and these tests pin down its causes.

The clock is faked throughout — with real timing the outcome would depend on
where a poll happens to fall relative to a second boundary, and the test would
pass or fail by luck.
"""
from __future__ import annotations

import time

import pytest

from vlcsync.vlc_state import VlcId

from blaufilter import controller as controller_module
from blaufilter.config import BlaufilterConfig
from blaufilter.controller import Controller, DeviceView
from blaufilter.tracker import PositionTracker


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeVlc:
    """Playback that follows the clock exactly, answering after a round trip.

    Half the delay is spent before the position is read and half after, which
    is what a request/response over WiFi looks like.
    """

    def __init__(self, clock: FakeClock, started_at: float, delay: float = 0.0):
        self.clock = clock
        self.started_at = started_at
        self.delay = delay

    def true_position(self) -> float:
        return self.clock() - self.started_at

    def get_seek(self):
        self.clock.advance(self.delay / 2)
        value = int(self.true_position())   # VLC reports whole seconds
        self.clock.advance(self.delay / 2)
        return value


def build(monkeypatch, delays):
    """One device per round-trip delay, all playing in exact sync."""
    clock = FakeClock()
    monkeypatch.setattr(controller_module.time, "time", clock)
    controller = Controller(BlaufilterConfig(max_devices=0), env=None)
    devices = []
    for index, delay in enumerate(delays):
        device = DeviceView(vlc=FakeVlc(clock, clock(), delay))
        device.length = 3600
        controller.devices[VlcId(f"10.0.0.{index + 1}", 4212)] = device
        devices.append(device)
    return clock, controller, devices


def poll_for(clock, controller, seconds, step=0.1):
    until = clock() + seconds
    while clock() < until:
        controller._poll_positions()
        clock.advance(step)


def test_a_slow_device_does_not_skew_the_others_measurement(monkeypatch):
    """The bug: one clock reading taken before the loop was used for every
    device, although they are polled one after another. A device answering
    slowly pushed everything polled after it seconds off."""
    clock, controller, devices = build(monkeypatch, [0.6, 0.0])
    poll_for(clock, controller, 6.0)

    now = clock()
    slow, fast = (d.tracker.est_position(now, 1.0) for d in devices)
    assert slow is not None and fast is not None, "beide Geräte müssen kalibriert sein"
    assert abs(slow - fast) < 0.05, (
        f"gemessener Drift {abs(slow - fast) * 1000:.0f} ms, obwohl beide "
        "Geräte exakt synchron laufen"
    )


def test_estimate_matches_the_true_position(monkeypatch):
    """Accuracy is bounded by how often the device is polled: the second
    change can lie anywhere between two polls, so half that gap is the best
    achievable — and the estimate has to actually reach it."""
    delay, step = 0.3, 0.1
    clock, controller, devices = build(monkeypatch, [delay])
    poll_for(clock, controller, 6.0, step)

    now = clock()
    estimate = devices[0].tracker.est_position(now, 1.0)
    assert estimate is not None
    best_possible = (delay + step) / 2
    assert abs(estimate - devices[0].vlc.true_position()) <= best_possible


def test_drift_of_a_device_that_really_runs_late(monkeypatch):
    """The measurement must still report genuine drift."""
    clock, controller, devices = build(monkeypatch, [0.0, 0.0])
    devices[1].vlc.started_at += 2.0      # zweites Gerät läuft 2 s hinterher
    poll_for(clock, controller, 6.0)

    now = clock()
    reference, late = (d.tracker.est_position(now, 1.0) for d in devices)
    assert late == pytest.approx(reference - 2.0, abs=0.05)


def test_a_second_change_is_dated_to_the_middle_of_the_poll_gap():
    """The change happened somewhere between the two polls. Dating it at the
    later poll puts the position systematically late; the middle is unbiased.

    Truth here: the position was exactly 11 somewhere in (100.0, 100.1].
    """
    tracker = PositionTracker()
    tracker.observe(100.0, 10)
    tracker.observe(100.1, 11)
    # Bestenfalls 11.0 bei 100.05 -> bei 100.6 also 11.55, Fehler höchstens 50 ms
    assert tracker.est_position(100.6, 1.0) == pytest.approx(11.55, abs=0.01)


def test_a_delayed_poll_costs_accuracy_but_stays_close():
    """A poll that arrives 1.9 s late can only place the change within that
    window. The estimate must stay near the truth instead of assuming the
    change happened at the moment the reply arrived — that assumption was
    what produced drift spikes of about a second."""
    tracker = PositionTracker()
    tracker.observe(100.0, 10)
    tracker.observe(100.1, 11)          # 11.0 fiel in (100.0, 100.1]
    tracker.observe(102.0, 12)          # verspätete Abfrage, 1,9 s Lücke

    truth = 13.9                        # bei 103.0, ausgehend von 11.0 bei 100.1
    naive = 13.0                        # so rechnete die alte Fassung
    estimate = tracker.est_position(103.0, 1.0)
    assert abs(estimate - truth) < 0.5
    assert abs(estimate - truth) < abs(naive - truth)


def test_a_jump_still_invalidates_the_calibration():
    tracker = PositionTracker()
    tracker.observe(100.0, 10)
    tracker.observe(100.1, 11)
    tracker.observe(100.2, 40)          # Sprung: Seek oder Schleifenumbruch
    assert tracker.est_position(100.3, 1.0) is None
