# coding: utf8
"""Forced rewind/fast-forward dampening policy (pure, unit-testable)."""

from syncplay import constants


class ForcedCorrectionGate(object):
    """Suppresses rapid forced desync corrections (seek-storm prevention).

    Does not apply to explicit doSeek; callers must only consult this for
    desync-driven rewind/fast-forward. Soft slowdown is never gated.
    """

    def __init__(self):
        self._lastForcedAt = None
        self._diffHistory = []

    def clear(self):
        self._lastForcedAt = None
        self._diffHistory = []

    def note_forced(self, now, abs_diff):
        self._lastForcedAt = float(now)
        self._diffHistory = [float(abs_diff)]

    def record_diff(self, abs_diff):
        self._diffHistory.append(float(abs_diff))
        if len(self._diffHistory) > constants.FORCED_CORRECTION_MIN_PERSISTENT_SAMPLES:
            self._diffHistory = self._diffHistory[-constants.FORCED_CORRECTION_MIN_PERSISTENT_SAMPLES:]

    def should_suppress(self, now, abs_diff, threshold):
        abs_diff = float(abs_diff)
        threshold = float(threshold)
        self.record_diff(abs_diff)

        if abs_diff >= constants.FORCED_CORRECTION_ESCALATION_FACTOR * threshold:
            return False

        if (
            self._lastForcedAt is not None and
            (float(now) - self._lastForcedAt) < constants.FORCED_CORRECTION_COOLDOWN
        ):
            # Suppress the bias/stutter loop unless desync is clearly growing.
            if not self._desyncGrowing():
                return True
            if len(self._diffHistory) < constants.FORCED_CORRECTION_MIN_PERSISTENT_SAMPLES:
                return True

        return False

    def _desyncGrowing(self):
        needed = constants.FORCED_CORRECTION_MIN_PERSISTENT_SAMPLES
        if len(self._diffHistory) < needed:
            return False
        # Strictly non-decreasing over the window with a small growth margin.
        for i in range(1, len(self._diffHistory)):
            if self._diffHistory[i] + 0.05 < self._diffHistory[i - 1]:
                return False
        return self._diffHistory[-1] > self._diffHistory[0]
