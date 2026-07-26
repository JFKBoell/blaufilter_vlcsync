"""Local agent: receive video bytes and restart the user VLC unit."""
from __future__ import annotations

import os
import subprocess
from typing import Optional

from flask import Flask, jsonify, request

from blaufilter import video_ops


def restart_vlc_unit(unit: str = "blaufilter-vlc") -> tuple[bool, str]:
    """Restart the systemd --user VLC unit. Returns (ok, message)."""
    if os.environ.get("BLAUFILTER_SKIP_VLC_RESTART") == "1":
        return True, "skipped (BLAUFILTER_SKIP_VLC_RESTART=1)"
    env = os.environ.copy()
    try:
        uid = os.getuid()
    except AttributeError:
        uid = None
    if uid is not None and "XDG_RUNTIME_DIR" not in env:
        env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "restart", unit],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except FileNotFoundError:
        return False, "systemctl not found"
    except subprocess.TimeoutExpired:
        return False, "systemctl restart timed out"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        return False, err
    return True, "restarted"


def create_agent_app(video_path: str, vlc_unit: str = "blaufilter-vlc") -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health():
        info = video_ops.video_info(video_path)
        return jsonify({"ok": True, "video": info})

    @app.put("/video")
    def put_video():
        if request.content_length == 0:
            return jsonify({"error": "empty body"}), 400
        try:
            info = video_ops.atomic_replace_from_stream(video_path, request.stream)
        except OSError as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True, "video": info})

    @app.post("/vlc/restart")
    def vlc_restart():
        ok, message = restart_vlc_unit(vlc_unit)
        status = 200 if ok else 500
        return jsonify({"ok": ok, "message": message}), status

    return app
