from __future__ import annotations

from functools import lru_cache
import re
import threading
import time
from typing import Set, List, Optional

from loguru import logger

from vlcsync.app_config import AppConfig
from vlcsync.vlc_finder import IVlcListFinder
from vlcsync.vlc_socket import VlcSocket
from vlcsync.vlc_state import PlayState, State, VlcId, PlayList, PlayListItem

VLC_IFACE_IP = "127.0.0.42"
RE_PLAYSTATE_COMPILED = re.compile(r"\( state (playing|stopped|paused) \)")
RE_PLAYLIST_ITEM = re.compile(r'\| {2}([ *])(\d+) - ')


class Vlc:
    def __init__(self, vlc_id: VlcId):
        self.vlc_id = vlc_id
        self.vlc_conn = VlcSocket(vlc_id)
        self.prev_state: State = self.cur_state()
        self.prev_volume = self.volume()

    def play_state(self) -> PlayState:
        status = self.vlc_conn.cmd("status")
        return self._extract_state(status)

    def get_seek(self) -> int | None:
        seek = self.vlc_conn.cmd("get_time")
        if seek != '':
            return int(seek)
        return None

    def playlist_goto(self, vlc_internal_index: int):
        self.vlc_conn.cmd(f"goto {vlc_internal_index}")

    def playlist(self) -> PlayList:
        cmd_resp = self.vlc_conn.cmd("playlist")
        return self._extract_playlist(cmd_resp)

    def volume(self) -> Optional[int]:
        vol = self.vlc_conn.cmd("volume")
        if vol.strip() != '':
            return int(float(vol.replace(",",'.')))
        else:
            return None

    def set_volume(self, volume: int):
        self.vlc_conn.cmd(f"volume {volume}")
        self.prev_volume = volume

    def seek(self, seek: int):
        self.vlc_conn.cmd(f"seek {seek}")

    def set_rate(self, rate: float):
        self.vlc_conn.cmd(f"rate {rate:.3f}")

    def get_length(self) -> int | None:
        length = self.vlc_conn.cmd("get_length")
        if length.strip() != '':
            return int(length)
        return None

    def stop(self):
        self.vlc_conn.cmd("stop")

    def pause(self):
        self.vlc_conn.cmd("pause")

    def play(self):
        self.vlc_conn.cmd("play")

    def cur_state(self) -> State:
        cur_seek = self.get_seek()

        return State(self.play_state(),
                     cur_seek,
                     self.playlist().active_order_index(),
                     # Abs time of video start
                     time.time() - (cur_seek or 0)
                     )

    def is_state_change(self) -> (bool, State, bool):
        cur_state: State = self.cur_state()
        prev_state: State = self.prev_state
        full_same, playlist_same = cur_state.same(prev_state)

        # Return cur_state for reduce further socket communications
        return not full_same, cur_state, not playlist_same

    def sync_to(self, new_state: State, source: Vlc, app_config: AppConfig) -> State:

        self._sync_playlist(new_state)
        self._sync_playstate(new_state)
        if not app_config.no_timestamp_sync:
            self._sync_timeline(new_state, source)

        cur_state = self.cur_state()
        self.prev_state = cur_state

        return cur_state

    def _sync_timeline(self, new_state: State, source: Vlc):
        cur_play_state = self.play_state()

        if cur_play_state == PlayState.PAUSED and source == self:
            """
            Skip sync seek with himself on pause (avoid flickering)
            As half-seconds not supported and cannot to set.
            """
            pass
        else:
            # In all other cases
            self.seek(new_state.seek)

    def _sync_playstate(self, new_state: State):
        cur_play_state: PlayState = self.play_state()
        if cur_play_state != new_state.play_state:
            if new_state.play_state == PlayState.STOPPED:
                self.stop()
            elif new_state.play_state == PlayState.PLAYING:
                if cur_play_state in [PlayState.PAUSED, PlayState.STOPPED]:
                    self.play()
            elif new_state.play_state == PlayState.PAUSED:
                if cur_play_state == PlayState.PLAYING:
                    self.pause()
            else:
                logger.warning(f"Unknown new play state {new_state.play_state} for player")

    def _sync_playlist(self, new_state: State):
        cur_playlist = self.playlist()
        if new_state.is_play_or_pause() and cur_playlist.active_order_index() != new_state.playlist_order_idx:
            if new_state.playlist_order_idx is not None and len(cur_playlist.items) > new_state.playlist_order_idx:
                self.playlist_goto(cur_playlist.items[new_state.playlist_order_idx].vlc_internal_index)
            else:
                self.stop()

    @staticmethod
    @lru_cache(maxsize=128)
    def _extract_state(status: str):
        match = RE_PLAYSTATE_COMPILED.search(status)
        return PlayState(match.group(1)) if match else PlayState.UNKNOWN

    @staticmethod
    @lru_cache(maxsize=128)
    def _extract_playlist(resp: str) -> PlayList:
        """ Playlist answer Format:
        +----[ Playlist - playlist ]
        | 1 - Плейлист
        |   6 - Video 1.mkv (00:23:37) [played 1 time]
        |  *4 - Video 2.mkv (00:23:44) [played 2 times]
        |   3 - Video 3.mkv (00:23:43) [played 1 time]
        |   5 - Video 4.mkv (00:23:44) [played 1 time]
        | 2 - Медиатека
        |   12 - Video 6.mkv (00:23:44)
        +----[ End of playlist ]
        """

        items: List[PlayListItem] = []
        active: Optional[PlayListItem] = None

        for idx, match in enumerate(re.finditer(RE_PLAYLIST_ITEM, resp)):
            item = PlayListItem(idx, match.group(2))
            items.append(item)
            if match.group(1) == "*":
                active = item

        return PlayList(items, active)

    def __repr__(self):
        return f"Vlc({self.vlc_id}, {self.prev_state=})"

    def close(self):
        self.vlc_conn.close()


class VlcProcs:
    def __init__(self, vlc_list_providers: Set[IVlcListFinder]):
        self.closed = False
        self._lock = threading.Lock()
        self._vlc_instances: dict[VlcId, Vlc] = {}
        self.vlc_list_providers = vlc_list_providers
        self.vlc_finder_thread = threading.Thread(target=self.refresh_vlc_list_periodically, daemon=True)
        self.vlc_finder_thread.start()

    def refresh_vlc_list_periodically(self):
        while not self.closed:
            start = time.time()

            vlc_candidates: Set[VlcId] = set()

            for vlc_list_provider in self.vlc_list_providers:
                vlc_list_provider: IVlcListFinder
                next_vlc_ids_list: Set[VlcId] = vlc_list_provider.get_vlc_list()

                vlc_candidates |= next_vlc_ids_list
                logger.debug(next_vlc_ids_list)

            with self._lock:
                known = set(self._vlc_instances.keys())

            # Remove missed
            for orphaned_vlc in known - vlc_candidates:
                self.dereg(orphaned_vlc)

            # Populate if not exists (connect outside the lock — can block)
            for vlc_id in vlc_candidates:
                with self._lock:
                    already = vlc_id in self._vlc_instances
                if already:
                    continue
                if vlc := self.try_connect(vlc_id):
                    with self._lock:
                        if self.closed or vlc_id in self._vlc_instances:
                            keep = False
                        else:
                            self._vlc_instances[vlc_id] = vlc
                            keep = True
                    if keep:
                        print(f"Found active instance {vlc_id}, with state {vlc.prev_state}", flush=True)
                    else:
                        vlc.close()

            logger.debug(f"Compute all_vlc (took {time.time() - start:.3f})...")
            time.sleep(5)

    @staticmethod
    def try_connect(vlc_id):
        try:
            return Vlc(vlc_id)
        except Exception as e:
            logger.opt(exception=True).debug("Cannot connect to {0}, cause: {1}", vlc_id, e)
            print(f"Cannot connect to {vlc_id} socket, cause: {e}. Skipping. Enable debug for more info. See --help. ", flush=True)
            return None

    @property
    def all_vlc(self) -> dict[VlcId, Vlc]:
        with self._lock:
            return self._vlc_instances.copy()

    def sync_all(self, state: State, source_vlc: Vlc, app_config: AppConfig):
        logger.debug(">" * 60)
        logger.debug(f"Detect change to {state} from {source_vlc.vlc_id}")
        logger.debug(f" old --> {source_vlc.prev_state} ")
        logger.debug(f" new --> {state} ")
        logger.debug(f" Time diff abs(old - new) {abs(source_vlc.prev_state.vid_start_at - state.vid_start_at)}")
        logger.debug("<" * 60)
        logger.debug("")
        print(">>> Sync players...", flush=True)

        for next_pid, next_vlc in self.all_vlc.items():
            next_vlc: Vlc
            new_state = next_vlc.sync_to(state, source_vlc, app_config)
            print(f"    Synced {next_pid} to {new_state}", flush=True)
        print()

    def dereg(self, vlc_id: VlcId):
        with self._lock:
            vlc_to_close = self._vlc_instances.pop(vlc_id, None)
        if vlc_to_close:
            print(f"Detect vlc instance closed {vlc_id}", flush=True)
            vlc_to_close.close()

    def close(self):
        self.closed = True
        with self._lock:
            instances = list(self._vlc_instances.values())
            self._vlc_instances.clear()
        for vlc in instances:
            vlc.close()

    def __del__(self):
        self.close()
