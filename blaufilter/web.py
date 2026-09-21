from __future__ import annotations

import functools
import hmac
import os

from flask import Flask, jsonify, redirect, request, send_from_directory

from blaufilter.controller import Controller
from blaufilter import distribute as video_distribute

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
PIN_HEADER = "X-Debug-Pin"


def create_app(controller: Controller) -> Flask:
    app = Flask(__name__)
    # Large 4K uploads over Wi‑Fi — no Flask default limit
    app.config["MAX_CONTENT_LENGTH"] = None

    def pin_ok(candidate: str) -> bool:
        expected = controller.cfg.debug_pin
        if not expected:
            return True
        return hmac.compare_digest(str(candidate or ""), expected)

    def require_pin(view):
        """Guard for the maintenance endpoints reachable from the debug page.

        This keeps the destructive controls out of reach of anyone who just
        joined the WiFi — it is not transport security (plain HTTP on a closed
        network), so keep the PIN out of anything internet-facing.
        """
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            if not pin_ok(request.headers.get(PIN_HEADER)):
                return jsonify({"error": "PIN erforderlich"}), 403
            return view(*args, **kwargs)
        return wrapper

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.errorhandler(404)
    def captive_portal(_error):
        """Send every unknown path to the control page.

        This is what makes the network a captive portal: phones probe a
        vendor URL after joining (/generate_204 on Android,
        /hotspot-detect.html on Apple, /connecttest.txt on Windows). None of
        them are routes here, so they land in this handler, and a redirect
        instead of the expected reply is exactly the signal that makes the
        device pop up the page by itself. Paired with the wildcard DNS entry
        in NetworkManager's dnsmasq config, which points every name at us.
        """
        if request.path.startswith("/api/"):
            return jsonify({"error": "not found"}), 404
        return redirect("/", code=302)

    @app.get("/api/status")
    def status():
        snapshot = controller.status_snapshot()
        snapshot["debug_pin_required"] = bool(controller.cfg.debug_pin)
        return jsonify(snapshot)

    @app.post("/api/debug/unlock")
    def debug_unlock():
        body = request.get_json(silent=True) or {}
        if not pin_ok(body.get("pin")):
            return jsonify({"ok": False, "error": "PIN falsch"}), 403
        return jsonify({"ok": True})

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

    @app.post("/api/seek_random")
    def seek_random():
        """Jump all players to the same random position."""
        position = controller.seek_random()
        if position is None:
            return jsonify({"error": "keine Videolänge bekannt"}), 409
        return jsonify({"ok": True, "position": position})

    @app.post("/api/resync")
    @require_pin
    def resync():
        controller.resync()
        return jsonify({"ok": True})

    @app.post("/api/restart_playback")
    @require_pin
    def restart_playback():
        """Reset timeline to 0 on all connected players (does not restart VLC)."""
        controller.restart_playback()
        return jsonify({"ok": True})

    @app.route("/api/video", methods=["POST", "PUT"])
    @require_pin
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
    @require_pin
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
    @require_pin
    def video_peers():
        """Probe agent fingerprints on all candidate IPs."""
        rows = video_distribute.probe_agent_videos(controller.cfg)
        host_fp = (controller.status_snapshot().get("video") or {}).get("fingerprint")
        for row in rows:
            peer_fp = (row.get("video") or {}).get("fingerprint")
            row["matches_host"] = bool(host_fp and peer_fp and peer_fp == host_fp)
        return jsonify({"host_fingerprint": host_fp, "peers": rows})

    return app
