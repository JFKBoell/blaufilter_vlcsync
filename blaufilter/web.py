from __future__ import annotations

import os

from flask import Flask, jsonify, request, send_from_directory

from blaufilter.controller import Controller
from blaufilter import distribute as video_distribute

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def create_app(controller: Controller) -> Flask:
    app = Flask(__name__)
    # Large 4K uploads over Wi‑Fi — no Flask default limit
    app.config["MAX_CONTENT_LENGTH"] = None

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/api/status")
    def status():
        return jsonify(controller.status_snapshot())

    @app.post("/api/play")
    def play():
        controller.play()
        return jsonify({"ok": True})

    @app.post("/api/pause")
    def pause():
        controller.pause()
        return jsonify({"ok": True})

    @app.post("/api/rate")
    def rate():
        body = request.get_json(silent=True) or {}
        try:
            requested = float(body["rate"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "body must be {\"rate\": <number>}"}), 400
        applied = controller.set_rate(requested)
        return jsonify({"ok": True, "rate": applied})

    @app.post("/api/resync")
    def resync():
        controller.resync()
        return jsonify({"ok": True})

    @app.post("/api/restart_playback")
    def restart_playback():
        """Reset timeline to 0 on all connected players (does not restart VLC)."""
        controller.restart_playback()
        return jsonify({
            "ok": True,
            "note": (
                "Wiedergabe auf Position 0 gesetzt. "
                "Für einen echten VLC-Neustart nach Video-Tausch: "
                "Video hochladen (aktiviert VLC-Neustart) oder "
                "POST /api/video/activate."
            ),
        })

    @app.route("/api/video", methods=["POST", "PUT"])
    def upload_video():
        """Replace main.mp4 locally, push to peer agents, optionally restart VLC.

        Preferred: raw body (PUT/POST with the file as body) — avoids werkzeug's
        extra multipart spool file for multi-GB uploads. multipart/form-data
        with field 'file' stays supported for compatibility.
        """
        if controller.video_job_busy():
            return jsonify({"error": "video job already running"}), 409

        activate = request.args.get("activate", "1") not in ("0", "false", "no")

        # target: "all" (default) or a device id -> upload only to that device
        target = request.args.get("target", "all")
        target_ip = None
        if target not in ("all", ""):
            try:
                target_id = int(target)
            except ValueError:
                return jsonify({"error": "target must be 'all' or a device id"}), 400
            if not 1 <= target_id <= controller.cfg.max_devices:
                return jsonify({"error": f"target id out of range 1..{controller.cfg.max_devices}"}), 400
            target_ip = controller.cfg.ip_for_id(target_id)

        if request.mimetype == "multipart/form-data":
            upload = request.files.get("file") or request.files.get("video")
            if upload is None or upload.filename == "":
                return jsonify({"error": "multipart field 'file' required"}), 400
            stream = upload.stream
        else:
            if not request.content_length:
                return jsonify({"error": "empty body"}), 400
            stream = request.stream

        try:
            job = controller.ingest_video_stream(stream, activate=activate,
                                                 target_ip=target_ip)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 409
        except OSError as e:
            return jsonify({"error": str(e)}), 500

        status_code = 200 if job.get("ok") else 207
        return jsonify({"ok": job.get("ok"), "job": job}), status_code

    @app.post("/api/video/activate")
    def activate_video():
        if controller.video_job_busy():
            return jsonify({"error": "video job already running"}), 409
        try:
            job = controller.activate_video()
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 409
        status_code = 200 if job.get("ok") else 207
        return jsonify({"ok": job.get("ok"), "job": job}), status_code

    @app.get("/api/video/peers")
    def video_peers():
        """Probe agent fingerprints on all candidate IPs."""
        rows = video_distribute.probe_agent_videos(controller.cfg)
        host_fp = (controller.status_snapshot().get("video") or {}).get("fingerprint")
        for row in rows:
            peer_fp = (row.get("video") or {}).get("fingerprint")
            row["matches_host"] = bool(host_fp and peer_fp and peer_fp == host_fp)
        return jsonify({"host_fingerprint": host_fp, "peers": rows})

    return app
