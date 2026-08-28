import pytest

from blaufilter.config import BlaufilterConfig
from blaufilter.controller import Controller
from blaufilter.web import create_app

PIN = BlaufilterConfig.debug_pin
PIN_HEADERS = {"X-Debug-Pin": PIN}


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
    assert resp.get_json()["rate"] == 3.0
    resp = http.post("/api/rate", json={"rate": 0.01})
    assert resp.get_json()["rate"] == 0.1
    resp = http.post("/api/rate", json={"rate": 2.75})
    assert resp.get_json()["rate"] == 2.75


def test_rate_rejects_bad_body(client):
    http, _, _ = client
    assert http.post("/api/rate", json={}).status_code == 400
    assert http.post("/api/rate", json={"rate": "fast"}).status_code == 400


def test_resync_without_devices_is_noop(client):
    http, _, _ = client
    assert http.post("/api/resync", headers=PIN_HEADERS).status_code == 200


def test_restart_playback_without_devices(client):
    http, _, _ = client
    resp = http.post("/api/restart_playback", headers=PIN_HEADERS)
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_seek_random_without_length_is_rejected(client):
    http, _, _ = client
    resp = http.post("/api/seek_random")
    assert resp.status_code == 409


def test_debug_endpoints_require_pin(client):
    http, _, _ = client
    for path in ("/api/resync", "/api/restart_playback", "/api/video/activate"):
        assert http.post(path).status_code == 403, path
        assert http.post(path, headers={"X-Debug-Pin": "9999"}).status_code == 403, path
    assert http.get("/api/video/peers").status_code == 403

    # The everyday controls stay open — the guard is only for maintenance
    assert http.post("/api/pause").status_code == 200
    assert http.post("/api/rate", json={"rate": 1.0}).status_code == 200
    assert http.get("/api/status").status_code == 200


def test_debug_unlock(client):
    http, _, _ = client
    assert http.post("/api/debug/unlock", json={"pin": PIN}).status_code == 200
    assert http.post("/api/debug/unlock", json={"pin": "0000"}).status_code == 403
    assert http.post("/api/debug/unlock", json={}).status_code == 403
    assert http.get("/api/status").get_json()["debug_pin_required"] is True


def test_empty_pin_disables_the_guard(tmp_path):
    cfg = BlaufilterConfig(max_devices=0, debug_pin="", video_path=str(tmp_path / "v.mp4"))
    app = create_app(Controller(cfg, env=None))
    app.testing = True
    http = app.test_client()
    assert http.post("/api/resync").status_code == 200
    assert http.get("/api/status").get_json()["debug_pin_required"] is False


def test_captive_portal_redirects_connectivity_probes(client):
    http, _, _ = client
    for probe in ("/generate_204", "/hotspot-detect.html", "/connecttest.txt", "/beliebig"):
        resp = http.get(probe)
        assert resp.status_code == 302, probe
        assert resp.headers["Location"].endswith("/"), probe


def test_unknown_api_path_stays_json(client):
    """The portal redirect must not swallow API 404s — the UI parses JSON."""
    http, _, _ = client
    resp = http.get("/api/gibtsnicht")
    assert resp.status_code == 404
    assert resp.get_json()["error"]


def test_index_served(client):
    http, _, _ = client
    resp = http.get("/")
    assert resp.status_code == 200
    assert b"Blaufilter" in resp.data
    assert "Geräte".encode("utf-8") in resp.data
    assert "Zufallsposition".encode("utf-8") in resp.data
    assert "Wiedergabe von vorn".encode("utf-8") in resp.data
    assert "Video austauschen".encode("utf-8") in resp.data
    assert b"/api/video" in resp.data
