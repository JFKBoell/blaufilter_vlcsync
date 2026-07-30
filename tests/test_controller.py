import time

import pytest

from vlcsync.vlc import VlcProcs
from vlcsync.vlc_state import PlayState

from blaufilter.config import BlaufilterConfig
from blaufilter.controller import Controller
from blaufilter.finder import StaticCandidateFinder
from blaufilter.tracker import modular_diff

from tests.rc_emulator import EmulatedPlayer, RcServerEmulator


@pytest.fixture
def stack(request):
    servers, envs = [], []

    def build(players, **cfg_overrides):
        servers.extend(RcServerEmulator(p) for p in players)
        cfg = BlaufilterConfig(dev_hosts=[s.address for s in servers], **cfg_overrides)
        env = VlcProcs({StaticCandidateFinder(cfg)})
        envs.append(env)
        return servers, Controller(cfg, env)

    yield build

    for env in envs:
        env.close()
    for server in servers:
        server.close()


def tick_until(controller, predicate, timeout=10.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with controller.lock:
            controller._tick()
        if predicate():
            return True
        time.sleep(interval)
    return False


def tick_for(controller, duration, interval=0.05):
    deadline = time.time() + duration
    while time.time() < deadline:
        with controller.lock:
            controller._tick()
        time.sleep(interval)


def slave_pairs(controller, servers):
    """(server, device) tuples for all non-master devices."""
    master_id = controller._pick_master()
    result = []
    for vlc_id, device in controller.devices.items():
        if vlc_id != master_id:
            server = next(s for s in servers if s.port == vlc_id.port)
            result.append((server, device))
    return result


def test_playstate_repr_is_always_str():
    # PlayState.UNKNOWN.value is None; a non-string __repr__ crashed the
    # DeviceView dataclass creation at import time on Python 3.11.2 (Bookworm)
    for state in PlayState:
        assert isinstance(repr(state), str)


def test_new_devices_get_rate_and_play_state(stack):
    players = [
        EmulatedPlayer(length=3600, start_position=100),
        EmulatedPlayer(length=3600, start_position=100),
        EmulatedPlayer(length=3600, start_position=100, playing=False),
    ]
    servers, controller = stack(players)

    assert tick_until(controller, lambda: len(controller.devices) == 3)

    for player in players:
        assert player.rates_received(), "desired rate must be pushed to every new device"
        assert player.state == "playing", "desired play state must be enforced on join"

    controller.set_rate(1.5)
    for player in players:
        assert player.rate == pytest.approx(1.5)


def test_drift_triggers_exactly_one_correction(stack):
    players = [
        EmulatedPlayer(length=3600, start_position=100),
        EmulatedPlayer(length=3600, start_position=100),
    ]
    # Force the seek path: low threshold, no smooth nudge
    servers, controller = stack(players, drift_threshold=0.5, rate_nudge=False)

    assert tick_until(controller, lambda: len(controller.devices) == 2)
    assert tick_until(controller,
                      lambda: all(d.last_position is not None for d in controller.devices.values()))

    (slave_server, slave_device), = slave_pairs(controller, servers)
    slave_server.player.apply_skew(2.0)

    assert tick_until(controller, lambda: len(slave_server.player.seeks_received()) > 0,
                      timeout=8.0), "sustained drift must trigger a seek correction"

    # Cooldown: no second correction right after the first
    tick_for(controller, 1.0)
    assert len(slave_server.player.seeks_received()) == 1

    target = float(slave_server.player.seeks_received()[0].split()[1])
    master_pos = controller.devices[controller._pick_master()].last_position
    assert abs(modular_diff(target, master_pos, 3600)) < 2.0

    # Drift is actually gone after recalibration
    assert tick_until(controller,
                      lambda: slave_device.last_drift is not None
                      and abs(slave_device.last_drift) < 0.5,
                      timeout=8.0)


def test_no_correction_near_loop_boundary(stack):
    players = [
        EmulatedPlayer(length=20, start_position=18.5),
        EmulatedPlayer(length=20, start_position=18.5),
    ]
    servers, controller = stack(players)

    assert tick_until(controller, lambda: len(controller.devices) == 2)

    for server, _ in slave_pairs(controller, servers):
        server.player.apply_skew(1.0)

    # Master crosses the wrap during this window; grace zone must suppress seeks
    tick_for(controller, 2.0)
    for player in players:
        assert not player.seeks_received()


def test_rate_change_causes_no_false_corrections(stack):
    players = [
        EmulatedPlayer(length=3600, start_position=50),
        EmulatedPlayer(length=3600, start_position=50),
    ]
    servers, controller = stack(players)

    assert tick_until(controller, lambda: len(controller.devices) == 2)
    assert tick_until(controller,
                      lambda: all(d.last_position is not None for d in controller.devices.values()))

    controller.set_rate(2.0)
    for player in players:
        assert player.rate == pytest.approx(2.0)

    tick_for(controller, 2.0)
    for player in players:
        assert not player.seeks_received()


def test_moderate_drift_uses_rate_nudge_not_seek(stack):
    """Default behavior: drift below the seek threshold is corrected smoothly
    via a temporary rate skew — no seek, so no visible stutter."""
    players = [
        EmulatedPlayer(length=3600, start_position=500),
        EmulatedPlayer(length=3600, start_position=500),
    ]
    servers, controller = stack(players)  # defaults: threshold 3.0, nudge on

    assert tick_until(controller, lambda: len(controller.devices) == 2)
    assert tick_until(controller,
                      lambda: all(d.last_position is not None for d in controller.devices.values()))

    (slave_server, slave_device), = slave_pairs(controller, servers)
    slave_server.player.apply_skew(1.0)  # below threshold -> nudge territory

    assert tick_until(controller, lambda: slave_device.nudging, timeout=8.0), \
        "moderate drift must trigger a rate nudge"
    assert slave_server.player.rate != pytest.approx(1.0), "nudge must skew the rate"
    assert not slave_server.player.seeks_received(), "no seek for moderate drift"

    # Nudge converges: skewed rate pulls the position back until drift is gone,
    # then the desired rate is restored
    assert tick_until(controller,
                      lambda: not slave_device.nudging
                      and slave_device.last_drift is not None
                      and abs(slave_device.last_drift) < 0.3,
                      timeout=60.0)
    assert slave_server.player.rate == pytest.approx(1.0)
    assert not slave_server.player.seeks_received()


def test_seek_cooldown_backs_off_on_rapid_recorrection(stack):
    players = [
        EmulatedPlayer(length=3600, start_position=100),
        EmulatedPlayer(length=3600, start_position=100),
    ]
    servers, controller = stack(players, drift_threshold=0.5, rate_nudge=False,
                                cooldown_s=1.0)

    assert tick_until(controller, lambda: len(controller.devices) == 2)
    assert tick_until(controller,
                      lambda: all(d.last_position is not None for d in controller.devices.values()))

    (slave_server, slave_device), = slave_pairs(controller, servers)
    slave_server.player.apply_skew(2.0)
    assert tick_until(controller, lambda: len(slave_server.player.seeks_received()) == 1,
                      timeout=8.0)
    first_cooldown = slave_device.seek_cooldown_s
    assert first_cooldown == pytest.approx(1.0)

    # Immediately drift again: the second correction must back off
    slave_server.player.apply_skew(2.0)
    assert tick_until(controller, lambda: len(slave_server.player.seeks_received()) == 2,
                      timeout=8.0)
    assert slave_device.seek_cooldown_s > first_cooldown


def test_pause_enforcement_does_not_toggle_back(stack):
    """RC 'pause' toggles. When VLC reports the old state for a moment after the
    command, re-enforcement must NOT send a second 'pause' (which would resume)."""
    players = [
        EmulatedPlayer(length=3600, start_position=10),
        EmulatedPlayer(length=3600, start_position=10),
    ]
    servers, controller = stack(players)
    assert tick_until(controller, lambda: len(controller.devices) == 2)

    # Freeze the reported state at 'playing' to simulate VLC's settling lag
    for server in servers:
        server.player.report_state_override = "playing"

    controller.pause()
    for _ in range(5):
        with controller.lock:
            controller._enforce_play_state()

    for player in (s.player for s in servers):
        pauses = [c for c in player.received if c.strip() == "pause"]
        assert len(pauses) == 1, "grace period must prevent double pause-toggle"

    # After the grace expires and VLC reports the real state, no further sends
    for server in servers:
        server.player.report_state_override = None
    for device in controller.devices.values():
        device.state_grace_until = 0.0
    with controller.lock:
        controller._enforce_play_state()
    for player in (s.player for s in servers):
        assert len([c for c in player.received if c.strip() == "pause"]) == 1


def test_single_connection_error_does_not_drop_device(stack):
    players = [EmulatedPlayer(length=3600, start_position=10)]
    servers, controller = stack(players)
    assert tick_until(controller, lambda: len(controller.devices) == 1)

    device = next(iter(controller.devices.values()))
    from vlcsync.vlc_socket import VlcConnectionError
    vlc_id = next(iter(controller.devices))
    controller._conn_fail(vlc_id, device)
    assert len(controller.devices) == 1, "first failure must not drop the device"
    controller._conn_fail(vlc_id, device)
    assert len(controller.devices) == 1, "second failure must not drop the device"
    controller._conn_fail(vlc_id, device)
    assert len(controller.devices) == 0, "third consecutive failure drops it"


def test_pause_and_play_fan_out(stack):
    players = [
        EmulatedPlayer(length=3600, start_position=10),
        EmulatedPlayer(length=3600, start_position=10),
    ]
    servers, controller = stack(players)

    assert tick_until(controller, lambda: len(controller.devices) == 2)

    controller.pause()
    assert all(p.state == "paused" for p in players)

    controller.play()
    assert all(p.state == "playing" for p in players)


def test_restart_playback_seeks_all_to_zero(stack):
    players = [
        EmulatedPlayer(length=3600, start_position=90),
        EmulatedPlayer(length=3600, start_position=95),
    ]
    servers, controller = stack(players)

    assert tick_until(controller, lambda: len(controller.devices) == 2)
    controller.restart_playback()
    for player in players:
        assert any(c.startswith("seek 0") for c in player.seeks_received())
        assert int(player.position()) < 2

    snap = controller.status_snapshot()
    assert snap["connected_devices"] == 2
    assert snap["expected_devices"] == 2
    assert snap["health"] in ("ok", "degraded")
