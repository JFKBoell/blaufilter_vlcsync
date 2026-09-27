from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from loguru import logger

from vlcsync.vlc import Vlc, VlcProcs
from vlcsync.vlc_socket import VlcConnectionError
from vlcsync.vlc_state import PlayState, VlcId

from blaufilter.config import (BlaufilterConfig, TUNING_LIMITS, clamp_rate,
                               clamp_tuning, normalize_offsets)
from blaufilter.tracker import PositionTracker, modular_diff
from blaufilter import video_ops
from blaufilter import distribute as video_distribute
from blaufilter.agent import restart_vlc_unit

TICK_INTERVAL = 0.1
PLAYSTATE_POLL_EVERY_N_TICKS = 10
LOOP_BOUNDARY_GRACE_S = 3.0
NUDGE_MIN_DRIFT = 0.15
NUDGE_DONE_DRIFT = 0.05
NUDGE_FACTOR_MIN = 0.02
NUDGE_FACTOR_MAX = 0.15
NUDGE_TARGET_S = 10.0
"""Convergence a rate nudge aims at. A seek only accepts whole seconds, so a
correction always leaves up to half a second behind for the nudge to absorb;
closing that took half a minute at the old 25s target, which is what "es dauert
eine Weile, bis die Werte sinken" was."""
NUDGE_RETARGET_S = 3.0
NUDGE_RETARGET_DELTA = 0.02
"""A running nudge is re-aimed only this rarely and only on this much change:
every rate command is a round trip and a small clock adjustment inside VLC."""
SEEK_LEAD_SETTLE_S = 2.0
SEEK_LEAD_GIVE_UP_S = 20.0
SEEK_LEAD_ALPHA = 0.3
SEEK_LEAD_MAX_S = 5.0
SEEK_LEAD_PLAUSIBLE_S = 5.0
CONN_FAIL_DROP_AFTER = 3
STATE_CMD_MIN_INTERVAL_S = 2.0
MOVING_WINDOW_S = 2.5
PAUSE_RESEND_MIN_ADVANCE = 2
SEEK_BACKOFF_WINDOW_S = 60.0
SEEK_COOLDOWN_MAX_S = 60.0
SEEK_PROGRESS_S = 0.25
"""How much smaller the drift must be than at the previous correction for the
correction to count as progress. Without this the backoff also punished a
sequence of corrections that was converging — which is what the first few are
while the seek lead is still being learned."""
DRIFT_WARN_MS = 250
DRIFT_BAD_MS = 500


@dataclass
class DeviceView:
    """Per-device sync state as seen by the controller.

    (Explicit docstring also keeps dataclass creation away from repr()-ing the
    field defaults on older Python patch levels — see PlayState.__repr__.)
    """
    vlc: Vlc
    tracker: PositionTracker = field(default_factory=PositionTracker)
    length: Optional[int] = None
    applied_rate: float = 1.0
    cooldown_until: float = 0.0
    over_threshold_count: int = 0
    last_drift: Optional[float] = None
    last_correction_at: Optional[float] = None
    last_position: Optional[float] = None
    play_state: PlayState = PlayState.UNKNOWN
    nudging: bool = False
    conn_fail_count: int = 0
    """Consecutive RC failures; only a streak drops the device (WiFi tolerance)."""
    last_seek_value: Optional[int] = None
    last_seek_change_at: float = 0.0
    """Movement evidence: when the integer get_time value last changed. A truly
    paused VLC never advances — unlike its RC status report, which can lag the
    real state by seconds."""
    state_cmd_sent_at: float = 0.0
    seek_value_at_state_cmd: Optional[int] = None
    """When and at which position the last play/pause command was sent. RC
    'pause' toggles, so it is only ever re-sent on movement PROOF after the
    previous send — a stale status report must never trigger a re-toggle."""
    seek_cooldown_s: float = 0.0
    """Current per-device seek cooldown; doubles on rapid re-corrections."""
    last_correction_drift: Optional[float] = None
    """Drift that triggered the previous correction, to tell a converging series
    of corrections from one that is going nowhere."""
    nudge_skew: float = 0.0
    nudge_set_at: float = 0.0
    """Relative rate skew currently applied by the nudge, and when it was set."""
    seek_lead_s: Optional[float] = None
    """Learned duration of a seek on this device; None until measured once."""
    awaiting_lead_residual: bool = False
    lead_seek_at: float = 0.0
    lead_commanded_s: float = 0.0
    """A correction is in flight and its outcome has not been measured yet: when
    it was sent and how far ahead of the master it aimed."""


class Controller:
    """Master controller: owns desired play state and rate, pushes them to all
    VLC instances and seek-corrects slaves that drift away from the master.

    Replaces vlcsync's peer "last change wins" model for this use case, because
    the VLC RC interface can only SET the playback rate, never read it back, so
    a rate change could never propagate through state-change detection.
    """

    def __init__(self, cfg: BlaufilterConfig, env: VlcProcs):
        self.cfg = cfg
        self.env = env
        self.lock = threading.RLock()
        self.desired_play_state: PlayState = PlayState.PLAYING
        self.desired_rate: float = 1.0
        self.devices: Dict[VlcId, DeviceView] = {}
        self._tick_count = 0
        self.closed = False
        self.started_at = time.time()
        self.last_video_job: Optional[dict] = None
        self._video_job_lock = threading.Lock()
        self._video_busy = False
        self._random_start_pending = cfg.random_start

    # ------------------------------------------------------------------ loop

    def run(self):
        while not self.closed:
            try:
                with self.lock:
                    self._tick()
            except VlcConnectionError as e:
                self._drop_device(e.vlc_id)
            except Exception:
                logger.opt(exception=True).warning("Controller tick failed, continuing...")
            time.sleep(TICK_INTERVAL)

    def close(self):
        self.closed = True

    def _tick(self):
        self._tick_count += 1
        self._reconcile_devices()
        self._maybe_random_start()
        self._poll_positions()
        if self._tick_count % PLAYSTATE_POLL_EVERY_N_TICKS == 0:
            self._enforce_play_state()
        self._correct_drift()

    def _maybe_random_start(self):
        """Start the loop at a random position once, as soon as the first
        player reports a video length after boot."""
        if not self._random_start_pending:
            return
        length = self._known_length()
        if not length:
            return
        self._random_start_pending = False
        target = random.randrange(length)
        print(f"Random start at {target}s", flush=True)
        self._seek_all(target)

    # ----------------------------------------------------------- reconciling

    def _reconcile_devices(self):
        current = self.env.all_vlc

        for vlc_id in list(self.devices.keys() - current.keys()):
            self._drop_device(vlc_id)

        for vlc_id, vlc in current.items():
            if vlc_id not in self.devices:
                self._init_device(vlc_id, vlc)

    def _init_device(self, vlc_id: VlcId, vlc: Vlc):
        device = DeviceView(vlc=vlc)
        try:
            device.length = vlc.get_length()
            vlc.set_rate(self.desired_rate)
            device.applied_rate = self.desired_rate
            self._apply_play_state(device)
        except VlcConnectionError:
            self.env.dereg(vlc_id)
            return
        self.devices[vlc_id] = device
        print(f"Device joined: {vlc_id} (length={device.length}s)", flush=True)

    def _drop_device(self, vlc_id: VlcId):
        if vlc_id in self.devices:
            del self.devices[vlc_id]
            print(f"Device left: {vlc_id}", flush=True)
        self.env.dereg(vlc_id)

    # --------------------------------------------------------------- polling

    def _conn_fail(self, vlc_id: VlcId, device: DeviceView):
        """Tolerate short WiFi hiccups: only a failure streak drops the device.

        A single timed-out command leaves the connection usable (VlcSocket
        drains the late reply before the next command)."""
        device.conn_fail_count += 1
        if device.conn_fail_count >= CONN_FAIL_DROP_AFTER:
            self._drop_device(vlc_id)

    def _poll_positions(self):
        # Each device gets the timestamp of its own reply, taken at the middle
        # of the round trip. Using one reading from before the loop made a
        # device that answers slowly skew everything polled after it: their
        # replies were dated back to the start of the loop, so their estimated
        # position ran ahead by however long the slow device had taken. Over
        # WiFi, where a retry costs a second, that showed up as drift spikes
        # of about that size which vanished at the next clean reading.
        for vlc_id, device in list(self.devices.items()):
            try:
                sent_at = time.time()
                value = device.vlc.get_seek()
                observed_at = (sent_at + time.time()) / 2
                device.tracker.observe(observed_at, value)
                device.conn_fail_count = 0
            except VlcConnectionError:
                self._conn_fail(vlc_id, device)
                continue
            if value is not None and value != device.last_seek_value:
                device.last_seek_value = value
                device.last_seek_change_at = observed_at

        # Extrapolated to one common instant, so the positions are comparable
        now = time.time()
        for device in self.devices.values():
            device.last_position = device.tracker.est_position(now, device.applied_rate)

    def _enforce_play_state(self):
        for vlc_id, device in list(self.devices.items()):
            try:
                self._apply_play_state(device)
                device.conn_fail_count = 0
            except VlcConnectionError:
                self._conn_fail(vlc_id, device)

    def _apply_play_state(self, device: DeviceView):
        """Enforce the desired play state against movement EVIDENCE, not just
        VLC's status report — the lua CLI status can lag the real input state
        by several seconds, and RC 'pause' is a toggle: acting on a stale
        'playing' report after a pause command would resume playback.
        """
        now = time.time()
        if device.state_cmd_sent_at and now - device.state_cmd_sent_at < STATE_CMD_MIN_INTERVAL_S:
            return
        device.play_state = device.vlc.play_state()

        # "moving": the integer position changed recently (window scaled for
        # slow playback rates, where increments arrive less often)
        window = MOVING_WINDOW_S / min(1.0, device.applied_rate or 1.0)
        moving = device.last_seek_change_at > 0 and (now - device.last_seek_change_at) < window

        if self.desired_play_state == PlayState.PLAYING:
            # 'play' does not toggle — re-sending is harmless. Send when VLC
            # reports paused/stopped OR the position provably stands still.
            if device.play_state in (PlayState.PAUSED, PlayState.STOPPED) or not moving:
                device.vlc.play()
                device.tracker.reset()
                device.play_state = PlayState.PLAYING
                device.state_cmd_sent_at = now
                device.seek_value_at_state_cmd = device.last_seek_value
        elif self.desired_play_state == PlayState.PAUSED:
            if device.seek_value_at_state_cmd is None:
                # First pause since the desired state changed: trust the (still
                # fresh) report or visible movement
                should_pause = device.play_state == PlayState.PLAYING or moving
            else:
                # A pause was already sent: ONLY movement since that command
                # proves it did not stick. A stale 'playing' status must never
                # cause a re-toggle.
                should_pause = (
                    device.last_seek_value is not None
                    and abs(device.last_seek_value - device.seek_value_at_state_cmd)
                    >= PAUSE_RESEND_MIN_ADVANCE
                )
            if should_pause:
                device.vlc.pause()  # RC "pause" toggles
                device.tracker.reset()
                device.play_state = PlayState.PAUSED
                device.state_cmd_sent_at = now
                device.seek_value_at_state_cmd = device.last_seek_value

    # ------------------------------------------------------ drift correction

    def _pick_master(self) -> Optional[VlcId]:
        if not self.devices:
            return None
        host_addr = self.cfg.ip_for_id(1)
        for vlc_id in self.devices:
            if vlc_id.addr == host_addr:
                return vlc_id
        return min(self.devices, key=lambda v: (v.addr, v.port))

    def _correct_drift(self):
        if self.desired_play_state != PlayState.PLAYING:
            return

        master_id = self._pick_master()
        if master_id is None:
            return

        now = time.time()
        master = self.devices[master_id]
        length = master.length
        master_pos = master.tracker.est_position(now, master.applied_rate)
        if master_pos is None or not length:
            return
        master.last_drift = 0.0

        # Devices cross the loop wrap at slightly different wall-clock times and
        # get_time is transient there — never correct near the boundary.
        master_pos_mod = master_pos % length
        if master_pos_mod < LOOP_BOUNDARY_GRACE_S or master_pos_mod > length - LOOP_BOUNDARY_GRACE_S:
            return

        # Everything is compared in "picture time": the reported position minus
        # the device's calibrated display lag. Without it the controller aligns
        # the two decoders' inputs, which is not what a camera sees.
        master_ref = master_pos - self._offset_s(master_id)

        for vlc_id, device in list(self.devices.items()):
            if vlc_id == master_id:
                continue
            if (device.awaiting_lead_residual
                    and now - device.lead_seek_at > SEEK_LEAD_GIVE_UP_S):
                device.awaiting_lead_residual = False
            slave_pos = device.tracker.est_position(now, device.applied_rate)
            if slave_pos is None:
                device.last_drift = None
                continue

            drift = modular_diff(slave_pos - self._offset_s(vlc_id), master_ref, length)
            device.last_drift = drift

            if device.awaiting_lead_residual:
                # A correction is being measured: leave the device alone, a
                # nudge would hide the very error we want to learn from.
                self._learn_seek_lead(device, drift, now)
                continue

            if abs(drift) > self.cfg.drift_threshold:
                device.over_threshold_count += 1
            else:
                device.over_threshold_count = 0

            try:
                if (device.over_threshold_count >= self.cfg.hysteresis_cycles
                        and now >= device.cooldown_until):
                    self._seek_correct(device, master_ref, length, now, vlc_id)
                elif self.cfg.rate_nudge:
                    self._rate_nudge(device, drift, now)
                device.conn_fail_count = 0
            except VlcConnectionError:
                self._conn_fail(vlc_id, device)

    def _offset_s(self, vlc_id: VlcId) -> float:
        return self.cfg.offset_s_for_ip(vlc_id.addr)

    def _seek_lead(self, device: DeviceView) -> float:
        return self.cfg.seek_lead_s if device.seek_lead_s is None else device.seek_lead_s

    def _learn_seek_lead(self, device: DeviceView, drift: float, now: float):
        """Learn from the outcome of the last correction how long a seek
        actually takes on this device.

        Whatever drift is left once the picture is back is the part of the seek
        the lead failed to cover: landed behind means the lead was that much too
        short. Seeks accept whole seconds only, so every single sample carries up
        to half a second of rounding error — hence a running average instead of
        taking one measurement at face value.
        """
        waited = now - device.lead_seek_at
        if waited < SEEK_LEAD_SETTLE_S:
            return                      # decoder may still be refilling
        device.awaiting_lead_residual = False
        if abs(drift) > SEEK_LEAD_PLAUSIBLE_S:
            return                      # not a seek-latency problem, don't learn
        needed = device.lead_commanded_s - drift
        previous = self._seek_lead(device)
        learned = previous + SEEK_LEAD_ALPHA * (needed - previous)
        device.seek_lead_s = max(0.0, min(SEEK_LEAD_MAX_S, learned))

    def _seek_correct(self, device: DeviceView, master_ref: float, length: int,
                      now: float, vlc_id: VlcId):
        # A 4K HEVC seek visibly stalls the decoder, and keyframe granularity
        # means it may land off-target. Rapid re-corrections therefore back off
        # exponentially instead of stuttering in a loop — unless they are getting
        # somewhere, which is exactly the case while the seek lead is still being
        # learned.
        drift = device.last_drift or 0.0
        improving = (device.last_correction_drift is not None
                     and abs(drift) < abs(device.last_correction_drift) - SEEK_PROGRESS_S)
        if (device.last_correction_at is not None
                and now - device.last_correction_at < SEEK_BACKOFF_WINDOW_S
                and not improving):
            device.seek_cooldown_s = min(
                max(device.seek_cooldown_s, self.cfg.cooldown_s) * 2,
                SEEK_COOLDOWN_MAX_S,
            )
        else:
            device.seek_cooldown_s = self.cfg.cooldown_s
        device.last_correction_drift = drift

        # Aim where the master will be when the seek has actually taken effect,
        # plus this device's own display lag. Sending the master's current
        # position instead left a corrected device a full second or two behind —
        # the seek itself takes that long, and playback resumes from the
        # commanded position, it does not catch up.
        lead = self._seek_lead(device)
        offset_s = self._offset_s(vlc_id)
        target = round(master_ref + lead + offset_s) % length
        print(f"Drift correction: seek {device.vlc.vlc_id} to {target}s "
              f"(drift {drift:+.2f}s, lead {lead:.2f}s, "
              f"next earliest in {device.seek_cooldown_s:.0f}s)",
              flush=True)
        self._end_nudge(device)
        device.vlc.seek(target)
        device.tracker.reset()
        self._note_commanded_seek(device, target)
        device.over_threshold_count = 0
        device.cooldown_until = now + device.seek_cooldown_s
        device.last_correction_at = now
        # Learn against what was really commanded, not against the lead we
        # wanted: the whole-second rounding is part of the outcome.
        device.lead_commanded_s = modular_diff(target - offset_s, master_ref, length)
        device.lead_seek_at = now
        device.awaiting_lead_residual = True

    @staticmethod
    def _note_commanded_seek(device: DeviceView, target: int):
        """A seek WE commanded must not count as movement evidence — otherwise
        the pause re-send logic would read the position jump as 'still playing'
        and toggle a paused device back on."""
        device.last_seek_value = target
        device.last_seek_change_at = 0.0
        if device.seek_value_at_state_cmd is not None:
            device.seek_value_at_state_cmd = target

    def _nudge_skew(self, drift: float) -> float:
        """Relative rate change that closes this drift in NUDGE_TARGET_S.

        Also used above the seek threshold: while the hysteresis count fills up
        and during the cooldown after a correction, a nudge is the only thing
        acting at all, and pulling at full strength is strictly better than
        waiting it out."""
        if abs(drift) <= NUDGE_MIN_DRIFT:
            return 0.0
        strength = min(NUDGE_FACTOR_MAX,
                       max(NUDGE_FACTOR_MIN, abs(drift) / NUDGE_TARGET_S))
        # Ahead (drift > 0) -> slow down; behind -> speed up
        return -strength if drift > 0 else strength

    def _rate_nudge(self, device: DeviceView, drift: float, now: float):
        """Smoothly pull an off-position device back by temporarily skewing its
        rate — invisible to viewers, unlike a seek."""
        if device.nudging and abs(drift) < NUDGE_DONE_DRIFT:
            self._end_nudge(device)
            return
        wanted = self._nudge_skew(drift)
        if wanted == 0.0:
            return
        if device.nudging and (now - device.nudge_set_at < NUDGE_RETARGET_S
                               or abs(wanted - device.nudge_skew) < NUDGE_RETARGET_DELTA):
            # Keep pulling with the skew already set; re-aiming costs a command
            return
        nudge_rate = self.desired_rate * (1 + wanted)
        device.vlc.set_rate(nudge_rate)
        device.applied_rate = nudge_rate
        device.nudging = True
        device.nudge_skew = wanted
        device.nudge_set_at = now

    def _end_nudge(self, device: DeviceView):
        if not device.nudging:
            return
        device.vlc.set_rate(self.desired_rate)
        device.applied_rate = self.desired_rate
        device.nudging = False
        device.nudge_skew = 0.0

    # ------------------------------------------------------------ web-facing

    def _set_desired_play_state(self, state: PlayState):
        # A CHANGED desired state resets the per-device command tracking (the
        # transition is new); a repeated request keeps it, protecting against
        # a double pause-toggle.
        if self.desired_play_state != state:
            self.desired_play_state = state
            for device in self.devices.values():
                device.state_cmd_sent_at = 0.0
                device.seek_value_at_state_cmd = None
        self._enforce_play_state()

    def play(self):
        with self.lock:
            self._set_desired_play_state(PlayState.PLAYING)

    def pause(self):
        with self.lock:
            self._set_desired_play_state(PlayState.PAUSED)

    def tuning(self) -> dict:
        values = {key: getattr(self.cfg, key) for key in TUNING_LIMITS}
        # String keys: this dict goes out as JSON and comes back from the page
        values["device_offsets_ms"] = {str(k): v
                                       for k, v in sorted(self.cfg.device_offsets_ms.items())}
        return values

    def set_tuning(self, values: dict) -> dict:
        """Apply new drift-correction settings to the running controller.

        The tick loop reads these off cfg every pass, so they take effect at
        once. Pending over-threshold counts and cooldowns are cleared, as they
        were accumulated against the old thresholds and would otherwise cause
        one correction judged by the settings that no longer apply.
        """
        with self.lock:
            applied = {}
            for key, raw in values.items():
                if key == "device_offsets_ms":
                    self.cfg.device_offsets_ms = normalize_offsets(raw)
                    applied[key] = {str(k): v for k, v
                                    in sorted(self.cfg.device_offsets_ms.items())}
                    continue
                if key not in TUNING_LIMITS:
                    continue
                applied[key] = clamp_tuning(key, raw)
                setattr(self.cfg, key, applied[key])
            if applied:
                for device in self.devices.values():
                    device.over_threshold_count = 0
                    device.cooldown_until = 0.0
                    device.seek_cooldown_s = 0.0
            if "seek_lead_s" in applied:
                # A hand-set lead replaces what was learned — otherwise the
                # entered value would be silently ignored on every device that
                # had already measured itself.
                for device in self.devices.values():
                    device.seek_lead_s = None
                    device.awaiting_lead_residual = False
            return applied

    def set_rate(self, rate: float) -> float:
        with self.lock:
            self.desired_rate = clamp_rate(rate)
            for vlc_id, device in list(self.devices.items()):
                try:
                    device.vlc.set_rate(self.desired_rate)
                    device.applied_rate = self.desired_rate
                    device.nudging = False
                    device.nudge_skew = 0.0
                    device.tracker.reset()
                    device.over_threshold_count = 0
                except VlcConnectionError:
                    self._drop_device(vlc_id)
            return self.desired_rate

    def resync(self):
        with self.lock:
            master_id = self._pick_master()
            if master_id is None:
                return
            master = self.devices[master_id]
            now = time.time()
            master_pos = master.tracker.est_position(now, master.applied_rate)
            if master_pos is None or not master.length:
                return
            master_ref = master_pos - self._offset_s(master_id)
            for vlc_id, device in list(self.devices.items()):
                if vlc_id == master_id:
                    continue
                device.cooldown_until = 0.0
                device.over_threshold_count = 0
                try:
                    self._seek_correct(device, master_ref, master.length, now, vlc_id)
                except VlcConnectionError:
                    self._drop_device(vlc_id)

    def restart_playback(self):
        """Seek all connected players to 0 (does not restart the VLC process)."""
        with self.lock:
            self._seek_all(0)

    def seek_random(self) -> Optional[int]:
        """Seek all players to the same random position. Returns it, or None
        when no device reports a usable video length."""
        with self.lock:
            length = self._known_length()
            if not length:
                return None
            target = random.randrange(length)
            self._seek_all(target)
            return target

    def _known_length(self) -> Optional[int]:
        for device in self.devices.values():
            if device.length:
                return device.length
        return None

    def _seek_all(self, position: int):
        """Put every connected player at the same point of the timeline.

        Not at the same number: the players are told to jump at the same moment,
        but each takes its own time to get there and then resumes from where it
        was told, so the one with the slower decoder ends up permanently behind.
        That is where the two seconds after a random jump came from. Each device
        therefore aims ahead by as much as its own seek and display lag exceed
        the quickest device's — only the difference between them matters, and
        measuring it from the quickest keeps "start at 0" starting at 0.

        Caller holds the lock."""
        now = time.time()
        length = self._known_length()
        heads = {vlc_id: self._seek_lead(device) + self._offset_s(vlc_id)
                 for vlc_id, device in self.devices.items()}
        quickest = min(heads.values(), default=0.0)
        for vlc_id, device in list(self.devices.items()):
            try:
                self._end_nudge(device)
                target = round(position + heads[vlc_id] - quickest)
                if length:
                    target %= length
                device.vlc.seek(target)
                device.tracker.reset()
                self._note_commanded_seek(device, target)
                device.over_threshold_count = 0
                device.cooldown_until = now + self.cfg.cooldown_s
                device.last_correction_at = now
                device.last_drift = 0.0
                self._apply_play_state(device)
            except VlcConnectionError:
                self._conn_fail(vlc_id, device)

    def _video_info(self) -> dict:
        return video_ops.video_info(self.cfg.video_path)

    def video_job_busy(self) -> bool:
        return self._video_busy

    def ingest_video_stream(self, stream, activate: bool = True,
                            target_ip: Optional[str] = None) -> dict:
        """Replace video and optionally restart VLC — on all devices or one target.

        target_ip=None: replace the host file and push to all reachable peers.
        target_ip=<addr>: only that device receives the upload; the host file
        stays untouched unless the host itself is the target. This enables a
        different video per device (all videos must have the same length for
        the drift sync to make sense).
        """
        with self._video_job_lock:
            if self._video_busy:
                raise RuntimeError("video job already running")
            self._video_busy = True

        self_ip = self.cfg.ip_for_id(self.cfg.device_id)
        job = {
            "phase": "saving",
            "target": target_ip or "all",
            "started_at": time.time(),
            "finished_at": None,
            "ok": False,
            "local": None,
            "distribute": [],
            "activate": [],
            "error": None,
        }
        self.last_video_job = job
        try:
            if target_ip is None or target_ip == self_ip:
                local = video_ops.atomic_replace_from_stream(self.cfg.video_path, stream)
                job["local"] = local
                # Host row without HTTP round-trip
                host_row = {
                    "address": self_ip,
                    "id": self.cfg.device_id,
                    "ok": True,
                    "video": local,
                    "message": "local",
                    "local": True,
                }
                if target_ip is None:
                    job["phase"] = "distributing"
                    peers = video_distribute.distribute_video(
                        self.cfg, self.cfg.video_path, skip_ips=[self_ip]
                    )
                    job["distribute"] = [host_row] + peers
                else:
                    job["distribute"] = [host_row]
                if activate:
                    job["phase"] = "activating"
                    if target_ip is None:
                        job["activate"] = video_distribute.activate_playback(self.cfg)
                    else:
                        ok, message = restart_vlc_unit(self.cfg.vlc_unit)
                        job["activate"] = [{
                            "address": self_ip, "id": self.cfg.device_id,
                            "ok": ok, "message": message, "local": True,
                        }]
            else:
                # Remote-only target: spool beside the video (same filesystem),
                # push to that one agent, clean up
                tmp_path = self.cfg.video_path + ".push-tmp"
                try:
                    video_ops.atomic_replace_from_stream(tmp_path, stream)
                    job["phase"] = "distributing"
                    job["distribute"] = [
                        video_distribute.push_video_to(self.cfg, tmp_path, target_ip)
                    ]
                finally:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                if activate and all(r.get("ok") for r in job["distribute"]):
                    job["phase"] = "activating"
                    job["activate"] = [video_distribute.restart_vlc_on(self.cfg, target_ip)]

            local_ok = job["local"] is None or bool(job["local"].get("present"))
            job["ok"] = (local_ok
                         and all(r.get("ok") for r in job["distribute"])
                         and all(r.get("ok") for r in job["activate"]))
            job["phase"] = "done"
            return job
        except Exception as e:
            job["error"] = str(e)
            job["phase"] = "error"
            job["ok"] = False
            raise
        finally:
            job["finished_at"] = time.time()
            self.last_video_job = job
            with self._video_job_lock:
                self._video_busy = False

    def activate_video(self) -> dict:
        with self._video_job_lock:
            if self._video_busy:
                raise RuntimeError("video job already running")
            self._video_busy = True
        job = {
            "phase": "activating",
            "started_at": time.time(),
            "finished_at": None,
            "ok": False,
            "activate": [],
            "error": None,
        }
        self.last_video_job = job
        try:
            job["activate"] = video_distribute.activate_playback(self.cfg)
            job["ok"] = all(r.get("ok") for r in job["activate"])
            job["phase"] = "done"
            return job
        except Exception as e:
            job["error"] = str(e)
            job["phase"] = "error"
            job["ok"] = False
            raise
        finally:
            job["finished_at"] = time.time()
            self.last_video_job = job
            with self._video_job_lock:
                self._video_busy = False

    def _candidate_rows(self, connected_by_addr: Dict[str, dict]) -> List[dict]:
        rows = []
        for addr, port in self.cfg.candidate_addresses():
            key = f"{addr}:{port}"
            if key in connected_by_addr:
                rows.append(connected_by_addr[key])
                continue
            rows.append({
                "id": self.cfg.id_for_ip(addr),
                "address": key,
                "connected": False,
                "is_master": False,
                "position": None,
                "drift_ms": None,
                "play_state": None,
                "last_correction_at": None,
                "length": None,
                "offset_ms": round(self.cfg.device_offsets_ms.get(
                    self.cfg.id_for_ip(addr) or 0, 0)),
                "seek_lead_s": None,
                "seek_lead_learned": False,
                "nudge_percent": 0.0,
            })
        known = {r["address"] for r in rows}
        for addr_key, row in connected_by_addr.items():
            if addr_key not in known:
                rows.append(row)
        rows.sort(key=lambda d: (d["id"] is None, d["id"] or 0, d["address"]))
        return rows

    def _health_and_issues(
        self,
        *,
        connected: int,
        expected: int,
        devices: List[dict],
        video: dict,
        video_length: Optional[int],
    ) -> tuple[str, List[str]]:
        issues: List[str] = []
        if video.get("checked") and not video.get("present"):
            issues.append(f"Video-Datei fehlt: {video.get('path')}")
        # NOTE: no warning for unused candidate slots — the config always lists
        # max_devices slots, but installations may run with any subset of them.
        if expected > 0 and connected == 0:
            issues.append("Kein VLC-Gerät verbunden")

        if connected > 0 and not video_length:
            issues.append("Master meldet keine Videolänge (Medium fehlt in VLC?)")

        lengths = {d.get("length") for d in devices
                   if d.get("connected") and d.get("length")}
        if len(lengths) > 1:
            issues.append(
                "Geräte melden unterschiedliche Videolängen — "
                "der Sync erfordert gleich lange Videos"
            )

        bad_drift = [
            d for d in devices
            if d.get("connected") and not d.get("is_master")
            and d.get("drift_ms") is not None and abs(d["drift_ms"]) >= DRIFT_BAD_MS
        ]
        if bad_drift:
            issues.append(f"{len(bad_drift)} Gerät(e) mit Drift ≥ {DRIFT_BAD_MS} ms")
        else:
            warn_drift = [
                d for d in devices
                if d.get("connected") and not d.get("is_master")
                and d.get("drift_ms") is not None and abs(d["drift_ms"]) >= DRIFT_WARN_MS
            ]
            if warn_drift:
                issues.append(
                    f"{len(warn_drift)} Gerät(e) mit Drift ≥ {DRIFT_WARN_MS} ms"
                )

        if expected > 0 and connected == 0:
            return "offline", issues
        if issues:
            return "degraded", issues
        return "ok", []

    def status_snapshot(self) -> dict:
        with self.lock:
            # Ages are reported, not absolute timestamps: the Pi has no
            # real-time clock and no internet on its own network, so its clock
            # can be hours off from the phone showing the page. A browser
            # subtracting one from the other produced nonsense like "40 h ago"
            # on a device that had been up for six.
            now = time.time()

            def age(timestamp):
                return None if timestamp is None else max(0.0, round(now - timestamp, 1))

            master_id = self._pick_master()
            connected_by_addr: Dict[str, dict] = {}
            last_correction_at = None
            for vlc_id, device in self.devices.items():
                if device.last_correction_at is not None:
                    if last_correction_at is None or device.last_correction_at > last_correction_at:
                        last_correction_at = device.last_correction_at
                row = {
                    "id": self.cfg.id_for_ip(vlc_id.addr),
                    "address": f"{vlc_id.addr}:{vlc_id.port}",
                    "connected": True,
                    "is_master": vlc_id == master_id,
                    "position": device.last_position,
                    "drift_ms": None if device.last_drift is None else round(device.last_drift * 1000),
                    "play_state": device.play_state.value,
                    "last_correction_at": device.last_correction_at,
                    "last_correction_age_s": age(device.last_correction_at),
                    "length": device.length,
                    "offset_ms": round(self._offset_s(vlc_id) * 1000),
                    "seek_lead_s": round(self._seek_lead(device), 2),
                    "seek_lead_learned": device.seek_lead_s is not None,
                    "nudge_percent": round(device.nudge_skew * 100, 1) if device.nudging else 0.0,
                }
                connected_by_addr[row["address"]] = row

            devices = self._candidate_rows(connected_by_addr)
            master = self.devices.get(master_id) if master_id else None
            video = self._video_info()
            expected = len(self.cfg.candidate_addresses())
            connected = len(self.devices)
            video_length = master.length if master else None
            health, issues = self._health_and_issues(
                connected=connected,
                expected=expected,
                devices=devices,
                video=video,
                video_length=video_length,
            )

            return {
                "play_state": self.desired_play_state.value,
                "rate": self.desired_rate,
                "video_length": video_length,
                "master": f"{master_id.addr}:{master_id.port}" if master_id else None,
                "devices": devices,
                "health": health,
                "issues": issues,
                "expected_devices": expected,
                "connected_devices": connected,
                "video": video,
                "last_correction_at": last_correction_at,
                "last_correction_age_s": age(last_correction_at),
                "uptime_s": round(now - self.started_at, 1),
                "tuning": self.tuning(),
                "video_busy": self._video_busy,
                "last_video_job": self.last_video_job,
            }
