"""T3: event-driven wiring tests for MpvPlayer (fake pipe + fake reactor).

No real mpv, no Twisted reactor: the player is assembled via object.__new__
and only its collaborators are stubbed. ``FakeReactor.callFromThread`` runs
the callback inline, which makes the threaded design deterministic here.
"""
import unittest
from unittest import mock

from syncplay import constants
from syncplay.players.mpv import MpvPlayer
from syncplay.players.mpv_status import (
    EVENT_FILE_LOADED,
    EVENT_START_FILE,
    OBSERVED_PROPERTIES,
    MpvStatusCache,
)


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeReactor:
    def __init__(self):
        self.calls = []

    def callFromThread(self, fn, *args):
        self.calls.append((fn, args))
        fn(*args)

    def callLater(self, delay, fn, *args):
        self.calls.append((fn, args))
        fn(*args)


class FakeUI:
    def __init__(self):
        self.debug = []
        self.errors = []

    def showDebugMessage(self, msg, *args):
        self.debug.append(msg)

    def showErrorMessage(self, msg, *args):
        self.errors.append(msg)

    def showMessage(self, msg, *args):
        self.debug.append(msg)


class FakeClient:
    def __init__(self):
        self.ui = FakeUI()
        self.status_updates = []
        self.file_updates = []
        self.eof_calls = 0
        self.stopped = []
        self.global_paused = False
        self.global_position = 5.0
        self._config = {option: True for option in constants.MPV_SYNCPLAYINTF_OPTIONS_TO_SEND}
        self._config.update({
            "chatOutputEnabled": True,
            "chatMoveOSD": False,
            "chatInputEnabled": False,
            "chatInputPosition": 0,
        })

    def getGlobalPaused(self):
        return self.global_paused

    def getGlobalPosition(self):
        return self.global_position

    def updatePlayerStatus(self, paused, position):
        self.status_updates.append((paused, position))

    def updateFile(self, filename, duration, path):
        self.file_updates.append((filename, duration, path))

    def eofReportedByPlayer(self):
        self.eof_calls += 1

    def stop(self, showWarning=True):
        self.stopped.append(showWarning)


class FakePipe:
    def __init__(self):
        self.property_bindings = {}
        self.event_bindings = {}
        self.commands = []

    def bind_property_observer(self, name, callback):
        self.property_bindings[name] = callback
        return len(self.property_bindings)

    def bind_event(self, name, callback):
        self.event_bindings.setdefault(name, set()).add(callback)

    def command(self, *args):
        self.commands.append(args)
        programmed = getattr(self, "command_results", {}).get(args)
        if programmed is not None:
            return programmed
        if args[:1] == ("get_property",):
            return None  # property unavailable
        return None

    def trigger_property(self, name, data):
        self.property_bindings[name](name, data)

    def trigger_event(self, event, **data):
        payload = dict(data)
        payload["event"] = event
        for callback in self.event_bindings.get(event, ()):
            callback(payload)


class FakeListener:
    def __init__(self):
        self.readyToSend = True
        self.sent = []
        self.chats = []
        self.mpvpipe = FakePipe()
        self.mpv_arguments = {}

    @property
    def isReadyForSend(self):
        return self.readyToSend

    def setReadyToSend(self, state):
        self.readyToSend = state

    def sendLine(self, line, notReadyAfterThis=None):
        self.sent.append(line)

    def sendChat(self, message):
        self.chats.append(message)


def make_player(paused=False, position=10.0, file_loaded=True):
    player = object.__new__(MpvPlayer)
    player.reactor = FakeReactor()
    player._client = FakeClient()
    player.clock = FakeClock()
    player._cache = MpvStatusCache(clock=player.clock)
    player._listener = FakeListener()
    player.quitReason = None
    player.lastResetTime = None
    player.delayedFilePath = None
    if file_loaded:
        player._cache.mark_file_loaded()
        player._cache.force_pause(paused)
        player._cache.force_position(position, stamp=True)
    return player


class WiringTests(unittest.TestCase):
    def test_wire_observers_installs_all_property_and_event_bindings(self):
        player = make_player()
        pipe = FakePipe()
        player._wireObservers(pipe)
        self.assertEqual(set(pipe.property_bindings), set(OBSERVED_PROPERTIES))
        self.assertEqual(set(pipe.event_bindings),
                         {EVENT_START_FILE, EVENT_FILE_LOADED, "client-message"})

    def test_all_property_callbacks_route_to_on_mpv_property(self):
        player = make_player()
        pipe = FakePipe()
        player._wireObservers(pipe)
        for name, callback in pipe.property_bindings.items():
            self.assertEqual(callback, player._onMpvProperty)


class PropertyCallbackTests(unittest.TestCase):
    @staticmethod
    def _wired(player):
        pipe = FakePipe()
        player._wireObservers(pipe)
        return player, pipe

    def test_status_properties_update_cache_without_reactor_hop(self):
        # Hard design rule: time-pos/pause must never flood the reactor.
        player, pipe = self._wired(make_player(file_loaded=True))
        player.clock.advance(2)  # open the MPV_NEWFILE_IGNORE_TIME window
        pipe.trigger_property("pause", True)
        pipe.trigger_property("time-pos", 42.0)
        snap = player._cache.snapshot()
        self.assertTrue(snap.paused)
        self.assertEqual(snap.position, 42.0)
        self.assertEqual(player.reactor.calls, [])

    def test_eof_edge_hops_to_eofDetected(self):
        player, pipe = self._wired(make_player(file_loaded=False))
        pipe.trigger_property("eof-reached", True)
        self.assertEqual(player._client.eof_calls, 1)
        # Second delivery of the same value: no extra EOF.
        pipe.trigger_property("eof-reached", True)
        self.assertEqual(player._client.eof_calls, 1)

    def test_position_gating_supplied_by_callback_context(self):
        # not loaded yet → position gated to the client's global position
        player, pipe = self._wired(make_player(file_loaded=False))
        pipe.trigger_property("time-pos", 42.0)
        self.assertEqual(player._cache.snapshot().position,
                         player._client.getGlobalPosition())


class EventCallbackTests(unittest.TestCase):
    def setUp(self):
        self.player = make_player(file_loaded=True)
        self.pipe = FakePipe()
        self.player._wireObservers(self.pipe)

    def test_start_file_gates_send_queue_and_clears_file_state(self):
        self.pipe.trigger_property("path", "a.mkv")
        self.pipe.trigger_event(EVENT_START_FILE)
        self.assertFalse(self.player._listener.readyToSend)
        snap = self.player._cache.snapshot()
        self.assertFalse(snap.file_loaded)
        self.assertIsNone(snap.path)

    def test_file_loaded_spawns_resolution_worker(self):
        with mock.patch("syncplay.players.mpv.threading.Thread") as thread_cls:
            self.pipe.trigger_event(EVENT_FILE_LOADED)
        thread_cls.assert_called_once()
        kwargs = thread_cls.call_args[1]
        self.assertEqual(kwargs["target"], self.player._resolveFileLoadFields)
        self.assertTrue(kwargs.get("daemon"))
        self.assertEqual(kwargs.get("args"),
                         (self.player._cache.current_load_generation(),))

    def test_unknown_event_is_ignored(self):
        self.pipe.trigger_event("playback-restart")
        self.assertTrue(self.player._listener.readyToSend)


class FileLoadResolutionTests(unittest.TestCase):
    def test_cached_file_fields_skip_their_ipc_queries_but_state_is_resynced(self):
        player = make_player(file_loaded=False)
        player._listener.setReadyToSend(False)
        # Global state matching mpv's reality: no pause/position reconcile
        # from _onFileUpdate muddies the assertions below.
        player._client.global_paused = True
        player._client.global_position = 3.5
        player._cache.on_property("filename", "ep1.mkv")
        player._cache.on_property("duration", 120.0)
        player._cache.on_property("path", "file:///ep1.mkv")
        player._listener.mpvpipe.command_results = {
            ("get_property", "pause"): True,
            ("get_property", "time-pos"): 3.5,
        }
        player._resolveFileLoadFields(player._cache.current_load_generation())
        queried = player._listener.mpvpipe.commands
        # filename/duration/path came from observers — not re-queried …
        self.assertNotIn(("get_property", "filename"), queried)
        self.assertNotIn(("get_property", "duration"), queried)
        self.assertNotIn(("get_property", "path"), queried)
        # … but pause/time-pos ARE re-read (observers only fire on changes).
        snap = player._cache.snapshot()
        self.assertIs(snap.paused, True)
        self.assertEqual(snap.position, 3.5)
        # hops to finalize on the reactor (FakeReactor runs it inline)
        self.assertIsNotNone(snap.last_loaded_time)
        self.assertTrue(player._listener.readyToSend)

    def test_missing_duration_queried_over_ipc_and_cached(self):
        player = make_player(file_loaded=False)
        player._cache.on_property("filename", "ep1.mkv")
        player._cache.on_property("path", "file:///ep1.mkv")
        player._listener.mpvpipe.command_results = {("get_property", "duration"): 55.0}
        player._resolveFileLoadFields(player._cache.current_load_generation())
        self.assertEqual(player._cache.snapshot().duration, 55.0)
        self.assertIn(("get_property", "duration"), player._listener.mpvpipe.commands)

    def test_unavailable_duration_normalises_to_zero(self):
        player = make_player(file_loaded=False)
        player._cache.on_property("filename", "stream.m3u8")
        player._cache.on_property("path", "https://example/stream.m3u8")
        # no command_results → get_property returns None (property unavailable)
        player._resolveFileLoadFields(player._cache.current_load_generation())
        self.assertEqual(player._client.file_updates,
                         [("stream.m3u8", 0.0, "https://example/stream.m3u8")])

    def test_stale_generation_worker_aborts_without_writes(self):
        # Playlist advance mid-resolve: the worker of the previous load must
        # not pollute the next file's cache or finalize it.
        player = make_player(file_loaded=False)
        stale_generation = player._cache.current_load_generation()
        player._cache.clear_file()  # a newer load started
        player._listener.setReadyToSend(False)
        player._listener.mpvpipe.command_results = {
            ("get_property", "pause"): True,
            ("get_property", "time-pos"): 9.9,
            ("get_property", "filename"): "next.mkv",
            ("get_property", "duration"): 1.0,
            ("get_property", "path"): "/tmp/next.mkv",
        }
        player._resolveFileLoadFields(stale_generation)
        snap = player._cache.snapshot()
        self.assertIsNone(snap.filename)
        self.assertIsNone(snap.duration)
        self.assertIsNone(snap.path)
        self.assertEqual(player._client.file_updates, [])
        self.assertFalse(player._listener.readyToSend)
        self.assertNotIn(["set_property", "pause", "no"], player._listener.sent)

    def test_load_resync_prevents_pause_unpause_deadlock(self):
        # Regression: pre-load the gated cache stored the client's initial
        # global 0.0; without the load-time resync, setPaused(False) hit
        # `0.0 == False`, returned early, and mpv stayed paused forever while
        # positions were pure wall-clock extrapolation.
        player = make_player(file_loaded=False)
        player._cache.force_pause(0.0)  # poisoned pre-load gate write
        player._cache.on_property("filename", "ep1.mkv")
        player._cache.on_property("duration", 8.0)
        player._cache.on_property("path", "/tmp/ep1.mkv")
        player._listener.mpvpipe.command_results = {
            ("get_property", "pause"): True,
            ("get_property", "time-pos"): 0.0,
        }
        player._resolveFileLoadFields(player._cache.current_load_generation())
        # _onFileUpdate's reconcile saw resynced True vs global False and
        # actually sent the unpause (with a poisoned 0.0 the guard swallowed
        # it and mpv never started).
        self.assertIn(["set_property", "pause", "no"], player._listener.sent)
        self.assertIs(player._cache.snapshot().paused, False)


class FileUpdateHookTests(unittest.TestCase):
    def test_finalize_marks_loaded_and_readies_send_queue(self):
        player = make_player(file_loaded=False)
        player._listener.setReadyToSend(False)
        player._cache.on_property("filename", "ep1.mkv")
        player._cache.on_property("duration", 10.0)
        player._cache.on_property("path", "file:///ep1.mkv")
        player._finalizeFileLoad()
        self.assertTrue(player._cache.snapshot().file_loaded)
        self.assertTrue(player._listener.readyToSend)
        self.assertEqual(player._client.file_updates, [("ep1.mkv", 10.0, "file:///ep1.mkv")])

    def test_position_reconciles_to_global_when_not_recently_reset(self):
        player = make_player(file_loaded=False, position=0.0)
        player._cache.on_property("filename", "ep1.mkv")
        player._cache.on_property("duration", 10.0)
        player._cache.on_property("path", "file:///ep1.mkv")
        player._finalizeFileLoad()
        # setPosition ran (FakeReactor executes inline): property command sent
        self.assertIn(["set_property", "time-pos", "5.0"], player._listener.sent)

    def test_pause_reconcile_actually_applies_global_pause(self):
        # Regression: the old code called getGlobalPaused as a getter and
        # discarded the result, so the pause state was never reconciled.
        player = make_player(file_loaded=False, paused=False)
        player._client.global_paused = True
        player._cache.on_property("filename", "ep1.mkv")
        player._cache.on_property("duration", 10.0)
        player._cache.on_property("path", "file:///ep1.mkv")
        player._finalizeFileLoad()
        self.assertIn(["set_property", "pause", "yes"], player._listener.sent)
        self.assertTrue(player._cache.snapshot().paused)

    def test_placeholder_guard_reads_cache_and_skips_update(self):
        try:
            from syncplay.players.iina import IinaPlayer
        except ImportError:  # pragma: no cover
            self.skipTest("IINA module unavailable")
        player = object.__new__(IinaPlayer)
        player.reactor = FakeReactor()
        player._client = FakeClient()
        player._cache = MpvStatusCache(clock=FakeClock())
        player._listener = FakeListener()
        player.quitReason = None
        player.lastResetTime = None
        player._cache.on_property("filename", "iina-bkg.png")
        player._cache.on_property("duration", 1.0)
        player._cache.on_property("path", "file:///iina-bkg.png")
        player._onFileUpdate()
        self.assertEqual(player._client.file_updates, [])
        self.assertFalse(player._cache.snapshot().file_loaded)

        player._cache.on_property("filename", "movie.mkv")
        player._onFileUpdate()
        self.assertEqual(player._client.file_updates[0][0], "movie.mkv")


class AskForStatusTests(unittest.TestCase):
    def test_not_ready_gate_reports_nothing(self):
        player = make_player()
        player._listener.setReadyToSend(False)
        player.askForStatus()
        self.assertEqual(player._client.status_updates, [])

    def test_ready_reports_cache_values(self):
        player = make_player(paused=True, position=10.0, file_loaded=True)
        # stamp is fresh (FakeClock now) → no extrapolation
        player.askForStatus()
        self.assertEqual(player._client.status_updates, [(True, 10.0)])

    def test_not_loaded_falls_back_to_global_state(self):
        player = make_player(file_loaded=False)
        player.askForStatus()
        self.assertEqual(player._client.status_updates,
                         [(player._client.getGlobalPaused(), player._client.getGlobalPosition())])

    def test_unresponsive_player_drops_and_still_reports(self):
        # Unpaused + stale beyond the threshold (paused/eof states no longer
        # count as unresponsive — they are normal resting states).
        player = make_player(paused=False, position=10.0, file_loaded=True)
        player.clock.advance(120.0)  # beyond MPV_UNRESPONSIVE_THRESHOLD
        player.askForStatus()
        self.assertTrue(player._client.ui.errors)
        self.assertIn(["quit"], player._listener.sent)      # drop() tried to quit
        self.assertEqual(player._client.stopped, [False])   # client.stop scheduled
        self.assertEqual(len(player._client.status_updates), 1)  # parity: still reports


class LineReceivedTests(unittest.TestCase):
    def test_stale_ans_status_lines_are_ignored(self):
        player = make_player()
        player.lineReceived("ANS_time_pos=5.0")
        player.lineReceived("ANS_pause=yes")
        self.assertEqual(player._cache.snapshot().position, 10.0)
        self.assertEqual(player._client.status_updates, [])

    def test_abnormal_exit_toasts_reason_and_drops(self):
        player = make_player()
        player.lineReceived("Exiting... (Terminated)")
        self.assertTrue(player._client.ui.errors)
        self.assertIn(["quit"], player._listener.sent)
        self.assertIn(False, player._client.stopped)

    def test_clean_quit_drops_without_toast(self):
        player = make_player()
        player.lineReceived("Exiting... (Quit)")
        self.assertEqual(player._client.ui.errors, [])
        self.assertIn(["quit"], player._listener.sent)

    def test_load_failure_reopens_send_gate(self):
        player = make_player()
        player._listener.setReadyToSend(False)
        player.lineReceived("Failed to open file /missing.mkv")
        self.assertTrue(player._listener.readyToSend)

    def test_playing_line_gates_and_clears(self):
        player = make_player()
        player.lineReceived("Playing: /some/file.mkv")
        self.assertFalse(player._listener.readyToSend)
        self.assertFalse(player._cache.snapshot().file_loaded)

    def test_legacy_chat_marker_line_is_ignored(self):
        # The Lua script no longer prints these; stray log lines are inert.
        player = make_player()
        player.lineReceived("<chat>hello</chat>")
        self.assertEqual(player._listener.chats, [])

    def test_legacy_options_ping_marker_line_is_ignored(self):
        # The handshake now travels as a client-message event (T4).
        player = make_player()
        player.lineReceived("<get_syncplayintf_options>")
        self.assertFalse(any(
            isinstance(line, list) and "set_syncplayintf_options" in line
            for line in player._listener.sent))


class ClientMessageRoutingTests(unittest.TestCase):
    """Lua → Python transport: syncplay-chat / syncplay-intf-ready."""

    def setUp(self):
        self.player = make_player()
        self.pipe = FakePipe()
        self.player._wireObservers(self.pipe)

    def _fire(self, *args):
        self.pipe.trigger_event("client-message", args=list(args))

    def _sends_options(self):
        return any(isinstance(line, list) and "set_syncplayintf_options" in line
                   for line in self.player._listener.sent)

    def test_chat_routed_verbatim_including_backslashes_and_quotes(self):
        payload = 'C:\\path "quoted" <b>hi</b>'
        self._fire("syncplay-chat", payload)
        self.assertEqual(self.player._listener.chats, [payload])

    def test_chat_without_payload_ignored(self):
        self._fire("syncplay-chat")
        self.assertEqual(self.player._listener.chats, [])

    def test_script_ready_triggers_options_send_via_reactor_hop(self):
        self._fire("syncplay-intf-ready")
        self.assertTrue(self._sends_options())
        # The handshake must hop off the EventHandler thread: _sendMpvOptions
        # issues synchronous mpvpipe.command() calls that would
        # head-of-line-block every other callback for up to 120 s.
        hopped = [fn for fn, _args in self.player.reactor.calls
                  if fn == self.player._sendMpvOptions]
        self.assertEqual(len(hopped), 1)

    def test_unknown_message_prefix_ignored(self):
        self._fire("something-else", "x")
        self.assertEqual(self.player._listener.chats, [])
        self.assertFalse(self._sends_options())

    def test_empty_message_ignored(self):
        self._fire()
        self.assertEqual(self.player._listener.chats, [])
        self.assertFalse(self._sends_options())


class CommandBridgeTests(unittest.TestCase):
    def test_set_paused_writes_property_and_cache_once(self):
        player = make_player(paused=False, file_loaded=True)
        player.setPaused(True)
        self.assertIn(["set_property", "pause", "yes"], player._listener.sent)
        self.assertTrue(player._cache.snapshot().paused)
        sent_count = len(player._listener.sent)
        player.setPaused(True)  # already that state → no second command
        self.assertEqual(len(player._listener.sent), sent_count)

    def test_set_position_updates_cache_and_stamps(self):
        player = make_player(position=10.0, file_loaded=True)
        player.setPosition(30.0)
        snap = player._cache.snapshot()
        self.assertEqual(snap.position, 30.0)
        self.assertIsNotNone(snap.last_position_time)
        self.assertIn(["set_property", "time-pos", "30.0"], player._listener.sent)


if __name__ == "__main__":
    unittest.main()
