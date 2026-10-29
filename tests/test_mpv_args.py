"""T1: startup-args builders must not mutate shared module-level constants.

The config `playerArgs` list (passed by reference from client.py:898) and the
`constants.MPV_ARGS` dict are shared across launches; adapters must copy.
"""
import unittest
from unittest import mock

from syncplay import constants
from syncplay.players.memento import MementoPlayer
from syncplay.players.mpv import MpvPlayer

try:
    from syncplay.players.mpvnet import MpvnetPlayer
    HAS_MPVNET = True
except ImportError:  # pragma: no cover
    HAS_MPVNET = False

try:
    from syncplay.players import mpcbe
    HAS_MPCBE = True
except ImportError:  # pywin32 missing (non-Windows CI)
    HAS_MPCBE = False


class MpvStartupArgsTests(unittest.TestCase):
    def setUp(self):
        self._original_mpv_args = dict(constants.MPV_ARGS)

    def tearDown(self):
        constants.MPV_ARGS.clear()
        constants.MPV_ARGS.update(self._original_mpv_args)

    def test_get_startup_args_returns_new_dict(self):
        result = MpvPlayer.getStartupArgs(None)
        self.assertIsNot(result, constants.MPV_ARGS)

    def test_get_startup_args_does_not_mutate_constant(self):
        MpvPlayer.getStartupArgs(["--foo=bar"])
        self.assertEqual(constants.MPV_ARGS, self._original_mpv_args)
        self.assertNotIn("script", constants.MPV_ARGS)
        self.assertNotIn("foo", constants.MPV_ARGS)

    def test_get_startup_args_is_idempotent(self):
        first = MpvPlayer.getStartupArgs(["--foo=bar"])
        second = MpvPlayer.getStartupArgs(["--foo=bar"])
        self.assertEqual(first, second)

    def test_script_path_injected(self):
        result = MpvPlayer.getStartupArgs(None)
        self.assertIn("script", result)
        self.assertTrue(result["script"].endswith("syncplayintf.lua"))

    def test_user_args_merge(self):
        result = MpvPlayer.getStartupArgs(["--foo=bar", "-x", '--label="quoted"'])
        self.assertEqual(result.get("foo"), "bar")
        self.assertEqual(result.get("x"), "")
        self.assertEqual(result.get("label"), "quoted")

    def test_empty_user_arg_skipped(self):
        result = MpvPlayer.getStartupArgs(["--", "-"])
        self.assertNotIn("", result)


class MementoStartupArgsTests(unittest.TestCase):
    def setUp(self):
        self._original_mpv_args = dict(constants.MPV_ARGS)

    def tearDown(self):
        constants.MPV_ARGS.clear()
        constants.MPV_ARGS.update(self._original_mpv_args)

    def test_memento_uses_scripts_key(self):
        result = MementoPlayer.getStartupArgs(None)
        self.assertIn("scripts", result)
        self.assertNotIn("script", result)

    def test_memento_does_not_mutate_constant(self):
        MementoPlayer.getStartupArgs(None)
        self.assertEqual(constants.MPV_ARGS, self._original_mpv_args)
        self.assertNotIn("scripts", constants.MPV_ARGS)


@unittest.skipUnless(HAS_MPVNET, "mpvnet module unavailable")
class MpvnetRunArgsTests(unittest.TestCase):
    def test_run_does_not_mutate_caller_args(self):
        user_args = ["--keep-open=no"]
        snapshot = list(user_args)
        with mock.patch.object(MpvnetPlayer, "__init__", return_value=None), \
                mock.patch.object(MpvnetPlayer, "getExpandedPath", side_effect=lambda p: p):
            MpvnetPlayer.run(mock.Mock(), "mpvnet", None, user_args)
        self.assertEqual(user_args, snapshot)
        self.assertNotIn("auto-load-folder", user_args)

    def test_run_adds_complete_flag(self):
        captured = {}

        def capture_init(client, path, file_path, args):
            captured["args"] = list(args)

        with mock.patch.object(MpvnetPlayer, "__init__", side_effect=capture_init), \
                mock.patch.object(MpvnetPlayer, "getExpandedPath", side_effect=lambda p: p):
            MpvnetPlayer.run(mock.Mock(), "mpvnet", None, [])
        self.assertIn("--auto-load-folder=no", captured["args"])


@unittest.skipUnless(HAS_MPCBE, "pywin32 unavailable (non-Windows)")
class MpcBeRunArgsTests(unittest.TestCase):
    def test_run_copies_args_and_appends_mpc_flags(self):
        user_args = ["/play"]
        snapshot = list(user_args)
        fake_client = mock.Mock()
        original_run = mpcbe.MpcBePlayer.run
        with mock.patch.object(mpcbe, "MpcBePlayer") as mock_cls:
            original_run(fake_client, "mpc-be.exe", None, user_args)
        self.assertEqual(user_args, snapshot)
        started_args = mock_cls.return_value._mpcApi.startMpc.call_args[0][1]
        self.assertEqual(started_args, ["/play", "/open", "/new"])
        self.assertIsNot(started_args, user_args)


if __name__ == "__main__":
    unittest.main()
