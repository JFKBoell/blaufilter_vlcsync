#!/usr/bin/env python3
"""Phase 0: measure what the two VLC interfaces can actually tell us.

Runs against players that are already playing the same file and speak both the
RC interface (whole seconds) and the blaufilter Lua interface (microseconds).
For each one it reports the readback granularity, and for a pair it reports how
noisy the resulting drift measurement is — which is the number the whole sync
depends on.

    python3 scripts/probe_vlc_precision.py --rc 127.0.0.1:4212,127.0.0.1:4222 \\
                                           --ms 127.0.0.1:4214,127.0.0.1:4224

On a device, run it against that device alone to check the interface is there:

    python3 scripts/probe_vlc_precision.py --ms 192.168.4.12:4214
"""
from __future__ import annotations

import argparse
import socket
import statistics
import sys
import time


def parse_hosts(value: str) -> list[tuple[str, int]]:
    hosts = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        addr, _, port = item.partition(":")
        hosts.append((addr, int(port or 4212)))
    return hosts


class RcClient:
    """The interface in use today: line protocol with a '> ' prompt."""

    name = "rc"

    def __init__(self, addr: tuple[str, int]):
        self.addr = addr
        self.sock = socket.create_connection(addr, timeout=2.0)
        self.sock.settimeout(2.0)
        self._drain()

    def _drain(self) -> None:
        self.sock.settimeout(0.3)
        try:
            while self.sock.recv(4096):
                pass
        except socket.timeout:
            pass
        finally:
            self.sock.settimeout(2.0)

    def _cmd(self, command: str) -> str:
        self.sock.sendall((command + "\n").encode())
        chunks = b""
        while b"> " not in chunks:
            data = self.sock.recv(4096)
            if not data:
                break
            chunks += data
        return chunks.decode(errors="replace").replace("> ", "").strip()

    def sample(self) -> tuple[float, float | None, float | None]:
        """(local wallclock at mid round trip, position_s, device clock)"""
        sent = time.time()
        reply = self._cmd("get_time")
        at = (sent + time.time()) / 2
        try:
            return at, float(reply.splitlines()[-1]), None
        except (ValueError, IndexError):
            return at, None, None

    def close(self) -> None:
        self.sock.close()


class MsClient:
    """The proposed interface: one line carries everything, in microseconds."""

    name = "ms"

    def __init__(self, addr: tuple[str, int]):
        self.addr = addr
        self.sock = socket.create_connection(addr, timeout=2.0)
        self.sock.settimeout(2.0)
        self.buffer = b""

    def _line(self, command: str) -> str:
        self.sock.sendall((command + "\n").encode())
        while b"\n" not in self.buffer:
            data = self.sock.recv(4096)
            if not data:
                raise ConnectionError("closed")
            self.buffer += data
        line, _, self.buffer = self.buffer.partition(b"\n")
        return line.decode(errors="replace").strip()

    def sample(self) -> tuple[float, float | None, float | None]:
        sent = time.time()
        reply = self._line("t")
        at = (sent + time.time()) / 2
        parts = reply.split()
        if len(parts) < 6 or parts[0] != "t":
            return at, None, None
        position = float(parts[1]) / 1e6
        device_clock = float(parts[5]) / 1e6
        return at, (position if position or parts[4] != "none" else None), device_clock

    def seek(self, seconds: float) -> str:
        return self._line(f"s {int(round(seconds * 1e6))}")

    def close(self) -> None:
        self.sock.close()


def collect(client, duration: float, interval: float) -> list[tuple]:
    samples = []
    deadline = time.time() + duration
    while time.time() < deadline:
        samples.append(client.sample())
        time.sleep(interval)
    return samples


def describe_steps(client, samples: list[tuple]) -> None:
    steps, values = [], []
    previous = None
    for at, position, _clock in samples:
        if position is None:
            continue
        values.append(position)
        if previous is not None and position != previous[1]:
            steps.append((at - previous[0], position - previous[1]))
        if previous is None or position != previous[1]:
            previous = (at, position)

    print(f"  {client.name} {client.addr[0]}:{client.addr[1]}")
    print(f"    samples          : {len(values)}")
    if not steps:
        print("    no change seen — is it playing?")
        return
    content = [c for _w, c in steps]
    print(f"    changes          : {len(steps)}")
    print(f"    step size (media): min {min(content) * 1000:.1f} ms  "
          f"median {statistics.median(content) * 1000:.1f} ms  "
          f"max {max(content) * 1000:.1f} ms")
    print(f"    quantisation     : {min(content) * 1000:.0f} ms "
          f"→ boundary sampling gives roughly ±{interval_hint / 2 * 1000:.0f} ms")


def drift_noise(clients, duration: float, interval: float) -> None:
    """Estimate the drift between two players repeatedly and report the spread.

    Both players run from the same file; whatever their true offset is, it is
    constant over a few seconds. So the spread of the estimates IS the
    measurement noise — no reference clock needed.
    """
    trackers = [Boundary() for _ in clients]
    estimates = []
    deadline = time.time() + duration
    while time.time() < deadline:
        now = None
        for client, tracker in zip(clients, trackers):
            at, position, device_clock = client.sample()
            tracker.observe(at, position, device_clock)
            now = time.time()
        a, b = (t.estimate(now) for t in trackers)
        if a is not None and b is not None:
            estimates.append(a - b)
        time.sleep(interval)

    if len(estimates) < 10:
        print(f"    not enough estimates ({len(estimates)})")
        return
    spread = statistics.pstdev(estimates)
    print(f"    estimates        : {len(estimates)}")
    print(f"    mean drift       : {statistics.mean(estimates) * 1000:+.1f} ms")
    print(f"    noise (1 sigma)  : {spread * 1000:.1f} ms")
    print(f"    peak-to-peak     : {(max(estimates) - min(estimates)) * 1000:.1f} ms")


class Boundary:
    """The controller's position estimator, in miniature.

    Dates the moment the reported position changed and extrapolates from there.
    Uses the device's own clock for the dating when the interface provides one,
    which takes the network out of the measurement.
    """

    def __init__(self) -> None:
        self.last = None
        self.last_at = None
        self.boundary = None            # (local time, position)
        self.skew = None               # local - device clock

    def observe(self, at: float, position: float | None, device_clock: float | None):
        if position is None:
            self.boundary = None
            self.last = None
            return
        if device_clock is not None:
            # Minimum skew over the run: the sample with the shortest round
            # trip is the least wrong one, the same idea NTP uses.
            skew = at - device_clock
            if self.skew is None or skew < self.skew:
                self.skew = skew
            at = device_clock + self.skew
        if self.last is not None and position != self.last:
            gap = max(0.0, at - self.last_at)
            self.boundary = (at - gap / 2, position)
        self.last, self.last_at = position, at

    def estimate(self, now: float) -> float | None:
        if self.boundary is None:
            return None
        at, position = self.boundary
        return position + (now - at)


def correction_test(master: MsClient, slave: MsClient, lead: float,
                    interval: float, rounded: bool) -> dict:
    """Do one drift correction by hand and see where the slave ended up.

    This is how the real seek duration of a device is measured: the slave is
    sent to where the master will be `lead` seconds from now, and whatever drift
    is left afterwards says how wrong that guess was. A seek that takes longer
    than the lead leaves the device behind (negative residual), so

        real seek duration = commanded lead − residual

    With `rounded` the target is truncated to whole seconds, which is all the RC
    interface can do — the difference between the two runs is what the precise
    seek buys.
    """
    trackers = {id(c): Boundary() for c in (master, slave)}

    def settle(seconds: float):
        deadline = time.time() + seconds
        last = (None, None)
        while time.time() < deadline:
            for client in (master, slave):
                at, position, clock = client.sample()
                trackers[id(client)].observe(at, position, clock)
            now = time.time()
            last = tuple(trackers[id(c)].estimate(now) for c in (master, slave))
            time.sleep(interval)
        return last

    before = settle(2.5)
    if None in before:
        return {"error": "no position estimate — are both playing?"}
    baseline = before[1] - before[0]

    master_now = trackers[id(master)].estimate(time.time())
    target = master_now + lead
    if rounded:
        target = round(target)
    slave.seek(target)
    trackers[id(slave)] = Boundary()          # the old calibration is void

    after = settle(5.0)
    if None in after:
        return {"error": "slave did not report a position again within 5 s"}
    residual = after[1] - after[0]
    return {
        "baseline_ms": baseline * 1000,
        "commanded_lead_ms": (target - master_now) * 1000,
        "residual_ms": residual * 1000,
        "seek_duration_ms": (target - master_now - residual) * 1000,
    }


interval_hint = 0.02


def main() -> int:
    global interval_hint
    parser = argparse.ArgumentParser()
    parser.add_argument("--rc", default="", help="host:port[,host:port] RC interface")
    parser.add_argument("--ms", default="", help="host:port[,host:port] blaufilter interface")
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument("--interval", type=float, default=0.02)
    parser.add_argument("--seek-test", action="store_true",
                        help="ask the ms interface for a fractional seek and check it landed")
    parser.add_argument("--correction-test", action="store_true",
                        help="run one drift correction on the second --ms player and "
                             "report how long its seek really takes (needs two players)")
    parser.add_argument("--lead", type=float, default=1.0,
                        help="seek lead to use for --correction-test (default 1.0 s)")
    args = parser.parse_args()
    interval_hint = args.interval

    for label, hosts, factory in (("RC (whole seconds)", parse_hosts(args.rc), RcClient),
                                  ("blaufilter (microseconds)", parse_hosts(args.ms), MsClient)):
        if not hosts:
            continue
        print(f"\n== {label}")
        clients = []
        for addr in hosts:
            try:
                clients.append(factory(addr))
            except OSError as e:
                print(f"  {addr[0]}:{addr[1]} not reachable: {e}")
        if not clients:
            continue
        for client in clients:
            describe_steps(client, collect(client, args.seconds, args.interval))
        if len(clients) >= 2:
            print(f"  drift between {clients[0].addr[1]} and {clients[1].addr[1]}:")
            drift_noise(clients[:2], args.seconds, args.interval)
        if args.correction_test and factory is MsClient and len(clients) >= 2:
            for rounded in (False, True):
                how = "whole seconds (what RC can do)" if rounded else "microseconds"
                print(f"  correction with a {args.lead:.2f} s lead, target in {how}:")
                result = correction_test(clients[0], clients[1], args.lead,
                                         args.interval, rounded)
                if "error" in result:
                    print(f"    {result['error']}")
                    continue
                print(f"    drift before      : {result['baseline_ms']:+.1f} ms")
                print(f"    lead commanded    : {result['commanded_lead_ms']:+.1f} ms")
                print(f"    drift after       : {result['residual_ms']:+.1f} ms")
                print(f"    → seek took about : {result['seek_duration_ms']:.0f} ms "
                      f"(use as seek_lead_s)")

        if args.seek_test and factory is MsClient:
            target = 12.345678
            client = clients[0]
            print(f"  seek test: asking for {target} s")
            client.seek(target)
            time.sleep(1.0)
            _at, position, _clock = client.sample()
            print(f"    reported afterwards: {position:.6f} s "
                  f"(offset {position - target:+.3f} s incl. playback since)")
        for client in clients:
            client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
