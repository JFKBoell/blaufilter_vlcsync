"""Tests for atomic video replace and agent receive/push."""
from __future__ import annotations

import io
import socket
import threading
import time

import pytest
from waitress import serve

from blaufilter.agent import create_agent_app
from blaufilter.config import BlaufilterConfig
from blaufilter.controller import Controller
from blaufilter.distribute import distribute_video, probe_agent_videos
from blaufilter import video_ops
from blaufilter.web import create_app


def test_atomic_replace_from_stream(tmp_path):
    dest = tmp_path / "main.mp4"
    dest.write_bytes(b"old")
    info = video_ops.atomic_replace_from_stream(str(dest), io.BytesIO(b"new-video-bytes"))
    assert dest.read_bytes() == b"new-video-bytes"
    assert info["present"] is True
    assert info["size_bytes"] == len(b"new-video-bytes")
    assert info["fingerprint"]
    assert not (tmp_path / "main.mp4.uploading").exists()


def test_atomic_replace_cleans_temp_on_error(tmp_path):
    dest = tmp_path / "main.mp4"

    class Boom(io.BytesIO):
        def read(self, size=-1):
            raise OSError("disk full")

    with pytest.raises(OSError):
        video_ops.atomic_replace_from_stream(str(dest), Boom(b"x"))
    assert not dest.exists()
    assert not (tmp_path / "main.mp4.uploading").exists()


def test_agent_put_video_and_health(tmp_path):
    video = tmp_path / "main.mp4"
    app = create_agent_app(str(video))
    client = app.test_client()

    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["video"]["present"] is False

    resp = client.put("/video", data=b"hello-agent", content_type="application/octet-stream")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert video.read_bytes() == b"hello-agent"
    assert body["video"]["fingerprint"]

    health = client.get("/health").get_json()
    assert health["video"]["fingerprint"] == body["video"]["fingerprint"]


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_port(port, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    pytest.fail(f"server on port {port} did not start")


def test_distribute_to_local_agent(tmp_path):
    peer_video = tmp_path / "peer" / "main.mp4"
    peer_video.parent.mkdir()
    host_video = tmp_path / "host" / "main.mp4"
    host_video.parent.mkdir()
    host_video.write_bytes(b"distributed-content")

    app = create_agent_app(str(peer_video))
    port = _free_port()
    threading.Thread(
        target=lambda: serve(app, host="127.0.0.1", port=port, threads=2),
        daemon=True,
    ).start()
    _wait_port(port)

    # Push to 127.0.0.1 (device id 1); pretend we are device 2 so self is not skipped wrongly
    cfg = BlaufilterConfig(
        device_id=2,
        max_devices=1,
        subnet="127.0.0",
        agent_port=port,
        video_path=str(host_video),
    )
    results = distribute_video(cfg, str(host_video), skip_ips=[])
    assert len(results) == 1
    assert results[0]["ok"] is True, results
    assert peer_video.read_bytes() == b"distributed-content"

    peers = probe_agent_videos(cfg)
    assert peers[0]["ok"] is True
    assert peers[0]["video"]["size_bytes"] == len(b"distributed-content")


def test_web_video_upload_local_only(tmp_path):
    dest = tmp_path / "main.mp4"
    dest.write_bytes(b"before")
    cfg = BlaufilterConfig(max_devices=0, video_path=str(dest), device_id=1)
    controller = Controller(cfg, env=None)
    app = create_app(controller)
    app.testing = True
    client = app.test_client()

    data = {
        "file": (io.BytesIO(b"uploaded-via-web"), "clip.mp4"),
    }
    resp = client.post(
        "/api/video?activate=0",
        data=data,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert dest.read_bytes() == b"uploaded-via-web"
    assert body["job"]["local"]["fingerprint"]
    assert controller.last_video_job["ok"] is True

    status = client.get("/api/status").get_json()
    assert status["video"]["size_bytes"] == len(b"uploaded-via-web")
    assert status["last_video_job"]["phase"] == "done"
