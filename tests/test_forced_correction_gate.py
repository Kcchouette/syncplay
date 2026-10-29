# coding: utf8
import unittest

from syncplay import constants
from syncplay.syncPolicy import ForcedCorrectionGate


class ForcedCorrectionGateTests(unittest.TestCase):
    def test_first_correction_not_suppressed(self):
        gate = ForcedCorrectionGate()
        self.assertFalse(gate.should_suppress(100.0, 4.5, 4.0))

    def test_cooldown_suppresses_non_growing(self):
        gate = ForcedCorrectionGate()
        gate.note_forced(100.0, 4.5)
        self.assertTrue(gate.should_suppress(100.5, 4.6, 4.0))
        self.assertTrue(gate.should_suppress(101.0, 4.5, 4.0))

    def test_escalation_allows_large_desync(self):
        gate = ForcedCorrectionGate()
        gate.note_forced(100.0, 4.5)
        self.assertFalse(gate.should_suppress(100.5, 9.0, 4.0))

    def test_clear_reopens_gate(self):
        gate = ForcedCorrectionGate()
        gate.note_forced(100.0, 4.5)
        gate.clear()
        self.assertFalse(gate.should_suppress(100.5, 4.6, 4.0))

    def test_growing_desync_eventually_allowed(self):
        gate = ForcedCorrectionGate()
        gate.note_forced(100.0, 4.2)
        # Under cooldown; not enough history yet to prove growth
        self.assertTrue(gate.should_suppress(100.5, 4.5, 4.0))
        # Window now shows growing desync — allow again
        self.assertFalse(gate.should_suppress(101.0, 4.8, 4.0))

    def test_history_window_respected(self):
        gate = ForcedCorrectionGate()
        for i in range(10):
            gate.should_suppress(100.0 + i, 1.0 + i * 0.2, 4.0)
        self.assertLessEqual(
            len(gate._diffHistory), constants.FORCED_CORRECTION_MIN_PERSISTENT_SAMPLES)


if __name__ == "__main__":
    unittest.main()
