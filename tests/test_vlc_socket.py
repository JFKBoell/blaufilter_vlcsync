"""Thread-safety and timeout isolation for VlcSocket / VlcProcs."""
from __future__ import annotations

import socket
import threading
import time

import pytest

from vlcsync.vlc import Vlc, VlcProcs
from vlcsync.vlc_socket import VlcSocket, VlcConnectionError
from vlcsync.vlc_state import VlcId

from blaufilter.config import BlaufilterConfig
from blaufilter.finder import StaticCandidateFinder

from tests.rc_emulator import EmulatedPlayer, RcServerEmulator


def test_import_does_not_set_global_socket_timeout():
    # Regression: socket.setdefaulttimeout(0.5) leaked into Flask and probes.
    assert socket.getdefaulttimeout() is None


def test_cmd_after_close_raises(rc_server):
    vlc_id = VlcId("127.0.0.1", rc_server.port)
    sock = VlcSocket(vlc_id)
    sock.close()
    with pytest.raises(VlcConnectionError):
        sock.cmd("get_time")


def test_concurrent_cmds_do_not_interleave(rc_server):
    vlc = Vlc(VlcId("127.0.0.1", rc_server.port))
    errors = []
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait(timeout=5)
            for _ in range(20):
                assert vlc.get_seek() is not None
                assert vlc.play_state().value in ("playing", "paused", "stopped")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive()

    assert errors == []
    vlc.close()


def test_close_unblocks_and_dereg_is_safe(rc_server):
    cfg = BlaufilterConfig(dev_hosts=[rc_server.address])
    env = VlcProcs({StaticCandidateFinder(cfg)})
    deadline = time.time() + 5
    while time.time() < deadline and not env.all_vlc:
        time.sleep(0.05)
    assert env.all_vlc, "emulator should be discovered"

    vlc_id = next(iter(env.all_vlc))
    env.dereg(vlc_id)
    assert vlc_id not in env.all_vlc
    env.close()


@pytest.fixture
def rc_server():
    server = RcServerEmulator(EmulatedPlayer(length=120, start_position=10))
    yield server
    server.close()
