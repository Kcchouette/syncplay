# coding: utf8
"""Regression tests for the speed-revert-baseline fix.

Covers: baseline capture on slowdown, revert to captured baseline (the
reported bug: revert used to hardcode 1.00), fallback chain
live-read -> config['speed'] -> 1.0, relative slowdown, guards, option
application guards, and per-player getSpeed() parsing where testable
without a live player.
"""
import threading
import time
import types
import unittest

from syncplay.client import SyncplayClient
from syncplay.players.mplayer import MplayerPlayer
from syncplay.players.vlc import VlcPlayer


class StubUI(object):
    def __init__(self):
        self.messages = []
        self.debug = []

    def showMessage(self, msg, *args, **kwargs):
        self.messages.append(msg)

    def showDebugMessage(self, msg):
        self.debug.append(msg)


class StubPlayer(object):
    speedSupported = True

    def __init__(self, speed=None):
        self._speed = speed
        self.setSpeed_calls = []

    def getSpeed(self):
        return self._speed

    def setSpeed(self, value):
        self.setSpeed_calls.append(value)


def make_client(speed=None, config_speed=1.0, username="alice"):
    """SyncplayClient with just the state _slowDownToCoverTimeDifference touches."""
    client = object.__new__(SyncplayClient)
    client._config = {
        'slowdownThreshold': 1.5,
        'slowOnDesync': True,
        'speed': config_speed,
    }
    client.ui = StubUI()
    client._player = StubPlayer(speed)
    client._speedChanged = False
    client._speedBeforeSlowdown = None
    client.getUsername = lambda: username
    return client


class SlowdownRevertTests(unittest.TestCase):
    """Baseline capture and revert (client._slowDownToCoverTimeDifference)."""

    def test_slowdown_captures_baseline_and_applies_relative_rate(self):
        client = make_client(speed=1.21)
        changed = client._slowDownToCoverTimeDifference(2.0, "bob")
        self.assertTrue(changed)
        self.assertEqual(len(client._player.setSpeed_calls), 1)
        self.assertAlmostEqual(client._player.setSpeed_calls[0], 1.21 * 0.95)
        self.assertEqual(client._speedBeforeSlowdown, 1.21)
        self.assertTrue(client._speedChanged)

    def test_revert_restores_captured_baseline(self):
        # The reported bug: revert must restore 1.21, not 1.00.
        client = make_client(speed=1.21)
        client._slowDownToCoverTimeDifference(2.0, "bob")
        changed = client._slowDownToCoverTimeDifference(0.05, "bob")
        self.assertTrue(changed)
        self.assertEqual(client._player.setSpeed_calls[-1], 1.21)
        self.assertFalse(client._speedChanged)
        self.assertIsNone(client._speedBeforeSlowdown)

    def test_fallback_to_one_when_read_fails_and_config_default(self):
        client = make_client(speed=None, config_speed=1.0)
        client._slowDownToCoverTimeDifference(2.0, "bob")
        self.assertAlmostEqual(client._player.setSpeed_calls[0], 1.0 * 0.95)
        client._slowDownToCoverTimeDifference(0.05, "bob")
        self.assertEqual(client._player.setSpeed_calls[-1], 1.0)
        self.assertFalse(client._speedChanged)

    def test_fallback_to_config_speed_when_read_fails(self):
        client = make_client(speed=None, config_speed=1.21)
        client._slowDownToCoverTimeDifference(2.0, "bob")
        self.assertAlmostEqual(client._player.setSpeed_calls[0], 1.21 * 0.95)
        client._slowDownToCoverTimeDifference(0.05, "bob")
        self.assertEqual(client._player.setSpeed_calls[-1], 1.21)

    def test_relative_slowdown_for_baseline_below_one(self):
        client = make_client(speed=0.8)
        client._slowDownToCoverTimeDifference(2.0, "bob")
        self.assertAlmostEqual(client._player.setSpeed_calls[0], 0.8 * 0.95)
        client._slowDownToCoverTimeDifference(0.05, "bob")
        self.assertEqual(client._player.setSpeed_calls[-1], 0.8)

    def test_no_second_capture_while_speed_changed(self):
        client = make_client(speed=1.21)
        client._slowDownToCoverTimeDifference(2.0, "bob")
        calls_before = list(client._player.setSpeed_calls)
        changed = client._slowDownToCoverTimeDifference(2.0, "bob")
        self.assertFalse(changed)
        self.assertEqual(client._player.setSpeed_calls, calls_before)
        self.assertEqual(client._speedBeforeSlowdown, 1.21)

    def test_revert_happens_even_when_set_by_self(self):
        client = make_client(speed=1.21, username="alice")
        client._slowDownToCoverTimeDifference(2.0, "bob")
        changed = client._slowDownToCoverTimeDifference(0.05, "alice")
        self.assertTrue(changed)
        self.assertEqual(client._player.setSpeed_calls[-1], 1.21)

    def test_no_action_below_threshold(self):
        client = make_client(speed=1.21)
        changed = client._slowDownToCoverTimeDifference(1.0, "bob")
        self.assertFalse(changed)
        self.assertEqual(client._player.setSpeed_calls, [])
        self.assertIsNone(client._speedBeforeSlowdown)


class ApplyConfiguredSpeedTests(unittest.TestCase):
    """Option application guards (client._applyConfiguredSpeed)."""

    def _client(self, config_speed, speed_supported=True):
        client = make_client(speed=None, config_speed=config_speed)
        player = StubPlayer(None)
        player.speedSupported = speed_supported
        client._player = player
        return client

    def test_applies_configured_speed(self):
        client = self._client(1.21, speed_supported=True)
        client._applyConfiguredSpeed()
        self.assertEqual(client._player.setSpeed_calls, [1.21])

    def test_never_applies_when_config_is_default(self):
        # Guards the player-arguments path: no setSpeed at all when unset.
        client = self._client(1.0, speed_supported=True)
        client._applyConfiguredSpeed()
        self.assertEqual(client._player.setSpeed_calls, [])

    def test_never_applies_when_speed_unsupported(self):
        client = self._client(1.21, speed_supported=False)
        client._applyConfiguredSpeed()
        self.assertEqual(client._player.setSpeed_calls, [])


class MplayerGetSpeedTests(unittest.TestCase):
    """mplayer getSpeed(): ANS_speed parsing and bounded timeout."""

    def _make_player(self):
        player = object.__new__(MplayerPlayer)
        player._speed = None
        player._speedAsk = threading.Event()
        player._client = types.SimpleNamespace(ui=StubUI())
        player._listener = types.SimpleNamespace(sent=[], sendLine=lambda line: player._listener.sent.append(line))
        return player

    def test_line_received_parses_ans_speed(self):
        player = self._make_player()
        player.lineReceived("ANS_speed=1.21")
        self.assertEqual(player._speed, 1.21)
        self.assertTrue(player._speedAsk.is_set())

    def test_get_speed_timeout_returns_none(self):
        player = self._make_player()
        start = time.time()
        result = player.getSpeed()  # no reply from the stub listener
        elapsed = time.time() - start
        self.assertIsNone(result)
        self.assertLess(elapsed, 1.5)
        self.assertIn("get_property speed", player._listener.sent)

    def test_stale_event_is_cleared_before_ask(self):
        player = self._make_player()
        player._speedAsk.set()  # stale set event from a previous exchange
        result = player.getSpeed()  # no reply -> must still time out
        self.assertIsNone(result)

    def test_unparsable_value_yields_none(self):
        player = self._make_player()
        player.lineReceived("ANS_speed=not-a-number")
        self.assertIsNone(player._speed)
        self.assertTrue(player._speedAsk.is_set())


class VlcRateCacheTests(unittest.TestCase):
    """VLC rate cache parsing (non-blocking getSpeed)."""

    def _make_player(self, rate=None):
        player = object.__new__(VlcPlayer)
        player._rate = rate
        player._client = types.SimpleNamespace(ui=StubUI())
        return player

    def test_rate_parse_with_comma_radix(self):
        player = self._make_player()
        player.lineReceived(b"rate: 1,21")
        self.assertEqual(player._rate, 1.21)
        self.assertEqual(player.getSpeed(), 1.21)

    def test_rate_parse_plain_value(self):
        player = self._make_player()
        player.lineReceived(b"rate: 1.21")
        self.assertEqual(player.getSpeed(), 1.21)

    def test_unparsable_rate_keeps_previous_cache(self):
        player = self._make_player(rate=1.21)
        player.lineReceived(b"rate: nil")  # Lua may emit nil
        self.assertEqual(player.getSpeed(), 1.21)

    def test_no_input_keeps_previous_cache(self):
        player = self._make_player(rate=1.21)
        player.lineReceived(b"rate: no-input")
        self.assertEqual(player.getSpeed(), 1.21)

    def test_get_speed_before_first_poll_returns_none(self):
        player = self._make_player()
        self.assertIsNone(player.getSpeed())


if __name__ == '__main__':
    unittest.main()
