"""Times shown in the web UI.

From the installation: "Letzte Korrektur: vor 40h" on a device that had been
up for 6h34m. The Pi has no real-time clock and no internet on its own
network, so its clock sits wherever it happened to land at boot. The page
subtracted that from the phone's clock, and the difference between two
unrelated clocks is meaningless.
"""
from __future__ import annotations

import time

import pytest

from vlcsync.vlc_state import VlcId

from blaufilter import controller as controller_module
from blaufilter.config import BlaufilterConfig
from blaufilter.controller import Controller, DeviceView


class FakeClock:
    def __init__(self, start: float):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    # A clock far away from real time — exactly what a Pi without RTC shows
    fake = FakeClock(start=time.time() - 40 * 3600)
    monkeypatch.setattr(controller_module.time, "time", fake)
    return fake


def build(clock):
    controller = Controller(BlaufilterConfig(max_devices=0), env=None)
    device = DeviceView(vlc=None)
    device.length = 3600
    controller.devices[VlcId("10.0.0.1", 4212)] = device
    return controller, device


def test_age_is_measured_against_the_controllers_own_clock(clock):
    controller, device = build(clock)
    clock.advance(6 * 3600 + 34 * 60)          # Gerät läuft 6h34m
    device.last_correction_at = clock() - 90   # Korrektur vor 90 Sekunden

    status = controller.status_snapshot()
    assert status["last_correction_age_s"] == pytest.approx(90, abs=1)
    assert status["uptime_s"] == pytest.approx(6 * 3600 + 34 * 60, abs=1)


def test_age_never_exceeds_the_uptime(clock):
    """The symptom that was reported: an age far larger than the uptime can
    only come from comparing two different clocks."""
    controller, device = build(clock)
    clock.advance(2 * 3600)
    device.last_correction_at = clock() - 120

    status = controller.status_snapshot()
    assert status["last_correction_age_s"] <= status["uptime_s"]


def test_devices_report_their_own_age(clock):
    controller, device = build(clock)
    clock.advance(600)
    device.last_correction_at = clock() - 42

    row = controller.status_snapshot()["devices"][0]
    assert row["last_correction_age_s"] == pytest.approx(42, abs=1)


def test_no_correction_yet_reports_nothing(clock):
    controller, _device = build(clock)
    status = controller.status_snapshot()
    assert status["last_correction_age_s"] is None
    assert status["devices"][0]["last_correction_age_s"] is None


def test_the_page_does_not_compare_clocks():
    """Guards the fix itself: the template must not subtract the controller's
    timestamp from the browser's clock again."""
    import pathlib
    page = pathlib.Path("blaufilter/static/index.html").read_text()
    assert "last_correction_age_s" in page
    assert "Date.now() / 1000 - " not in page
