from __future__ import annotations

from typing import Optional


def modular_diff(a: float, b: float, length: float) -> float:
    """Signed smallest difference a - b on a circle of circumference `length`.

    A device at 10799.8s and one at 0.3s of a 10800s video are 0.5s apart,
    not 3 hours. Result is in (-length/2, length/2].
    """
    diff = (a - b) % length
    if diff > length / 2:
        diff -= length
    return diff


class PositionTracker:
    """Estimates sub-second playback position from VLC's integer-second `get_time`.

    RC `get_time` only returns whole seconds. When the returned integer
    increments between two polls, the true position at that instant is exactly
    the new value (boundary sampling). Between boundaries the position is
    extrapolated with the playback rate. Accuracy is roughly bounded by the
    poll interval plus network jitter.
    """

    def __init__(self):
        self._boundary_time: Optional[float] = None
        self._boundary_value: Optional[int] = None
        self._last_seen: Optional[int] = None

    def observe(self, wallclock: float, get_time_value: Optional[int]):
        if get_time_value is None:
            self.reset()
            return

        last = self._last_seen
        self._last_seen = get_time_value

        if last is None:
            return

        if get_time_value == last + 1:
            # Clean single-second increment: position is exactly N.0 now
            self._boundary_time = wallclock
            self._boundary_value = get_time_value
        elif get_time_value != last:
            # Backward jump or gap (seek, loop wrap, stall): recalibrate
            self._boundary_time = None
            self._boundary_value = None

    def est_position(self, now: float, rate: float) -> Optional[float]:
        if self._boundary_time is None:
            return None
        return self._boundary_value + rate * (now - self._boundary_time)

    def reset(self):
        self._boundary_time = None
        self._boundary_value = None
        self._last_seen = None
