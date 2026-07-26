from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from typing import List

CONFIG_PATH = "/etc/blaufilter/config"
DEFAULT_VIDEO_PATH = "/opt/blaufilter/video/main.mp4"

RATE_MIN = 0.5
RATE_MAX = 2.0


@dataclass
class BlaufilterConfig:
    device_id: int = 1
    role: str = "host"
    subnet: str = "192.168.4"
    rc_port: int = 4212
    max_devices: int = 6
    web_port: int = 80
    agent_port: int = 4213
    drift_threshold: float = 0.5
    hysteresis_cycles: int = 3
    cooldown_s: float = 5.0
    rate_nudge: bool = False
    video_path: str = DEFAULT_VIDEO_PATH
    """Local path of the shared loop video (checked by the web status API)."""
    vlc_unit: str = "blaufilter-vlc"
    """systemd --user unit name restarted after a video swap."""
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


def load(config_path: str = CONFIG_PATH) -> BlaufilterConfig:
    cfg = BlaufilterConfig()

    parser = configparser.ConfigParser()
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

    if env_hosts := os.environ.get("BLAUFILTER_HOSTS"):
        cfg.dev_hosts = [h.strip() for h in env_hosts.split(",") if h.strip()]
    if env_video := os.environ.get("BLAUFILTER_VIDEO"):
        cfg.video_path = env_video
    if env_agent := os.environ.get("BLAUFILTER_AGENT_PORT"):
        cfg.agent_port = int(env_agent)

    return cfg


def clamp_rate(rate: float) -> float:
    return max(RATE_MIN, min(RATE_MAX, rate))
