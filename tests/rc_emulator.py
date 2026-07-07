"""Fake VLC RC server for testing the blaufilter controller without real VLC.

Speaks just enough of the RC protocol (prompt ``> ``, line-based commands) to
satisfy vlcsync.vlc_socket.VlcSocket and vlcsync.vlc.Vlc.
"""
from __future__ import annotations

import socketserver
import threading
import time
from typing import List

PLAYLIST_RESPONSE = (
    "+----[ Playlist - playlist ]\r\n"
    "| 1 - Playlist\r\n"
    "|  *2 - main.mp4 (03:00:00)\r\n"
    "+----[ End of playlist ]"
)


class EmulatedPlayer:
    """In-memory player with rate, length, loop wrap and injectable clock skew."""

    def __init__(self, length: float = 3600.0, start_position: float = 0.0, playing: bool = True):
        self.length = length
        self.rate = 1.0
        self.state = "playing" if playing else "paused"
        self.volume = 256
        self._base = start_position
        self._t0 = time.time()
        self.received: List[str] = []
        self.lock = threading.Lock()

    def position(self) -> float:
        if self.state != "playing":
            return self._base
        return (self._base + (time.time() - self._t0) * self.rate) % self.length

    def _rebase(self):
        self._base = self.position()
        self._t0 = time.time()

    def seek(self, seconds: float):
        self._rebase()
        self._base = float(seconds) % self.length

    def set_rate(self, rate: float):
        self._rebase()
        self.rate = rate

    def play(self):
        self._rebase()
        self.state = "playing"

    def pause_toggle(self):
        self._rebase()
        self.state = "paused" if self.state == "playing" else "playing"

    def stop(self):
        self._rebase()
        self.state = "stopped"

    def apply_skew(self, seconds: float):
        """Shift the playback position to simulate drift."""
        self._base += seconds

    def seeks_received(self) -> List[str]:
        with self.lock:
            return [c for c in self.received if c.startswith("seek")]

    def rates_received(self) -> List[str]:
        with self.lock:
            return [c for c in self.received if c.startswith("rate")]

    def handle_command(self, line: str) -> str:
        with self.lock:
            self.received.append(line)
        cmd, _, arg = line.partition(" ")

        if cmd == "status":
            return f"( state {self.state} )"
        if cmd == "get_time":
            return str(int(self.position()))
        if cmd == "get_length":
            return str(int(self.length))
        if cmd == "playlist":
            return PLAYLIST_RESPONSE
        if cmd == "volume":
            if arg:
                self.volume = int(float(arg))
                return ""
            return str(self.volume)
        if cmd == "seek":
            self.seek(float(arg))
            return ""
        if cmd == "rate":
            self.set_rate(float(arg))
            return ""
        if cmd == "play":
            self.play()
            return ""
        if cmd == "pause":
            self.pause_toggle()
            return ""
        if cmd == "stop":
            self.stop()
            return ""
        return ""


class _RcHandler(socketserver.StreamRequestHandler):
    def handle(self):
        # vlcsync sets a 0.5s global default socket timeout on import; the RC
        # connection must survive arbitrary idle periods like real VLC does.
        self.connection.settimeout(None)
        self.wfile.write(b"> ")
        while True:
            try:
                line = self.rfile.readline()
            except OSError:
                return
            if not line:
                return
            command = line.decode().strip()
            response = self.server.player.handle_command(command)  # type: ignore[attr-defined]
            payload = f"{response}\r\n" if response else ""
            self.wfile.write(payload.encode() + b"> ")


class RcServerEmulator:
    def __init__(self, player: EmulatedPlayer, port: int = 0):
        self.player = player
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", port), _RcHandler)
        self.server.daemon_threads = True
        self.server.player = player  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
