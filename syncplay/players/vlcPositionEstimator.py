# coding: utf8
"""Pure VLC position clock model for Syncplay.

No Twisted / player / client imports so unit tests stay lightweight.
All time arguments are injected wall-clock seconds.
"""

from syncplay import constants


def sanitize_vlc_title_offset(position, duration):
    """Strip VLC Lua title×week offsets that land far beyond media duration."""
    if duration is None or duration <= 0:
        return position
    limit = duration + constants.VLC_POSITION_OVER_DURATION_TOLERANCE
    if position <= limit:
        return position
    week = constants.VLC_TITLE_WEEK_SECONDS
    sanitized = position
    while sanitized > limit and sanitized >= week:
        sanitized -= week
    if sanitized > limit:
        return position
    return sanitized


class EstimateResult(object):
    __slots__ = ("accepted", "is_seek", "is_frozen")

    def __init__(self, accepted=False, is_seek=False, is_frozen=False):
        self.accepted = accepted
        self.is_seek = is_seek
        self.is_frozen = is_frozen


class VlcPositionEstimator(object):
    """Smooths coarse VLC time samples and freezes internal state on rebuffer.

    ``reported_position_at`` always wall-advances while playing so a frozen
    internal estimate never stalls the position sent to the room (server
    min() would otherwise let a stalled VLC drag every peer).
    """

    def __init__(self):
        self._anchor_pos = 0.0
        self._anchor_wall = 0.0
        self._rate = 1.0
        self._has_sample = False
        self._frozen = False
        self._freeze_started_wall = None
        self._freeze_sample_count = 0
        self._duration_hint = None

    def reset(self, position, now):
        self._anchor_pos = max(float(position), 0.0)
        self._anchor_wall = float(now)
        self._rate = 1.0
        self._has_sample = True
        self._frozen = False
        self._freeze_started_wall = None
        self._freeze_sample_count = 0

    def has_sample(self):
        return self._has_sample

    def rate(self):
        return self._rate

    def is_frozen(self):
        return self._frozen

    def set_duration_hint(self, duration):
        if duration is None:
            self._duration_hint = None
            return
        try:
            self._duration_hint = float(duration)
        except (TypeError, ValueError):
            self._duration_hint = None

    def observe(self, vlc_position, now, paused):
        result = EstimateResult()
        vlc_position = float(vlc_position)
        now = float(now)

        if paused:
            self.reset(vlc_position, now)
            result.accepted = True
            return result

        if not self._has_sample:
            self.reset(vlc_position, now)
            result.accepted = True
            return result

        dt_wall = now - self._anchor_wall
        if dt_wall < constants.VLC_MIN_SAMPLE_GAP:
            result.is_frozen = self._frozen
            return result

        dt_vlc = vlc_position - self._anchor_pos

        near_eof = (
            self._duration_hint is not None and
            self._duration_hint > 0 and
            (self._duration_hint - vlc_position) < constants.VLC_EOF_DURATION_THRESHOLD and
            vlc_position >= 0
        )

        # Stalled VLC time while "playing" is freeze, not a seek jump, even if
        # wall clock has advanced a lot (abs(dt_vlc - dt_wall) would be large).
        if abs(dt_vlc) < constants.VLC_POSITION_FREEZE_EPSILON and not near_eof:
            self._freeze_sample_count += 1
            if self._freeze_started_wall is None:
                self._freeze_started_wall = self._anchor_wall
            freeze_duration = now - self._freeze_started_wall
            if (
                self._freeze_sample_count >= constants.VLC_POSITION_FREEZE_MIN_SAMPLES and
                freeze_duration > constants.VLC_POSITION_FREEZE_THRESHOLD
            ):
                self._frozen = True
            result.accepted = True
            result.is_frozen = self._frozen
            return result

        if abs(dt_vlc - dt_wall) > constants.VLC_SEEK_JUMP_THRESHOLD:
            self.reset(vlc_position, now)
            result.accepted = True
            result.is_seek = True
            return result

        sample_rate = dt_vlc / dt_wall
        sample_rate = max(constants.VLC_RATE_MIN, min(constants.VLC_RATE_MAX, sample_rate))
        self._rate = (
            (1.0 - constants.VLC_RATE_EMA_ALPHA) * self._rate +
            constants.VLC_RATE_EMA_ALPHA * sample_rate
        )
        self._anchor_pos = max(vlc_position, 0.0)
        self._anchor_wall = now
        self._frozen = False
        self._freeze_started_wall = None
        self._freeze_sample_count = 0
        result.accepted = True
        return result

    def position_at(self, now):
        if not self._has_sample:
            return 0.0
        if self._frozen:
            return self._anchor_pos
        elapsed = float(now) - self._anchor_wall
        return max(self._anchor_pos + self._rate * elapsed, 0.0)

    def reported_position_at(self, now, paused=False):
        if not self._has_sample:
            return 0.0
        if paused:
            return self._anchor_pos
        if self._frozen and self._freeze_started_wall is not None:
            # Keep advancing on the wire even while the internal clock is held.
            return max(self._anchor_pos + (float(now) - self._freeze_started_wall), 0.0)
        elapsed = float(now) - self._anchor_wall
        return max(self._anchor_pos + self._rate * elapsed, 0.0)
