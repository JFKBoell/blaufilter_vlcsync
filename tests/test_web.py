import pytest

from blaufilter.config import BlaufilterConfig
from blaufilter.controller import Controller
from blaufilter.web import create_app


@pytest.fixture
def client(tmp_path):
    video = tmp_path / "main.mp4"
    video.write_bytes(b"fake-video")
    cfg = BlaufilterConfig(max_devices=0, video_path=str(video))
    controller = Controller(cfg, env=None)
    app = create_app(controller)
    app.testing = True
    return app.test_client(), controller, video


def test_status(client):
    http, _controller, video = client
    resp = http.get("/api/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["play_state"] == "playing"
    assert body["rate"] == 1.0
    assert body["devices"] == []
    assert body["connected_devices"] == 0
    assert body["expected_devices"] == 0
    assert body["health"] == "ok"
    assert body["issues"] == []
    assert body["video"]["present"] is True
    assert body["video"]["path"] == str(video)
    assert body["video"]["fingerprint"]
    assert body["uptime_s"] >= 0


def test_status_reports_missing_video_and_offline_slots(tmp_path):
    missing = tmp_path / "missing.mp4"
    cfg = BlaufilterConfig(
        max_devices=2,
        subnet="10.0.0",
        video_path=str(missing),
    )
    app = create_app(Controller(cfg, env=None))
    app.testing = True
    body = app.test_client().get("/api/status").get_json()
    assert body["expected_devices"] == 2
    assert body["connected_devices"] == 0
    assert body["health"] == "offline"
    assert len(body["devices"]) == 2
    assert all(not d["connected"] for d in body["devices"])
    assert any("Video-Datei fehlt" in i for i in body["issues"])


def test_play_pause(client):
    http, _, _ = client
    assert http.post("/api/pause").status_code == 200
    assert http.get("/api/status").get_json()["play_state"] == "paused"
    assert http.post("/api/play").status_code == 200
    assert http.get("/api/status").get_json()["play_state"] == "playing"


def test_rate_is_clamped(client):
    http, _, _ = client
    resp = http.post("/api/rate", json={"rate": 5.0})
    assert resp.get_json()["rate"] == 2.0
    resp = http.post("/api/rate", json={"rate": 0.1})
    assert resp.get_json()["rate"] == 0.5
    resp = http.post("/api/rate", json={"rate": 1.25})
    assert resp.get_json()["rate"] == 1.25


def test_rate_rejects_bad_body(client):
    http, _, _ = client
    assert http.post("/api/rate", json={}).status_code == 400
    assert http.post("/api/rate", json={"rate": "fast"}).status_code == 400


def test_resync_without_devices_is_noop(client):
    http, _, _ = client
    assert http.post("/api/resync").status_code == 200


def test_restart_playback_without_devices(client):
    http, _, _ = client
    resp = http.post("/api/restart_playback")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert "VLC-Neustart" in body["note"]


def test_index_served(client):
    http, _, _ = client
    resp = http.get("/")
    assert resp.status_code == 200
    assert b"Blaufilter" in resp.data
    assert "Geräte".encode("utf-8") in resp.data
    assert "Wiedergabe von vorn".encode("utf-8") in resp.data
    assert "Video austauschen".encode("utf-8") in resp.data
    assert b"/api/video" in resp.data
