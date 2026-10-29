# coding:utf8
"""Pure, thread-safe status model for the mpv JSON-IPC adapter.

Stdlib + syncplay.constants only: no Twisted, no threads spawned, no mpv
imports — so every transition is unit-testable with an injected clock.

Threading contract: writes happen on the vendor lib's single EventHandler
thread, reads (``snapshot``/``calculated_position``) happen on the Twisted
reactor from ``askForStatus``. A single ``threading.Lock`` guards every
mutation and every composed read — ``frozen=True`` on the dataclass alone
would not prevent torn multi-field snapshots.

Wall-clock parity: the default clock is ``time.time`` (not monotonic)
because these stamps are compared against player-side timestamps that use
``time.time`` too (``lastResetTime`` set in ``openFile``), and the original
``getCalculatedPosition`` dead-reckoned against ``time.time()``.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, Dict, List, Optional, Tuple

from syncplay import constants

#: Properties observed over JSON IPC (bind_property_observer).
OBSERVED_PROPERTIES = (
    "pause",
    "time-pos",
    "duration",
    "path",
    "media-title",
    "filename",
    "speed",
    "eof-reached",
)

# mpv event names consumed by the adapter (bind_event).
EVENT_START_FILE = "start-file"
EVENT_FILE_LOADED = "file-loaded"
EVENT_QUIT = "quit"


class Signal(Enum):
    """Side effects the adapter must run on the reactor thread."""
    FILE_LOAD_START = auto()
    FILE_LOAD_DONE = auto()
    EOF = auto()
    QUIT = auto()


@dataclass(frozen=True)
class StatusSnapshot:
    paused: Optional[bool]
    position: Optional[float]
    last_position_time: Optional[float]
    duration: Optional[float]
    path: Optional[str]
    media_title: Optional[str]
    filename: Optional[str]
    speed: Optional[float]
    eof_reached: bool
    file_loaded: bool
    last_loaded_time: Optional[float]
    ready: bool


class MpvStatusCache:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._paused = None  # type: Optional[bool]
        self._position = None  # type: Optional[float]
        self._last_position_time = None  # type: Optional[float]
        self._duration = None  # type: Optional[float]
        self._path = None  # type: Optional[str]
        self._media_title = None  # type: Optional[str]
        self._filename = None  # type: Optional[str]
        self._speed = None  # type: Optional[float]
        self._eof_reached = False
        self._file_loaded = False
        self._last_loaded_time = None  # type: Optional[float]
        # Sticky per-process readiness: pause and time-pos must both have been
        # observed at least once. Deliberately NOT cleared per file — mpv does
        # not re-emit a `pause` property-change when the value is unchanged
        # across a playlist advance, so resetting it would leave `ready` False
        # forever after the first file.
        self._saw_pause = False
        self._saw_time_pos = False

    # ------------------------------------------------------------------ write

    def on_property(self, name, data, recently_reset=False,
                    global_position=0.0, global_paused=False) -> List[Signal]:
        """Fold one property-change into the cache.

        ``recently_reset``/``global_position``/``global_paused`` are external
        context supplied by the player: ``_recentlyReset()`` uses the player's
        ``lastResetTime`` (set in ``openFile``) and the global values come
        from the client — neither is derivable from mpv samples alone. They
        only influence the ``time-pos`` and ``pause`` gates.
        """
        with self._lock:
            if name == "time-pos":
                if data is None:
                    return []
                self._store_position(float(data), recently_reset, global_position)
                self._saw_time_pos = True
            elif name == "pause":
                if data is None:
                    return []
                self._store_pause_state(bool(data), global_paused)
                self._saw_pause = True
            elif name == "duration":
                if data is not None:
                    self._duration = float(data)
            elif name == "path":
                self._path = data
            elif name == "media-title":
                self._media_title = data
            elif name == "filename":
                self._filename = data
            elif name == "speed":
                if data is not None:
                    self._speed = float(data)
            elif name == "eof-reached":
                new_value = bool(data)
                rising_edge = new_value and not self._eof_reached
                self._eof_reached = new_value
                if rising_edge:
                    return [Signal.EOF]
        return []

    def on_event(self, event) -> List[Signal]:
        if event == EVENT_START_FILE:
            self.clear_file()
            return [Signal.FILE_LOAD_START]
        if event == EVENT_FILE_LOADED:
            return [Signal.FILE_LOAD_DONE]
        if event == EVENT_QUIT:
            return [Signal.QUIT]
        return []

    def _store_position(self, value: float, recently_reset: bool, global_position: float) -> None:
        # value is never None here: on_property guards before calling.
        self._last_position_time = self._clock()
        if recently_reset:
            self._position = 0.0
        elif self._file_is_loaded() or (
                value < constants.MPV_NEWFILE_IGNORE_TIME and self._file_loaded):
            self._position = max(value, 0.0)
        else:
            self._position = global_position

    def _store_pause_state(self, value: bool, global_paused: bool) -> None:
        if self._file_is_loaded():
            self._paused = bool(value)
        else:
            # Normalise: the client's initial `_globalPaused` is the float
            # 0.0, and `0.0 == False` would make setPaused(False)'s
            # early-return guard swallow every unpause command.
            self._paused = bool(global_paused)

    def _file_is_loaded(self) -> bool:
        return (
            self._file_loaded
            and self._last_loaded_time is not None
            and self._clock() > self._last_loaded_time + constants.MPV_NEWFILE_IGNORE_TIME
        )

    def mark_file_loaded(self, when: Optional[float] = None) -> None:
        """Sole owner of ``file_loaded``/``last_loaded_time`` (called from
        ``_onFileUpdate`` — the FILE_LOAD_DONE hook name is kept for IINA's
        override)."""
        with self._lock:
            self._file_loaded = True
            self._last_loaded_time = self._clock() if when is None else when

    def clear_file(self) -> None:
        with self._lock:
            self._path = None
            self._duration = None
            self._media_title = None
            self._filename = None
            self._eof_reached = False
            self._file_loaded = False
            self._last_loaded_time = None

    def record_position(self, value: float, recently_reset: bool,
                        global_position: float) -> None:
        """Gated write from adapter code (openFile's reset-to-zero path).

        Same semantics as an observed ``time-pos`` sample: stamp, then gate.
        """
        with self._lock:
            self._store_position(value, recently_reset, global_position)

    def force_pause(self, value: bool) -> None:
        """Direct write from ``setPaused`` — a command result, not an
        observation, so it bypasses the file-loaded gate."""
        with self._lock:
            self._paused = bool(value)

    def force_position(self, value: float, stamp: bool = False) -> None:
        """Direct write from ``setPosition`` (command path, ungated)."""
        with self._lock:
            self._position = max(float(value), 0.0)
            if stamp:
                self._last_position_time = self._clock()

    def stamp_position(self) -> None:
        """Refresh the dead-reckoning stamp (after unpause / post-seek)."""
        with self._lock:
            self._last_position_time = self._clock()

    # ------------------------------------------------------------------- read

    def snapshot(self) -> StatusSnapshot:
        with self._lock:
            return StatusSnapshot(
                paused=self._paused,
                position=self._position,
                last_position_time=self._last_position_time,
                duration=self._duration,
                path=self._path,
                media_title=self._media_title,
                filename=self._filename,
                speed=self._speed,
                eof_reached=self._eof_reached,
                file_loaded=self._file_loaded,
                last_loaded_time=self._last_loaded_time,
                ready=self._saw_pause and self._saw_time_pos,
            )

    def calculated_position(self, global_position: float, recently_reset: bool) -> Tuple[float, bool]:
        """Dead-reckoned position. Returns ``(position, unresponsive)``.

        The cache only computes; showing the unresponsive error and dropping
        the player stay the caller's job (as does ``lastResetTime``). The
        duplicated dead ``_recentlyReset`` branch of the original
        ``MpvPlayer.getCalculatedPosition`` is intentionally not ported: the
        first ``recently_reset`` check already returns before it.
        """
        with self._lock:
            position = self._position
            last_position_time = self._last_position_time
            file_loaded = self._file_loaded
            paused = self._paused
            eof_reached = self._eof_reached
            duration = self._duration

        if recently_reset:
            return 0.0, False
        if not file_loaded:
            return global_position, False
        if last_position_time is None:
            return global_position, False
        if paused:
            return position, False

        diff = self._clock() - last_position_time
        # At keep-open EOF mpv stops emitting time-pos changes: extrapolating
        # a frozen tail forever would walk the position past the file end.
        # A stale-but-eof tail is a normal resting state, not a dead player
        # (IPC death is detected separately via the socket quit callback).
        if eof_reached:
            return position, False
        if diff > constants.PLAYER_ASK_DELAY:
            extrapolated = position + diff
            if duration is not None:
                extrapolated = min(extrapolated, duration)
            return extrapolated, diff > constants.MPV_UNRESPONSIVE_THRESHOLD
        return position, diff > constants.MPV_UNRESPONSIVE_THRESHOLD


def build_mpv_args(base_args: Dict[str, str], script_key: Optional[str] = None,
                   script_path: Optional[str] = None,
                   user_args: Optional[List[str]] = None) -> Dict[str, str]:
    """Build a fresh startup-args dict; never mutates ``base_args``.

    ``script_key`` is parameterised because memento injects the interface Lua
    as ``scripts`` (plural) while mpv uses ``script``; IINA passes none.
    """
    args = dict(base_args)
    if script_key is not None and script_path is not None:
        args[script_key] = script_path
    for argToAdd in user_args or []:
        if argToAdd.startswith('--'):
            argToAdd = argToAdd[2:]
        elif argToAdd.startswith('-'):
            argToAdd = argToAdd[1:]
        if argToAdd.strip() == "":
            continue
        if "=" in argToAdd:
            (argName, argValue) = argToAdd.split("=", 1)
            if argValue and argValue[0] == '"' and argValue[-1] == '"':
                argValue = argValue[1:-1]
        else:
            argName = argToAdd
            argValue = ""
        args[argName] = argValue
    return args
