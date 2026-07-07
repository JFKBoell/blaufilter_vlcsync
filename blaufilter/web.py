from __future__ import annotations

import os

from flask import Flask, jsonify, request, send_from_directory

from blaufilter.controller import Controller

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def create_app(controller: Controller) -> Flask:
    app = Flask(__name__)

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

    return app
