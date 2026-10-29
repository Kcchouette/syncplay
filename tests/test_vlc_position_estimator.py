# coding: utf8
import unittest

from syncplay.players.vlcPositionEstimator import (
    VlcPositionEstimator,
    sanitize_vlc_title_offset,
)


class VlcPositionEstimatorTests(unittest.TestCase):
    def test_first_sample_accepted(self):
        est = VlcPositionEstimator()
        result = est.observe(10.0, 100.0, paused=False)
        self.assertTrue(result.accepted)
        self.assertAlmostEqual(est.position_at(100.0), 10.0)

    def test_smooth_playback_tracks_one_to_one(self):
        est = VlcPositionEstimator()
        est.observe(10.0, 100.0, paused=False)
        for i in range(1, 11):
            now = 100.0 + i
            est.observe(10.0 + i, now, paused=False)
            self.assertAlmostEqual(est.reported_position_at(now), 10.0 + i, places=2)

    def test_quanta_noise_does_not_false_seek(self):
        est = VlcPositionEstimator()
        est.observe(10.0, 100.0, paused=False)
        # VLC reports same second twice then advances by 1s (coarse quantum)
        r = est.observe(10.0, 100.4, paused=False)
        self.assertFalse(r.is_seek)
        r = est.observe(11.0, 100.9, paused=False)
        self.assertFalse(r.is_seek)
        self.assertTrue(r.accepted)

    def test_real_seek_jump_resets(self):
        est = VlcPositionEstimator()
        est.observe(10.0, 100.0, paused=False)
        r = est.observe(40.0, 101.0, paused=False)
        self.assertTrue(r.is_seek)
        self.assertTrue(r.accepted)
        self.assertAlmostEqual(est.position_at(101.0), 40.0)

    def test_rate_clamped_on_impossible_jump(self):
        est = VlcPositionEstimator()
        est.observe(0.0, 0.0, paused=False)
        # 2s wall, 3s vlc → rate 1.5 before clamp path; stays under seek threshold
        est.observe(3.0, 2.0, paused=False)
        self.assertLessEqual(est.rate(), 1.25)
        self.assertGreaterEqual(est.rate(), 0.75)

    def test_freeze_internal_holds_anchor(self):
        est = VlcPositionEstimator()
        est.observe(10.0, 100.0, paused=False)
        est.observe(10.0, 100.5, paused=False)
        r = est.observe(10.0, 103.0, paused=False)
        self.assertTrue(r.is_frozen)
        self.assertTrue(est.is_frozen())
        self.assertAlmostEqual(est.position_at(104.0), 10.0)

    def test_freeze_does_not_stall_reported_position(self):
        est = VlcPositionEstimator()
        est.observe(10.0, 100.0, paused=False)
        est.observe(10.0, 100.5, paused=False)
        est.observe(10.0, 103.0, paused=False)
        self.assertTrue(est.is_frozen())
        reported = est.reported_position_at(105.0, paused=False)
        self.assertGreater(reported, 10.0)

    def test_freeze_needs_min_samples(self):
        est = VlcPositionEstimator()
        est.observe(10.0, 100.0, paused=False)
        r = est.observe(10.0, 103.0, paused=False)
        # Only one frozen sample after initial — not enough for freeze flag yet
        self.assertFalse(r.is_frozen)

    def test_freeze_cleared_by_rate_bearing_sample(self):
        est = VlcPositionEstimator()
        est.observe(10.0, 100.0, paused=False)
        est.observe(10.0, 100.5, paused=False)
        est.observe(10.0, 103.0, paused=False)
        self.assertTrue(est.is_frozen())
        r = est.observe(13.0, 103.5, paused=False)
        self.assertFalse(r.is_frozen)
        self.assertFalse(est.is_frozen())

    def test_near_eof_is_not_freeze(self):
        est = VlcPositionEstimator()
        est.set_duration_hint(100.0)
        est.observe(98.5, 100.0, paused=False)
        est.observe(98.5, 100.5, paused=False)
        r = est.observe(98.5, 103.0, paused=False)
        self.assertFalse(r.is_frozen)

    def test_paused_resets_to_sample(self):
        est = VlcPositionEstimator()
        est.observe(50.0, 200.0, paused=False)
        est.observe(50.2, 250.0, paused=True)
        self.assertAlmostEqual(est.position_at(250.0), 50.2)
        self.assertAlmostEqual(est.reported_position_at(260.0, paused=True), 50.2)


class SanitizeTitleOffsetTests(unittest.TestCase):
    def test_strips_one_title_week(self):
        # Live repro: VLC reported 610582.443 for a 5795s file
        self.assertAlmostEqual(
            sanitize_vlc_title_offset(610582.443, 5795.14), 5782.443, places=3)

    def test_keeps_in_range_position(self):
        self.assertEqual(sanitize_vlc_title_offset(100.0, 5795.14), 100.0)

    def test_no_duration_returns_original(self):
        self.assertEqual(sanitize_vlc_title_offset(610582.443, None), 610582.443)
        self.assertEqual(sanitize_vlc_title_offset(610582.443, 0), 610582.443)

    def test_unfixable_returns_original(self):
        # Beyond duration after week strip still (tiny duration edge)
        self.assertEqual(sanitize_vlc_title_offset(1000.0, 1.0), 1000.0)


if __name__ == "__main__":
    unittest.main()
