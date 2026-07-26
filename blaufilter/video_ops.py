"""Atomic video file replace and fingerprint helpers."""
from __future__ import annotations

import os
from typing import BinaryIO, Optional


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
    info["fingerprint"] = f"{st.st_size}:{int(st.st_mtime)}"
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
