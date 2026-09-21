from __future__ import annotations

import socket
import threading
import time

from loguru import logger

from vlcsync.vlc_state import VlcId

VLC_PROMPT = b"> "
# Per-socket timeouts (do NOT use socket.setdefaulttimeout — it leaks into Flask etc.)
# Recv values are sized for loaded 2.4GHz WiFi: latency spikes of a second are
# normal there and must not tear down the connection.
CONNECT_TIMEOUT_S = 0.5
RECV_TIMEOUT_S = 1.5
RECV_DEADLINE_S = 2.0


class VlcSocket:
    def __init__(self, vlc_id: VlcId):
        self.vlc_id = vlc_id
        self._lock = threading.Lock()
        self._closed = False
        self.sock: socket.socket | None = None
        logger.trace("Connect {0}", vlc_id)
        try:
            sock = socket.create_connection(
                (vlc_id.addr, vlc_id.port), timeout=CONNECT_TIMEOUT_S
            )
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(RECV_TIMEOUT_S)
            self.sock = sock
            self._recv_answer()
        except Exception:
            self.close()
            raise

    def cmd(self, command: str) -> str:
        """Send one RC command and read the reply. Serialized per connection."""
        with self._lock:
            if self._closed or self.sock is None:
                raise VlcConnectionError("Socket already closed", self.vlc_id)
            self._drain_stale()
            logger.trace(f">>> Send {command=} to {self.vlc_id}")
            try:
                self.sock.sendall(f"{command}\r\n".encode())
            except OSError as e:
                if self._closed:
                    raise VlcConnectionError("Socket already closed", self.vlc_id) from e
                raise VlcConnectionError("Socket lost connection", self.vlc_id) from e
            data = self._recv_answer()
            answer = data.decode().replace("> ", "").replace("\r\n", "")
            logger.trace(f"<<< Receive {answer=} from {self.vlc_id}")
            return answer

    def _drain_stale(self):
        """Discard a late reply left over from a previously timed-out command.

        Keeps command/response pairing intact, so a single recv timeout does
        not force tearing down the connection.
        """
        try:
            self.sock.setblocking(False)
            while True:
                stale = self.sock.recv(4096)
                if not stale:
                    raise VlcConnectionError("Socket lost connection", self.vlc_id)
                logger.trace(f"Drained {len(stale)} stale bytes from {self.vlc_id}")
        except (BlockingIOError, InterruptedError):
            pass
        except VlcConnectionError:
            raise
        except OSError as e:
            raise VlcConnectionError("Unexpected socket error.", self.vlc_id) from e
        finally:
            try:
                self.sock.settimeout(RECV_TIMEOUT_S)
            except OSError:
                pass

    def _recv_answer(self):
        if self.sock is None:
            raise VlcConnectionError("Socket already closed", self.vlc_id)
        data = b''
        try:
            deadline = time.time() + RECV_DEADLINE_S
            while data[-2:] != VLC_PROMPT:
                if time.time() > deadline:
                    raise TimeoutError()
                chunk = self.sock.recv(1024)
                if not chunk:
                    raise VlcConnectionError("Socket lost connection", self.vlc_id)
                data += chunk

        except VlcConnectionError:
            raise
        except ConnectionAbortedError as e:
            raise VlcConnectionError("Socket lost connection", self.vlc_id) from e
        except (socket.timeout, TimeoutError) as e:
            logger.trace(f"Data when timeout {data}")
            raise VlcConnectionError("Socket receive answer native timeout.", self.vlc_id) from e
        except OSError as e:
            if self._closed:
                raise VlcConnectionError("Socket already closed", self.vlc_id) from e
            raise VlcConnectionError("Unexpected socket error.", self.vlc_id) from e

        return data

    def close(self):
        """Mark closed and shut the fd so a blocked recv in cmd() unblocks.

        Intentionally does not take ``_lock``: ``cmd()`` holds the lock while
        blocked in ``recv``, and we must close the fd to wake it.
        """
        self._closed = True
        sock = self.sock
        self.sock = None
        logger.trace("Close socket {0}...", self.vlc_id)
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


class VlcConnectionError(TimeoutError):
    def __init__(self, msg: str, vlc_id: VlcId):
        super().__init__(msg)
        self.vlc_id = vlc_id
