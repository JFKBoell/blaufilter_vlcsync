"""Drift-correction settings changed from the web UI."""
from __future__ import annotations

import errno
import os

import pytest

from blaufilter import config as bf_config
from blaufilter.config import BlaufilterConfig
from blaufilter.controller import Controller
from blaufilter.web import create_app

PIN_HEADERS = {"X-Debug-Pin": BlaufilterConfig.debug_pin}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(bf_config, "TUNING_PATH", str(tmp_path / "tuning"))
    cfg = BlaufilterConfig(max_devices=0, video_path=str(tmp_path / "v.mp4"))
    controller = Controller(cfg, env=None)
    app = create_app(controller)
    app.testing = True
    return app.test_client(), controller, tmp_path


def test_status_exposes_current_settings(client):
    http, controller, _ = client
    tuning = http.get("/api/status").get_json()["tuning"]
    assert tuning["drift_threshold"] == controller.cfg.drift_threshold
    assert tuning["hysteresis_cycles"] == controller.cfg.hysteresis_cycles
    assert tuning["cooldown_s"] == controller.cfg.cooldown_s


def test_change_takes_effect_and_is_persisted(client):
    http, controller, tmp_path = client
    resp = http.post("/api/tuning", headers=PIN_HEADERS,
                     json={"drift_threshold": 1.5, "cooldown_s": 30, "hysteresis_cycles": 5})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["saved"] is True

    # live on the running controller...
    assert controller.cfg.drift_threshold == 1.5
    assert controller.cfg.cooldown_s == 30
    assert controller.cfg.hysteresis_cycles == 5
    # ...and in the file the controller reads at startup
    assert bf_config.read_tuning(str(tmp_path / "tuning"))["drift_threshold"] == 1.5


def test_values_outside_the_range_are_clamped_not_rejected(client):
    http, controller, _ = client
    body = http.post("/api/tuning", headers=PIN_HEADERS,
                     json={"drift_threshold": 999, "cooldown_s": 0}).get_json()
    assert body["tuning"]["drift_threshold"] == bf_config.TUNING_LIMITS["drift_threshold"][1]
    assert body["tuning"]["cooldown_s"] == bf_config.TUNING_LIMITS["cooldown_s"][0]
    assert controller.cfg.drift_threshold == bf_config.TUNING_LIMITS["drift_threshold"][1]


def test_junk_is_rejected(client):
    http, _, _ = client
    assert http.post("/api/tuning", headers=PIN_HEADERS,
                     json={"drift_threshold": "schnell"}).status_code == 400
    assert http.post("/api/tuning", headers=PIN_HEADERS, json={}).status_code == 400
    # unknown keys must not silently create attributes
    assert http.post("/api/tuning", headers=PIN_HEADERS,
                     json={"web_port": 8080}).status_code == 400


def test_changing_settings_needs_the_pin(client):
    http, controller, _ = client
    before = controller.cfg.drift_threshold
    assert http.post("/api/tuning", json={"drift_threshold": 1.0}).status_code == 403
    assert controller.cfg.drift_threshold == before
    # reading them does not
    assert http.get("/api/tuning").status_code == 200


def test_read_only_filesystem_still_applies_the_change(client, monkeypatch):
    http, controller, _ = client

    def refuse(*_args, **_kwargs):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(bf_config, "write_tuning", refuse)
    body = http.post("/api/tuning", headers=PIN_HEADERS,
                     json={"drift_threshold": 2.0}).get_json()
    assert body["saved"] is False
    assert "schreibgeschützt" in body["note"]
    assert controller.cfg.drift_threshold == 2.0, "muss trotzdem sofort gelten"


def test_missing_permission_is_not_blamed_on_the_write_protection(client, monkeypatch):
    """The first version told users the write protection was on whenever
    saving failed — it was a permission problem on the directory."""
    http, controller, _ = client

    def refuse(*_args, **_kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(bf_config, "write_tuning", refuse)
    body = http.post("/api/tuning", headers=PIN_HEADERS,
                     json={"drift_threshold": 2.0}).get_json()
    assert body["saved"] is False
    assert "Schreibrechte" in body["note"]
    assert "Schreibschutz" not in body["note"], "darf nicht den Schreibschutz beschuldigen"
    assert controller.cfg.drift_threshold == 2.0


@pytest.mark.skipif(os.geteuid() == 0,
                    reason="root ignoriert Verzeichnisrechte — der Fall ist so nicht prüfbar")
def test_writing_needs_a_writable_directory_not_just_the_file(tmp_path):
    """The bug from the device: the file belonged to the service user, but the
    directory did not — and an atomic write creates a temporary file in it."""
    directory = tmp_path / "root-owned"
    directory.mkdir()
    target = directory / "tuning"
    target.write_text("[blaufilter]\n")
    target.chmod(0o666)      # die Datei selbst wäre beschreibbar
    directory.chmod(0o555)   # das Verzeichnis nicht

    try:
        with pytest.raises(OSError):
            bf_config.write_tuning({"drift_threshold": 1.0}, str(target))
    finally:
        directory.chmod(0o755)


def test_writing_works_in_a_directory_the_service_user_owns(tmp_path):
    state = tmp_path / "state"
    target = state / "tuning"
    bf_config.write_tuning({"drift_threshold": 1.25}, str(target))
    assert bf_config.read_tuning(str(target))["drift_threshold"] == 1.25
    assert not (state / "tuning.tmp").exists(), "keine Reste"


def test_tuning_file_overrides_the_installed_config(tmp_path):
    config_path = tmp_path / "config"
    tuning_path = tmp_path / "tuning"
    config_path.write_text("[blaufilter]\ndrift_threshold = 3.0\ncooldown_s = 10\n")
    tuning_path.write_text("[blaufilter]\ndrift_threshold = 0.8\n")

    cfg = bf_config.load(str(config_path), str(tuning_path))
    assert cfg.drift_threshold == 0.8, "geänderter Wert gewinnt"
    assert cfg.cooldown_s == 10, "ungeänderte Werte bleiben aus der Installation"


def test_broken_tuning_file_is_ignored(tmp_path):
    tuning_path = tmp_path / "tuning"
    tuning_path.write_text("[blaufilter]\ndrift_threshold = keineZahl\n")
    cfg = bf_config.load(str(tmp_path / "missing"), str(tuning_path))
    assert cfg.drift_threshold == BlaufilterConfig.drift_threshold
