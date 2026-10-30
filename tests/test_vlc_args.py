"""T5: VLC launch argv builder, interface installer, and version gate.

Pure-logic tests: no VLC binary, no Twisted reactor (importing
syncplay.players.vlc only pulls twisted.internet.protocol, not the reactor).
"""
import os
import stat
import tempfile
import threading
import unittest

from syncplay import constants
from syncplay.players.vlc import VlcPlayer

from test_mpv_signal_wiring import FakeClient, FakeReactor


class BuildVlcCallTests(unittest.TestCase):
    def setUp(self):
        self._orig_slave = list(constants.VLC_SLAVE_ARGS)
        self._orig_extra = list(constants.VLC_SLAVE_EXTRA_ARGS)

    def tearDown(self):
        constants.VLC_SLAVE_ARGS[:] = self._orig_slave
        constants.VLC_SLAVE_EXTRA_ARGS[:] = self._orig_extra

    def test_constants_not_mutated_by_builds(self):
        # The old class-body `SLAVE_ARGS = VLC_SLAVE_ARGS; extend(...)` mutated
        # the module constant at import time; the builder must not.
        before = list(constants.VLC_SLAVE_ARGS)
        VlcPlayer.buildVlcCall("vlc", None, "/mod", 10001)
        VlcPlayer.buildVlcCall("vlc", None, "/mod", 23456)
        self.assertEqual(constants.VLC_SLAVE_ARGS, before)

    def test_per_call_single_lua_config_with_distinct_ports(self):
        call1 = VlcPlayer.buildVlcCall("vlc", None, "/mod", 10001)
        call2 = VlcPlayer.buildVlcCall("vlc", None, "/mod", 23456)
        lua1 = [a for a in call1 if a.startswith("--lua-config=syncplay=")]
        lua2 = [a for a in call2 if a.startswith("--lua-config=syncplay=")]
        self.assertEqual(len(lua1), 1)
        self.assertEqual(len(lua2), 1)
        self.assertIn('port="10001"', lua1[0])
        self.assertIn('port="23456"', lua2[0])

    def test_repeated_builds_are_idempotent(self):
        call1 = VlcPlayer.buildVlcCall("vlc", None, "/mod", 10001, ["--foo=bar"])
        call2 = VlcPlayer.buildVlcCall("vlc", None, "/mod", 10001, ["--foo=bar"])
        self.assertEqual(call1, call2)

    def test_ascii_path_and_quoted_user_arg(self):
        call = VlcPlayer.buildVlcCall("vlc", "/tmp/f.mkv", "/mod", 1, ['--opt="val"'])
        self.assertIn("/tmp/f.mkv", call)
        self.assertIn("--opt=val", call)

    def test_non_ascii_path_converted_to_mrl(self):
        call = VlcPlayer.buildVlcCall("vlc", "/tmp/fïlm.mkv", "/mod", 1)
        self.assertTrue(any(a.startswith("file://") for a in call))

    def test_slave_args_present(self):
        call = VlcPlayer.buildVlcCall("vlc", None, "/mod", 1)
        self.assertIn("--extraintf=luaintf", call)
        self.assertIn("--lua-intf=syncplay", call)


class EnsureVlcInterfaceTests(unittest.TestCase):
    def _write(self, path, content):
        with open(path, "w") as f:
            f.write(content)

    def test_identical_content_skips_copy_but_reapplies_chmod(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src.lua")
            dst = os.path.join(d, "dst.lua")
            self._write(src, "-- interface\n")
            self._write(dst, "-- interface\n")
            os.chmod(dst, 0o644)
            os.utime(dst, (1000000000, 1000000000))
            before = os.stat(dst).st_mtime
            VlcPlayer._ensureVlcInterface(src, dst)
            self.assertEqual(os.stat(dst).st_mtime, before)  # not rewritten
            self.assertEqual(stat.S_IMODE(os.stat(dst).st_mode), 0o755)

    def test_different_content_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src.lua")
            dst = os.path.join(d, "dst.lua")
            self._write(src, "-- new\n")
            self._write(dst, "-- old\n")
            VlcPlayer._ensureVlcInterface(src, dst)
            with open(dst) as f:
                self.assertEqual(f.read(), "-- new\n")
            self.assertEqual(stat.S_IMODE(os.stat(dst).st_mode), 0o755)

    def test_missing_dst_created(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src.lua")
            dst = os.path.join(d, "sub", "dst.lua")
            os.makedirs(os.path.dirname(dst))
            self._write(src, "-- new\n")
            VlcPlayer._ensureVlcInterface(src, dst)
            with open(dst) as f:
                self.assertEqual(f.read(), "-- new\n")


class GetMRLTests(unittest.TestCase):
    def test_local_path_with_spaces(self):
        mrl = VlcPlayer.getMRL("/tmp/x y.mkv")
        self.assertTrue(mrl.startswith("file://"))
        self.assertNotIn(" ", mrl)

    def test_url_kept_and_quoted(self):
        mrl = VlcPlayer.getMRL("https://example.com/a b?x=1")
        self.assertTrue(mrl.startswith("https://example.com/"))
        self.assertNotIn(" ", mrl)


def _make_vlc_player():
    player = object.__new__(VlcPlayer)
    player.reactor = FakeReactor()
    player._client = FakeClient()
    player._vlcready = threading.Event()
    player._vlcclosed = threading.Event()
    player._durationAsk = threading.Event()
    player._filenameAsk = threading.Event()
    player._pathAsk = threading.Event()
    player._positionAsk = threading.Event()
    player._pausedAsk = threading.Event()
    player._listener = None
    player._vlcVersion = None
    player._filechanged = False
    player._paused = None
    player._duration = None
    player._filename = None
    player._filepath = None
    player._lastVLCPositionUpdate = None
    player.shownVLCLatencyError = False
    player._previousPreviousPosition = -2
    player._previousPosition = -1
    player._position = 0
    return player


class VlcVersionGateTests(unittest.TestCase):
    def test_too_old_version_drops_player(self):
        player = _make_vlc_player()
        player.lineReceived(b"vlc-version: 2.2.0")
        self.assertTrue(player._client.ui.errors)
        self.assertIn(False, player._client.stopped)  # drop() -> client.stop
        self.assertEqual(player._vlcVersion, "2.2.0")

    def test_supported_version_keeps_player(self):
        player = _make_vlc_player()
        player.lineReceived(b"vlc-version: 3.0.21")
        self.assertEqual(player._client.ui.errors, [])
        self.assertEqual(player._client.stopped, [])
        self.assertEqual(player._vlcVersion, "3.0.21")
        self.assertTrue(player._vlcready.is_set())

    def test_vlc4_rejected(self):
        player = _make_vlc_player()
        player.lineReceived(b"vlc-version: 4.0.0")
        self.assertTrue(player._client.ui.errors)
        self.assertIn(False, player._client.stopped)


if __name__ == "__main__":
    unittest.main()
