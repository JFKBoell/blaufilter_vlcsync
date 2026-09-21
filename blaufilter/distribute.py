"""Push video files to peer agents and orchestrate VLC restarts."""
from __future__ import annotations

import concurrent.futures
import json
import os
import urllib.error
import urllib.request
from typing import List, Optional

from blaufilter.config import BlaufilterConfig
from blaufilter.agent import restart_vlc_unit

PROBE_TIMEOUT_S = 2.5
"""Short /health probe before pushing — absent candidate IPs (the config always
lists all max_devices slots) must not count as failures or eat long timeouts."""


class AgentClient:
    def __init__(self, cfg: BlaufilterConfig, timeout_s: float = 600.0):
        self.cfg = cfg
        self.timeout_s = timeout_s

    def _url(self, addr: str, path: str) -> str:
        return f"http://{addr}:{self.cfg.agent_port}{path}"

    def health(self, addr: str, timeout: float = 5.0) -> dict:
        req = urllib.request.Request(self._url(addr, "/health"), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def is_reachable(self, addr: str, timeout: float = PROBE_TIMEOUT_S) -> bool:
        try:
            self.health(addr, timeout=timeout)
            return True
        except Exception:
            return False

    def put_video(self, addr: str, file_path: str) -> dict:
        import http.client
        size = os.path.getsize(file_path)
        conn = http.client.HTTPConnection(addr, self.cfg.agent_port, timeout=self.timeout_s)
        try:
            conn.putrequest("PUT", "/video")
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(size))
            conn.endheaders()
            with open(file_path, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    conn.send(chunk)
            resp = conn.getresponse()
            body = resp.read().decode()
            if resp.status >= 400:
                raise urllib.error.HTTPError(
                    self._url(addr, "/video"), resp.status, body, resp.headers, None
                )
            return json.loads(body)
        finally:
            conn.close()

    def restart_vlc(self, addr: str) -> dict:
        req = urllib.request.Request(
            self._url(addr, "/vlc/restart"), method="POST", data=b""
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())


def _result(addr: str, device_id: Optional[int], ok: bool, **extra) -> dict:
    row = {"address": addr, "id": device_id, "ok": ok}
    row.update(extra)
    return row


def _probe_targets(client: AgentClient, addrs: List[str]) -> dict[str, bool]:
    """Parallel short /health probe: addr -> reachable."""
    if not addrs:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(addrs))) as pool:
        futs = {pool.submit(client.is_reachable, a): a for a in addrs}
        return {futs[f]: f.result() for f in concurrent.futures.as_completed(futs)}


def _skipped(addr: str, device_id: Optional[int]) -> dict:
    return _result(addr, device_id, True, skipped=True,
                   message="offline — übersprungen")


def distribute_video(cfg: BlaufilterConfig, local_path: str,
                     skip_ips: Optional[List[str]] = None) -> List[dict]:
    """Push ``local_path`` to every *reachable* candidate agent except ``skip_ips``.

    Unreachable candidates (the config lists all max_devices slots, whether a
    device exists or not) are reported as skipped, not as failures. Pushes run
    serially: the WLAN is a shared medium, parallel transfers only add
    contention with the RC sync traffic.
    """
    skip = set(skip_ips or [])
    client = AgentClient(cfg)
    targets = [ip for ip in cfg.candidate_ips() if ip not in skip]
    reachable = _probe_targets(client, targets)
    results: List[dict] = []

    for addr in targets:
        device_id = cfg.id_for_ip(addr)
        if not reachable.get(addr):
            results.append(_skipped(addr, device_id))
            continue
        try:
            body = client.put_video(addr, local_path)
            results.append(_result(addr, device_id, True,
                                   video=body.get("video"), message="pushed"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace") if e.fp else str(e)
            results.append(_result(addr, device_id, False, error=f"HTTP {e.code}: {detail}"))
        except Exception as e:
            results.append(_result(addr, device_id, False, error=str(e)))

    results.sort(key=lambda r: (r["id"] is None, r["id"] or 0, r["address"]))
    return results


def push_video_to(cfg: BlaufilterConfig, local_path: str, addr: str) -> dict:
    """Push ``local_path`` to ONE explicitly chosen agent.

    Unlike distribute_video, an unreachable target is a real failure here —
    the user picked this device on purpose.
    """
    client = AgentClient(cfg)
    device_id = cfg.id_for_ip(addr)
    if not client.is_reachable(addr):
        return _result(addr, device_id, False, error="Gerät nicht erreichbar")
    try:
        body = client.put_video(addr, local_path)
        return _result(addr, device_id, True,
                       video=body.get("video"), message="pushed")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace") if e.fp else str(e)
        return _result(addr, device_id, False, error=f"HTTP {e.code}: {detail}")
    except Exception as e:
        return _result(addr, device_id, False, error=str(e))


def restart_vlc_on(cfg: BlaufilterConfig, addr: str) -> dict:
    """Restart VLC on ONE remote agent."""
    client = AgentClient(cfg)
    device_id = cfg.id_for_ip(addr)
    try:
        body = client.restart_vlc(addr)
        return _result(addr, device_id, bool(body.get("ok")),
                       message=body.get("message", "restarted"), local=False)
    except Exception as e:
        return _result(addr, device_id, False, error=str(e), local=False)


def activate_playback(cfg: BlaufilterConfig) -> List[dict]:
    """Restart VLC on this host and all peer agents."""
    client = AgentClient(cfg)
    results: List[dict] = []
    self_ip = cfg.ip_for_id(cfg.device_id)

    ok, message = restart_vlc_unit(cfg.vlc_unit)
    results.append(_result(self_ip, cfg.device_id, ok,
                           message=message, local=True))

    peers = [ip for ip in cfg.candidate_ips() if ip != self_ip]
    reachable = _probe_targets(client, peers)

    def remote(addr: str) -> dict:
        device_id = cfg.id_for_ip(addr)
        if not reachable.get(addr):
            row = _skipped(addr, device_id)
            row["local"] = False
            return row
        try:
            body = client.restart_vlc(addr)
            return _result(addr, device_id, bool(body.get("ok")),
                           message=body.get("message", "restarted"), local=False)
        except Exception as e:
            return _result(addr, device_id, False, error=str(e), local=False)

    if peers:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(peers))) as pool:
            futs = {pool.submit(remote, a): a for a in peers}
            for fut in concurrent.futures.as_completed(futs):
                results.append(fut.result())

    results.sort(key=lambda r: (r.get("id") is None, r.get("id") or 0, r["address"]))
    return results


def probe_agent_videos(cfg: BlaufilterConfig) -> List[dict]:
    """Fetch /health video fingerprints from all candidate agents (parallel)."""
    client = AgentClient(cfg)
    addrs = cfg.candidate_ips()

    def probe(addr: str) -> dict:
        device_id = cfg.id_for_ip(addr)
        try:
            body = client.health(addr, timeout=PROBE_TIMEOUT_S)
            return _result(addr, device_id, True, video=body.get("video"))
        except Exception as e:
            return _result(addr, device_id, False, error=str(e))

    if not addrs:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(addrs))) as pool:
        futs = {pool.submit(probe, a): a for a in addrs}
        rows = [f.result() for f in concurrent.futures.as_completed(futs)]
    rows.sort(key=lambda r: (r["id"] is None, r["id"] or 0, r["address"]))
    return rows
