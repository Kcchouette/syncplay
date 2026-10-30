"""T6: mplayer2 slave-protocol answer parsing (pure, no player process)."""
import unittest

from syncplay.players.mplayer import MplayerPlayer


class MplayerAnswerParseTests(unittest.TestCase):
    RE = MplayerPlayer.RE_ANSWER

    @staticmethod
    def _groups(match):
        return [g for g in match.groups() if g]

    def test_time_pos(self):
        match = self.RE.match("ANS_time_pos=12.5")
        self.assertIsNotNone(match)
        self.assertEqual(self._groups(match), ["time_pos", "12.5"])

    def test_length(self):
        match = self.RE.match("ANS_length=3600.0")
        self.assertIsNotNone(match)
        self.assertEqual(self._groups(match), ["length", "3600.0"])

    def test_pause(self):
        match = self.RE.match("ANS_pause=yes")
        self.assertEqual(self._groups(match), ["pause", "yes"])

    def test_exiting_end_of_file(self):
        match = self.RE.match("Exiting... (End of file)")
        self.assertEqual(self._groups(match), ["Exiting", "End of file"])

    def test_exiting_quit(self):
        match = self.RE.match("Exiting... (Quit)")
        self.assertEqual(self._groups(match), ["Exiting", "Quit"])

    def test_garbage_rejected(self):
        self.assertIsNone(self.RE.match("busy bee"))
        self.assertIsNone(self.RE.match(""))
        self.assertIsNone(self.RE.match("MPlayer 1.5"))


if __name__ == "__main__":
    unittest.main()
