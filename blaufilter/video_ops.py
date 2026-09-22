"""Atomic video file replace and fingerprint helpers."""
from __future__ import annotations

import hashlib
import os
from typing import BinaryIO, Optional

SAMPLE_BYTES = 1 << 20
"""How much of the head and tail goes into the fingerprint."""

_FP_CACHE: dict = {}
"""path -> ((size, mtime_ns), fingerprint). The status API is polled once a
second; without this, every poll would read megabytes off the SD card."""


def content_fingerprint(path: str, size: int, mtime_ns: int) -> Optional[str]:
    """Identify a video by its content, not by when it was written.

    The same file pushed to several devices gets a different mtime on each of
    them, so a size+mtime fingerprint could never match across devices — which
    is exactly what has to be compared to see which video sits where. Hashing
    the size plus the first and last megabyte distinguishes different videos
    reliably (they differ in their headers and their tail) and stays fast even
    for a multi-gigabyte file.
    """
    cached = _FP_CACHE.get(path)
    key = (size, mtime_ns)
    if cached and cached[0] == key:
        return cached[1]

    digest = hashlib.blake2b(digest_size=8)
    digest.update(str(size).encode())
    try:
        with open(path, "rb") as fh:
            digest.update(fh.read(SAMPLE_BYTES))
            if size > 2 * SAMPLE_BYTES:
                fh.seek(-SAMPLE_BYTES, os.SEEK_END)
                digest.update(fh.read(SAMPLE_BYTES))
    except OSError:
        return None

    fingerprint = digest.hexdigest()
    _FP_CACHE[path] = (key, fingerprint)
    return fingerprint


def video_info(path: Optional[str]) -> dict:
    info = {
        "path": path or None,
        "present": True,
        "size_bytes": None,
        "mtime": None,
        "fingerprint": None,
        "checked": bool(path),
    }
    if not path:
        return info
    try:
        st = os.stat(path)
    except OSError:
        info["present"] = False
        return info
    info["present"] = True
    info["size_bytes"] = st.st_size
    info["mtime"] = st.st_mtime
    info["fingerprint"] = content_fingerprint(path, st.st_size, st.st_mtime_ns)
    return info


def atomic_replace_from_stream(dest_path: str, stream: BinaryIO, chunk_size: int = 1024 * 1024) -> dict:
    """Write stream to ``dest_path.uploading`` then atomically replace ``dest_path``.

    Returns video_info() for the final file.
    """
    if not dest_path:
        raise ValueError("dest_path is required")
    parent = os.path.dirname(dest_path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_path = dest_path + ".uploading"
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_path, dest_path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise
    return video_info(dest_path)


def atomic_replace_from_path(dest_path: str, source_path: str) -> dict:
    """Copy ``source_path`` into place via temp file + replace."""
    with open(source_path, "rb") as src:
        return atomic_replace_from_stream(dest_path, src)
