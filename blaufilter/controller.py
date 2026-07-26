from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from loguru import logger

from vlcsync.vlc import Vlc, VlcProcs
from vlcsync.vlc_socket import VlcConnectionError
from vlcsync.vlc_state import PlayState, VlcId

from blaufilter.config import BlaufilterConfig, clamp_rate
from blaufilter.tracker import PositionTracker, modular_diff
from blaufilter import video_ops
from blaufilter import distribute as video_distribute

TICK_INTERVAL = 0.1
PLAYSTATE_POLL_EVERY_N_TICKS = 10
LOOP_BOUNDARY_GRACE_S = 3.0
NUDGE_FACTOR = 0.03
NUDGE_MIN_DRIFT = 0.15
NUDGE_DONE_DRIFT = 0.05
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
        self._poll_positions()
        if self._tick_count % PLAYSTATE_POLL_EVERY_N_TICKS == 0:
            self._enforce_play_state()
        self._correct_drift()

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

    def _poll_positions(self):
        now = time.time()
        for vlc_id, device in list(self.devices.items()):
            try:
                device.tracker.observe(now, device.vlc.get_seek())
            except VlcConnectionError:
                self._drop_device(vlc_id)
                continue
            device.last_position = device.tracker.est_position(now, device.applied_rate)

    def _enforce_play_state(self):
        for vlc_id, device in list(self.devices.items()):
            try:
                self._apply_play_state(device)
            except VlcConnectionError:
                self._drop_device(vlc_id)

    def _apply_play_state(self, device: DeviceView):
        device.play_state = device.vlc.play_state()
        if device.play_state == self.desired_play_state:
            return
        if self.desired_play_state == PlayState.PLAYING:
            if device.play_state in (PlayState.PAUSED, PlayState.STOPPED):
                device.vlc.play()
                device.tracker.reset()
                device.play_state = PlayState.PLAYING
        elif self.desired_play_state == PlayState.PAUSED:
            if device.play_state == PlayState.PLAYING:
                device.vlc.pause()  # RC "pause" toggles; only send when playing
                device.tracker.reset()
                device.play_state = PlayState.PAUSED

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

        for vlc_id, device in list(self.devices.items()):
            if vlc_id == master_id:
                continue
            slave_pos = device.tracker.est_position(now, device.applied_rate)
            if slave_pos is None:
                device.last_drift = None
                continue

            drift = modular_diff(slave_pos, master_pos, length)
            device.last_drift = drift

            if abs(drift) > self.cfg.drift_threshold:
                device.over_threshold_count += 1
            else:
                device.over_threshold_count = 0

            try:
                if (device.over_threshold_count >= self.cfg.hysteresis_cycles
                        and now >= device.cooldown_until):
                    self._seek_correct(device, master_pos, length, now)
                elif self.cfg.rate_nudge:
                    self._rate_nudge(device, drift)
            except VlcConnectionError:
                self._drop_device(vlc_id)

    def _seek_correct(self, device: DeviceView, master_pos: float, length: int, now: float):
        target = round(master_pos) % length
        print(f"Drift correction: seek {device.vlc.vlc_id} to {target}s "
              f"(drift {device.last_drift:+.2f}s)", flush=True)
        if device.nudging:
            device.vlc.set_rate(self.desired_rate)
            device.applied_rate = self.desired_rate
            device.nudging = False
        device.vlc.seek(target)
        device.tracker.reset()
        device.over_threshold_count = 0
        device.cooldown_until = now + self.cfg.cooldown_s
        device.last_correction_at = now

    def _rate_nudge(self, device: DeviceView, drift: float):
        """Smoothly pull a slightly-off device back by temporarily skewing its rate."""
        if device.nudging:
            if abs(drift) < NUDGE_DONE_DRIFT:
                device.vlc.set_rate(self.desired_rate)
                device.applied_rate = self.desired_rate
                device.nudging = False
        elif NUDGE_MIN_DRIFT < abs(drift) <= self.cfg.drift_threshold:
            # Ahead (drift > 0) -> slow down; behind -> speed up
            factor = 1 - NUDGE_FACTOR if drift > 0 else 1 + NUDGE_FACTOR
            nudge_rate = self.desired_rate * factor
            device.vlc.set_rate(nudge_rate)
            device.applied_rate = nudge_rate
            device.nudging = True

    # ------------------------------------------------------------ web-facing

    def play(self):
        with self.lock:
            self.desired_play_state = PlayState.PLAYING
            self._enforce_play_state()

    def pause(self):
        with self.lock:
            self.desired_play_state = PlayState.PAUSED
            self._enforce_play_state()

    def set_rate(self, rate: float) -> float:
        with self.lock:
            self.desired_rate = clamp_rate(rate)
            for vlc_id, device in list(self.devices.items()):
                try:
                    device.vlc.set_rate(self.desired_rate)
                    device.applied_rate = self.desired_rate
                    device.nudging = False
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
            for vlc_id, device in list(self.devices.items()):
                if vlc_id == master_id:
                    continue
                device.cooldown_until = 0.0
                device.over_threshold_count = 0
                try:
                    self._seek_correct(device, master_pos, master.length, now)
                except VlcConnectionError:
                    self._drop_device(vlc_id)

    def restart_playback(self):
        """Seek all connected players to 0 and apply the desired play state.

        This does not restart the VLC process — only resets the timeline.
        """
        with self.lock:
            now = time.time()
            for vlc_id, device in list(self.devices.items()):
                try:
                    if device.nudging:
                        device.vlc.set_rate(self.desired_rate)
                        device.applied_rate = self.desired_rate
                        device.nudging = False
                    device.vlc.seek(0)
                    device.tracker.reset()
                    device.over_threshold_count = 0
                    device.cooldown_until = now + self.cfg.cooldown_s
                    device.last_correction_at = now
                    device.last_drift = 0.0
                    self._apply_play_state(device)
                except VlcConnectionError:
                    self._drop_device(vlc_id)

    def _video_info(self) -> dict:
        return video_ops.video_info(self.cfg.video_path)

    def video_job_busy(self) -> bool:
        return self._video_busy

    def ingest_video_stream(self, stream, activate: bool = True) -> dict:
        """Replace local video, push to peer agents, optionally restart VLC everywhere."""
        with self._video_job_lock:
            if self._video_busy:
                raise RuntimeError("video job already running")
            self._video_busy = True

        job = {
            "phase": "saving",
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
            local = video_ops.atomic_replace_from_stream(self.cfg.video_path, stream)
            job["local"] = local
            job["phase"] = "distributing"
            self_ip = self.cfg.ip_for_id(self.cfg.device_id)
            # Always record host as local success without HTTP round-trip
            host_row = {
                "address": self_ip,
                "id": self.cfg.device_id,
                "ok": True,
                "video": local,
                "message": "local",
                "local": True,
            }
            peers = video_distribute.distribute_video(
                self.cfg, self.cfg.video_path, skip_ips=[self_ip]
            )
            job["distribute"] = [host_row] + peers
            if activate:
                job["phase"] = "activating"
                job["activate"] = video_distribute.activate_playback(self.cfg)
            job["ok"] = bool(local.get("present")) and all(
                r.get("ok") for r in job["distribute"]
            ) and (not activate or all(r.get("ok") for r in job["activate"]))
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
        if expected > 0 and connected == 0:
            issues.append("Kein VLC-Gerät verbunden")
        elif expected > 0 and connected < expected:
            issues.append(f"Nur {connected} von {expected} erwarteten Geräten online")

        if connected > 0 and not video_length:
            issues.append("Master meldet keine Videolänge (Medium fehlt in VLC?)")

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
                    "length": device.length,
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
                "uptime_s": round(time.time() - self.started_at, 1),
                "video_busy": self._video_busy,
                "last_video_job": self.last_video_job,
            }
