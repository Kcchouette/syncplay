"""T2: pure status-model tests for mpv_status (no I/O, no Twisted, no mpv)."""
import threading
import unittest

from syncplay import constants
from syncplay.players.mpv_status import (
    OBSERVED_PROPERTIES,
    MpvStatusCache,
    Signal,
    build_mpv_args,
)


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class PropertyUpdateTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.cache = MpvStatusCache(clock=self.clock)

    def test_observed_properties_cover_status_needs(self):
        for prop in ("pause", "time-pos", "duration", "path", "eof-reached"):
            self.assertIn(prop, OBSERVED_PROPERTIES)

    def test_unknown_property_is_ignored(self):
        signals = self.cache.on_property("vo-ok", "yes")
        self.assertEqual(signals, [])
        self.assertIsNone(self.cache.snapshot().position)

    def test_time_pos_and_pause_populate_snapshot(self):
        self.cache.mark_file_loaded()
        self.clock.advance(2)  # open the load window so gating stores samples
        self.cache.on_property("pause", True)
        self.cache.on_property("time-pos", 12.5, global_paused=False)
        snap = self.cache.snapshot()
        self.assertTrue(snap.paused)
        self.assertEqual(snap.position, 12.5)
        self.assertEqual(snap.last_position_time, self.clock.now)
        self.assertTrue(snap.ready)

    def test_ready_requires_both_pause_and_time_pos(self):
        self.cache.on_property("time-pos", 1.0)
        self.assertFalse(self.cache.snapshot().ready)
        self.cache.on_property("pause", False)
        self.assertTrue(self.cache.snapshot().ready)

    def test_none_time_pos_is_ignored(self):
        self.cache.on_property("pause", True)
        self.cache.on_property("time-pos", None)
        self.assertIsNone(self.cache.snapshot().position)
        self.assertIsNone(self.cache.snapshot().last_position_time)

    def test_duration_and_filename_populate(self):
        self.cache.on_property("duration", 120.0)
        self.cache.on_property("filename", "ep1.mkv")
        snap = self.cache.snapshot()
        self.assertEqual(snap.duration, 120.0)
        self.assertEqual(snap.filename, "ep1.mkv")

    def test_none_duration_keeps_previous_value(self):
        self.cache.on_property("duration", 120.0)
        self.cache.on_property("duration", None)
        self.assertEqual(self.cache.snapshot().duration, 120.0)


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.cache = MpvStatusCache(clock=self.clock)

    def test_start_file_clears_file_state_and_signals(self):
        self.cache.on_property("path", "a.mkv")
        self.cache.on_property("duration", 10.0)
        self.cache.mark_file_loaded()
        signals = self.cache.on_event("start-file")
        self.assertEqual(signals, [Signal.FILE_LOAD_START])
        snap = self.cache.snapshot()
        self.assertIsNone(snap.path)
        self.assertIsNone(snap.duration)
        self.assertFalse(snap.file_loaded)

    def test_start_file_does_not_reset_ready(self):
        self.cache.on_property("pause", True)
        self.cache.on_property("time-pos", 1.0)
        self.cache.on_event("start-file")
        # mpv will not re-emit `pause` if unchanged across files; ready is
        # sticky per-process so askForStatus must not fall back forever.
        self.assertTrue(self.cache.snapshot().ready)

    def test_file_loaded_signals_done(self):
        self.assertEqual(self.cache.on_event("file-loaded"), [Signal.FILE_LOAD_DONE])

    def test_unknown_event_no_signal(self):
        self.assertEqual(self.cache.on_event("playback-restart"), [])

    def test_clear_file_bumps_load_generation(self):
        generation = self.cache.current_load_generation()
        self.cache.clear_file()
        self.assertEqual(self.cache.current_load_generation(), generation + 1)
        self.cache.on_event("start-file")  # also routes through clear_file
        self.assertEqual(self.cache.current_load_generation(), generation + 2)

    def test_eof_rising_edge_signals_once(self):
        self.assertEqual(self.cache.on_property("eof-reached", False), [])
        self.assertEqual(self.cache.on_property("eof-reached", True), [Signal.EOF])
        # Already high: no second EOF (e.g. observer re-delivery).
        self.assertEqual(self.cache.on_property("eof-reached", True), [])
        self.assertEqual(self.cache.on_property("eof-reached", False), [])
        self.assertEqual(self.cache.on_property("eof-reached", True), [Signal.EOF])

    def test_eof_cleared_on_start_file(self):
        self.cache.on_property("eof-reached", True)
        self.cache.on_event("start-file")
        self.assertFalse(self.cache.snapshot().eof_reached)


class PositionGatingTests(unittest.TestCase):
    """Parity with MpvPlayer._storePosition / _storePauseState transitions."""

    def setUp(self):
        self.clock = FakeClock()
        self.cache = MpvStatusCache(clock=self.clock)

    def test_recently_reset_forces_zero_and_stamps(self):
        self.cache.on_property("time-pos", 42.0, recently_reset=True)
        snap = self.cache.snapshot()
        self.assertEqual(snap.position, 0.0)
        self.assertEqual(snap.last_position_time, self.clock.now)

    def test_no_file_loaded_falls_back_to_global(self):
        self.cache.on_property("time-pos", 42.0, global_position=7.0)
        self.assertEqual(self.cache.snapshot().position, 7.0)

    def test_file_loaded_window_accepts_value(self):
        self.cache.mark_file_loaded()
        self.clock.advance(2)  # beyond MPV_NEWFILE_IGNORE_TIME (1s)
        self.cache.on_property("time-pos", 42.0, global_position=7.0)
        self.assertEqual(self.cache.snapshot().position, 42.0)

    def test_new_file_window_small_value_accepted_without_full_delay(self):
        self.cache.mark_file_loaded()  # just loaded: window still closed
        self.cache.on_property("time-pos", 0.4, global_position=7.0)
        self.assertEqual(self.cache.snapshot().position, 0.4)

    def test_new_file_window_large_value_falls_back_to_global(self):
        self.cache.mark_file_loaded()
        self.cache.on_property("time-pos", 42.0, global_position=7.0)
        self.assertEqual(self.cache.snapshot().position, 7.0)

    def test_negative_value_clamped_to_zero_when_loaded(self):
        self.cache.mark_file_loaded()
        self.clock.advance(2)
        self.cache.on_property("time-pos", -1.0, global_position=7.0)
        self.assertEqual(self.cache.snapshot().position, 0.0)

    def test_pause_global_wins_before_file_loaded(self):
        # Not loaded yet: gated store mirrors the client's global pause.
        self.cache.on_property("pause", True, global_paused=False)
        self.assertFalse(self.cache.snapshot().paused)

    def test_pause_fallback_normalized_to_strict_bool(self):
        # The client's initial _globalPaused is the float 0.0; storing it
        # raw would make setPaused(False)'s guard (0.0 == False) swallow the
        # unpause command forever.
        self.cache.on_property("pause", True, global_paused=0.0)
        stored = self.cache.snapshot().paused
        self.assertIs(stored, False)

    def test_pause_stored_once_loaded(self):
        self.cache.mark_file_loaded()
        self.clock.advance(2)
        self.cache.on_property("pause", True, global_paused=False)
        self.assertTrue(self.cache.snapshot().paused)


class CalculatedPositionTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.cache = MpvStatusCache(clock=self.clock)

    def _loaded_at(self, position=10.0, paused=False):
        self.cache.mark_file_loaded()
        self.clock.advance(2)  # open the MPV_NEWFILE_IGNORE_TIME (1s) window
        self.cache.on_property("pause", paused)
        self.cache.on_property("time-pos", position)

    def test_recently_reset_returns_zero(self):
        self._loaded_at()
        position, unresponsive = self.cache.calculated_position(
            global_position=99.0, recently_reset=True)
        self.assertEqual(position, 0.0)
        self.assertFalse(unresponsive)

    def test_not_loaded_returns_global(self):
        position, unresponsive = self.cache.calculated_position(
            global_position=99.0, recently_reset=False)
        self.assertEqual(position, 99.0)
        self.assertFalse(unresponsive)

    def test_no_position_stamp_returns_global(self):
        self.cache.mark_file_loaded()
        position, _ = self.cache.calculated_position(99.0, recently_reset=False)
        self.assertEqual(position, 99.0)

    def test_recent_unpaused_position_not_extrapolated(self):
        self._loaded_at(position=10.0, paused=False)
        self.clock.advance(0.05)  # < PLAYER_ASK_DELAY (0.1)
        position, unresponsive = self.cache.calculated_position(99.0, False)
        self.assertAlmostEqual(position, 10.0)
        self.assertFalse(unresponsive)

    def test_stale_unpaused_position_extrapolated(self):
        self._loaded_at(position=10.0, paused=False)
        self.clock.advance(0.3)
        position, unresponsive = self.cache.calculated_position(99.0, False)
        self.assertAlmostEqual(position, 10.3)
        self.assertFalse(unresponsive)

    def test_stale_paused_position_not_extrapolated(self):
        self._loaded_at(position=10.0, paused=True)
        self.clock.advance(0.3)
        position, unresponsive = self.cache.calculated_position(99.0, False)
        self.assertAlmostEqual(position, 10.0)
        # A paused player going quiet is normal, not an unresponsive one.
        self.assertFalse(unresponsive)

    def test_unresponsive_flag_beyond_threshold_when_playing(self):
        self._loaded_at(position=10.0, paused=False)
        self.clock.advance(constants.MPV_UNRESPONSIVE_THRESHOLD + 1)
        position, unresponsive = self.cache.calculated_position(99.0, False)
        self.assertTrue(unresponsive)
        self.assertAlmostEqual(position, 10.0 + constants.MPV_UNRESPONSIVE_THRESHOLD + 1)

    def test_unresponsive_unpaused_still_extrapolates(self):
        self._loaded_at(position=10.0, paused=False)
        self.clock.advance(constants.MPV_UNRESPONSIVE_THRESHOLD + 5)
        position, unresponsive = self.cache.calculated_position(99.0, False)
        self.assertTrue(unresponsive)
        self.assertAlmostEqual(position, 10.0 + constants.MPV_UNRESPONSIVE_THRESHOLD + 5)

    def test_eof_freezes_extrapolation(self):
        # keep-open EOF: time-pos stops changing while mpv reports unpaused
        # (or its pause change was lost) — position must not walk past the
        # end, and the resting state must not count as unresponsive.
        self._loaded_at(position=8.0, paused=False)
        self.cache.on_property("eof-reached", True)
        self.clock.advance(30.0)
        position, unresponsive = self.cache.calculated_position(99.0, False)
        self.assertAlmostEqual(position, 8.0)
        self.assertFalse(unresponsive)

    def test_extrapolation_capped_at_duration(self):
        self._loaded_at(position=7.9, paused=False)
        self.cache.on_property("duration", 8.023)
        self.clock.advance(5.0)
        position, _ = self.cache.calculated_position(99.0, False)
        self.assertAlmostEqual(position, 8.023)


class SnapshotConsistencyTests(unittest.TestCase):
    def test_snapshot_is_immutable(self):
        cache = MpvStatusCache(clock=FakeClock())
        cache.on_property("duration", 10.0)
        snap = cache.snapshot()
        with self.assertRaises(Exception):
            snap.duration = 99.0  # frozen dataclass

    def test_concurrent_writes_and_reads_keep_fields_consistent(self):
        # duration and speed are always written as a coordinated pair; a torn
        # snapshot would ever show them out of sync.
        cache = MpvStatusCache(clock=FakeClock())
        stop = threading.Event()
        violations = []

        def writer():
            for i in range(3000):
                cache.on_property("duration", float(i))
                cache.on_property("speed", float(i))

        def reader():
            while not stop.is_set():
                snap = cache.snapshot()
                if (snap.duration is not None and snap.speed is not None
                        and snap.duration != snap.speed):
                    violations.append((snap.duration, snap.speed))
                    return

        writer_thread = threading.Thread(target=writer)
        reader_thread = threading.Thread(target=reader)
        writer_thread.start()
        reader_thread.start()
        writer_thread.join()
        stop.set()
        reader_thread.join(timeout=5)
        self.assertEqual(violations, [])


class BuildMpvArgsTests(unittest.TestCase):
    def setUp(self):
        self.base = {"force-window": "yes", "idle": "yes"}

    def test_returns_new_dict(self):
        result = build_mpv_args(self.base)
        self.assertIsNot(result, self.base)

    def test_does_not_mutate_base(self):
        build_mpv_args(self.base, "script", "/x/syncplayintf.lua", ["--foo=bar"])
        self.assertEqual(self.base, {"force-window": "yes", "idle": "yes"})

    def test_script_key_injected(self):
        result = build_mpv_args(self.base, "script", "/x/syncplayintf.lua")
        self.assertEqual(result["script"], "/x/syncplayintf.lua")

    def test_memento_scripts_key(self):
        result = build_mpv_args(self.base, "scripts", "/x/syncplayintf.lua")
        self.assertEqual(result["scripts"], "/x/syncplayintf.lua")
        self.assertNotIn("script", result)

    def test_no_script_key_when_absent(self):
        result = build_mpv_args(self.base)
        self.assertNotIn("script", result)
        self.assertNotIn("scripts", result)

    def test_user_args_merge(self):
        result = build_mpv_args(self.base, "script", "/x/lua",
                                ["--foo=bar", "-x", '--label="quoted"'])
        self.assertEqual(result.get("foo"), "bar")
        self.assertEqual(result.get("x"), "")
        self.assertEqual(result.get("label"), "quoted")

    def test_empty_value_does_not_crash(self):
        result = build_mpv_args(self.base, user_args=["--foo="])
        self.assertEqual(result.get("foo"), "")

    def test_blank_arg_skipped(self):
        result = build_mpv_args(self.base, user_args=["--", "-"])
        self.assertNotIn("", result)

    def test_idempotent(self):
        first = build_mpv_args(self.base, "script", "/x/lua", ["--foo=bar"])
        second = build_mpv_args(self.base, "script", "/x/lua", ["--foo=bar"])
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
