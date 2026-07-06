import time

import pytest

from vlcsync.vlc import VlcProcs

from blaufilter.config import BlaufilterConfig
from blaufilter.controller import Controller
from blaufilter.finder import StaticCandidateFinder
from blaufilter.tracker import modular_diff

from tests.rc_emulator import EmulatedPlayer, RcServerEmulator


@pytest.fixture
def stack(request):
    servers, envs = [], []

    def build(players):
        servers.extend(RcServerEmulator(p) for p in players)
        cfg = BlaufilterConfig(dev_hosts=[s.address for s in servers])
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
    servers, controller = stack(players)

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
