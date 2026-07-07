import pytest

from blaufilter.tracker import PositionTracker, modular_diff


class TestModularDiff:
    def test_plain_difference(self):
        assert modular_diff(5.0, 3.0, 100.0) == pytest.approx(2.0)
        assert modular_diff(3.0, 5.0, 100.0) == pytest.approx(-2.0)

    def test_wrap_around(self):
        # 0.3s and 9.8s of a 10s loop are 0.5s apart, not 9.5s
        assert modular_diff(0.3, 9.8, 10.0) == pytest.approx(0.5)
        assert modular_diff(9.8, 0.3, 10.0) == pytest.approx(-0.5)

    def test_zero(self):
        assert modular_diff(7.0, 7.0, 10.0) == pytest.approx(0.0)


class TestPositionTracker:
    def test_no_estimate_before_boundary(self):
        t = PositionTracker()
        t.observe(100.0, 42)
        assert t.est_position(100.5, 1.0) is None

    def test_boundary_sampling(self):
        t = PositionTracker()
        t.observe(100.0, 42)
        t.observe(100.4, 42)
        t.observe(100.7, 43)  # increment: position is exactly 43.0 at wallclock 100.7
        assert t.est_position(101.2, 1.0) == pytest.approx(43.5)

    def test_extrapolation_respects_rate(self):
        t = PositionTracker()
        t.observe(100.0, 42)
        t.observe(100.5, 43)
        assert t.est_position(101.0, 2.0) == pytest.approx(44.0)
        assert t.est_position(101.0, 0.5) == pytest.approx(43.25)

    def test_gap_invalidates_calibration(self):
        t = PositionTracker()
        t.observe(100.0, 42)
        t.observe(100.5, 43)
        assert t.est_position(100.6, 1.0) is not None
        t.observe(101.0, 50)  # seek jump
        assert t.est_position(101.1, 1.0) is None

    def test_backward_jump_invalidates_calibration(self):
        t = PositionTracker()
        t.observe(100.0, 42)
        t.observe(100.5, 43)
        t.observe(101.0, 1)  # loop wrap
        assert t.est_position(101.1, 1.0) is None

    def test_recalibrates_after_jump(self):
        t = PositionTracker()
        t.observe(100.0, 42)
        t.observe(100.5, 43)
        t.observe(101.0, 1)
        t.observe(101.5, 2)  # clean increment again
        assert t.est_position(102.0, 1.0) == pytest.approx(2.5)

    def test_none_value_resets(self):
        t = PositionTracker()
        t.observe(100.0, 42)
        t.observe(100.5, 43)
        t.observe(101.0, None)
        assert t.est_position(101.1, 1.0) is None

    def test_reset(self):
        t = PositionTracker()
        t.observe(100.0, 42)
        t.observe(100.5, 43)
        t.reset()
        assert t.est_position(100.6, 1.0) is None
