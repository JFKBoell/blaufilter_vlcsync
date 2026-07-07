from __future__ import annotations

import socket
from typing import Set

from vlcsync.vlc_finder import IVlcListFinder
from vlcsync.vlc_state import VlcId

from blaufilter.config import BlaufilterConfig

PROBE_TIMEOUT = 0.3


class StaticCandidateFinder(IVlcListFinder):
    """Probes the fixed set of candidate addresses derived from device IDs.

    The candidate set is tiny (max 6 devices), so a plain TCP connect probe
    per rediscovery cycle is cheaper and more robust than mDNS or a subnet scan.
    """

    def __init__(self, cfg: BlaufilterConfig):
        self.cfg = cfg

    def get_vlc_list(self) -> Set[VlcId]:
        found = set()
        for addr, port in self.cfg.candidate_addresses():
            if self._is_reachable(addr, port):
                found.add(VlcId(addr, port))
        return found

    @staticmethod
    def _is_reachable(addr: str, port: int) -> bool:
        try:
            with socket.create_connection((addr, port), timeout=PROBE_TIMEOUT):
                return True
        except OSError:
            return False
