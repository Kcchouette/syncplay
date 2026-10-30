"""T7: every player in the registry exposes the capability contract."""
import unittest

from syncplay.players import getAvailablePlayers
from syncplay.players.basePlayer import BasePlayer, DummyPlayer

CAPABILITY_FLAGS = (
    "speedSupported",
    "chatOSDSupported",
    "alertOSDSupported",
    "customOpenDialog",
    "osdMessageSeparator",
)


class CapabilityFlagsTests(unittest.TestCase):
    def test_base_player_declares_all_flags(self):
        for flag in CAPABILITY_FLAGS:
            self.assertTrue(hasattr(BasePlayer, flag), flag)

    def test_every_registered_player_exposes_flags(self):
        # Includes DummyPlayer substitutions (pywin32/IINA absent on Linux).
        for player in getAvailablePlayers():
            for flag in CAPABILITY_FLAGS:
                self.assertTrue(hasattr(player, flag),
                                "{} missing {}".format(player.__name__, flag))

    def test_dummy_player_defaults(self):
        self.assertIs(DummyPlayer.speedSupported, False)
        self.assertIs(DummyPlayer.chatOSDSupported, False)
        self.assertIs(DummyPlayer.alertOSDSupported, False)
        self.assertIs(DummyPlayer.customOpenDialog, False)
        self.assertIsInstance(DummyPlayer.osdMessageSeparator, str)

    def test_known_player_capabilities(self):
        from syncplay.players.mplayer import MplayerPlayer
        from syncplay.players.mpv import MpvPlayer
        from syncplay.players.vlc import VlcPlayer

        self.assertIs(MpvPlayer.speedSupported, True)
        self.assertIs(MpvPlayer.chatOSDSupported, True)
        self.assertIs(VlcPlayer.speedSupported, True)
        self.assertIs(VlcPlayer.chatOSDSupported, False)
        self.assertIs(MplayerPlayer.chatOSDSupported, False)

    def test_open_custom_open_dialog_is_callable_instance_style(self):
        # Regression: it used to be a staticmethod taking `self`, which
        # the GUI's zero-arg instance calls would have rejected.
        import inspect
        self.assertFalse(isinstance(
            inspect.getattr_static(BasePlayer, "openCustomOpenDialog"),
            staticmethod))


if __name__ == "__main__":
    unittest.main()
