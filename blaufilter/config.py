from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from typing import List

CONFIG_PATH = "/etc/blaufilter/config"
TUNING_PATH = "/opt/blaufilter/state/tuning"
"""Sync settings changed from the web UI; values here win over CONFIG_PATH.

Not in /etc/blaufilter: that directory belongs to root, and writing a file
atomically means creating a temporary one next to it, which needs write
permission on the *directory* — owning the file alone is not enough. This is
runtime state written by the service user, so it lives under /opt/blaufilter
in a directory the installer hands to that user."""
DEFAULT_VIDEO_PATH = "/opt/blaufilter/video/main.mp4"

RATE_MIN = 0.1
RATE_MAX = 3.0

# What the web UI may change, with the range it is allowed to set.
TUNING_LIMITS = {
    "drift_threshold": (0.2, 10.0),
    "hysteresis_cycles": (1, 20),
    "cooldown_s": (1.0, 120.0),
}


@dataclass
class BlaufilterConfig:
    device_id: int = 1
    role: str = "host"
    subnet: str = "192.168.4"
    rc_port: int = 4212
    max_devices: int = 6
    web_port: int = 80
    agent_port: int = 4213
    drift_threshold: float = 3.0
    """Seek-correction threshold. Seeks visibly stall 4K HEVC decoding, so they
    are the last resort; smaller drifts are corrected smoothly via rate nudge."""
    hysteresis_cycles: int = 3
    cooldown_s: float = 10.0
    rate_nudge: bool = True
    video_path: str = DEFAULT_VIDEO_PATH
    """Local path of the shared loop video (checked by the web status API)."""
    vlc_unit: str = "blaufilter-vlc"
    """systemd --user unit name restarted after a video swap."""
    random_start: bool = True
    """Jump to a random position once playback starts after boot."""
    debug_pin: str = "1234"
    """Guards the debug endpoints. Empty string disables the check."""
    dev_hosts: List[str] = field(default_factory=list)
    """Override candidate list, e.g. ["127.0.0.1:5501", "127.0.0.1:5502"] for local dev."""

    def candidate_ips(self) -> List[str]:
        """Unique device IPs (RC port stripped) used for agent push."""
        seen = []
        for addr, _port in self.candidate_addresses():
            if addr not in seen:
                seen.append(addr)
        return seen

    def ip_for_id(self, device_id: int) -> str:
        # ID 1 is the host / AP gateway; clients get .12 .. .16
        if device_id == 1:
            return f"{self.subnet}.1"
        return f"{self.subnet}.{10 + device_id}"

    def candidate_addresses(self) -> List[tuple[str, int]]:
        if self.dev_hosts:
            result = []
            for host in self.dev_hosts:
                addr, _, port = host.partition(":")
                result.append((addr, int(port) if port else self.rc_port))
            return result
        return [(self.ip_for_id(dev_id), self.rc_port)
                for dev_id in range(1, self.max_devices + 1)]

    def id_for_ip(self, addr: str) -> int | None:
        for dev_id in range(1, self.max_devices + 1):
            if self.ip_for_id(dev_id) == addr:
                return dev_id
        return None


def clamp_tuning(key: str, value) -> float | int:
    """Coerce a tuning value into its allowed range. Raises ValueError on junk."""
    low, high = TUNING_LIMITS[key]
    number = float(value)
    number = max(low, min(high, number))
    return int(round(number)) if isinstance(low, int) else round(number, 2)


def read_tuning(tuning_path: str | None = None) -> dict:
    # Resolved at call time, not bound as a default: the path is a module
    # constant that tests replace.
    tuning_path = tuning_path or TUNING_PATH
    parser = configparser.ConfigParser(interpolation=None)
    if not (os.path.exists(tuning_path) and parser.read(tuning_path)
            and parser.has_section("blaufilter")):
        return {}
    section = parser["blaufilter"]
    values = {}
    for key in TUNING_LIMITS:
        if key in section:
            try:
                values[key] = clamp_tuning(key, section[key])
            except ValueError:
                continue
    return values


def write_tuning(values: dict, tuning_path: str | None = None) -> None:
    """Persist the web UI's sync settings. Raises OSError when the filesystem
    is read-only — which it is whenever the write protection is enabled."""
    tuning_path = tuning_path or TUNING_PATH
    parser = configparser.ConfigParser(interpolation=None)
    parser["blaufilter"] = {k: str(v) for k, v in values.items()}
    directory = os.path.dirname(tuning_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = tuning_path + ".tmp"
    with open(tmp, "w") as handle:
        parser.write(handle)
    os.replace(tmp, tuning_path)


def load(config_path: str = CONFIG_PATH,
         tuning_path: str | None = None) -> BlaufilterConfig:
    cfg = BlaufilterConfig()

    # interpolation off: values like an SSID containing '%' must not be parsed
    parser = configparser.ConfigParser(interpolation=None)
    if os.path.exists(config_path) and parser.read(config_path) and parser.has_section("blaufilter"):
        section = parser["blaufilter"]
        cfg.device_id = section.getint("device_id", cfg.device_id)
        cfg.role = section.get("role", cfg.role)
        cfg.subnet = section.get("subnet", cfg.subnet)
        cfg.rc_port = section.getint("rc_port", cfg.rc_port)
        cfg.max_devices = section.getint("max_devices", cfg.max_devices)
        cfg.web_port = section.getint("web_port", cfg.web_port)
        cfg.agent_port = section.getint("agent_port", cfg.agent_port)
        cfg.drift_threshold = section.getfloat("drift_threshold", cfg.drift_threshold)
        cfg.hysteresis_cycles = section.getint("hysteresis_cycles", cfg.hysteresis_cycles)
        cfg.cooldown_s = section.getfloat("cooldown_s", cfg.cooldown_s)
        cfg.rate_nudge = section.getboolean("rate_nudge", cfg.rate_nudge)
        cfg.video_path = section.get("video_path", cfg.video_path)
        cfg.vlc_unit = section.get("vlc_unit", cfg.vlc_unit)
        cfg.random_start = section.getboolean("random_start", cfg.random_start)
        cfg.debug_pin = section.get("debug_pin", cfg.debug_pin).strip()

    # Applied last: what was changed at runtime beats the installed defaults
    for key, value in read_tuning(tuning_path).items():
        setattr(cfg, key, value)

    if env_hosts := os.environ.get("BLAUFILTER_HOSTS"):
        cfg.dev_hosts = [h.strip() for h in env_hosts.split(",") if h.strip()]
    if env_video := os.environ.get("BLAUFILTER_VIDEO"):
        cfg.video_path = env_video
    if env_agent := os.environ.get("BLAUFILTER_AGENT_PORT"):
        cfg.agent_port = int(env_agent)

    return cfg


def clamp_rate(rate: float) -> float:
    return max(RATE_MIN, min(RATE_MAX, rate))
