import pytest

from blaufilter.config import BlaufilterConfig
from blaufilter.controller import Controller
from blaufilter.web import create_app


@pytest.fixture
def client():
    controller = Controller(BlaufilterConfig(), env=None)
    app = create_app(controller)
    app.testing = True
    return app.test_client()


def test_status(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["play_state"] == "playing"
    assert body["rate"] == 1.0
    assert body["devices"] == []


def test_play_pause(client):
    assert client.post("/api/pause").status_code == 200
    assert client.get("/api/status").get_json()["play_state"] == "paused"
    assert client.post("/api/play").status_code == 200
    assert client.get("/api/status").get_json()["play_state"] == "playing"


def test_rate_is_clamped(client):
    resp = client.post("/api/rate", json={"rate": 5.0})
    assert resp.get_json()["rate"] == 2.0
    resp = client.post("/api/rate", json={"rate": 0.1})
    assert resp.get_json()["rate"] == 0.5
    resp = client.post("/api/rate", json={"rate": 1.25})
    assert resp.get_json()["rate"] == 1.25


def test_rate_rejects_bad_body(client):
    assert client.post("/api/rate", json={}).status_code == 400
    assert client.post("/api/rate", json={"rate": "fast"}).status_code == 400


def test_resync_without_devices_is_noop(client):
    assert client.post("/api/resync").status_code == 200


def test_index_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Blaufilter" in resp.data
