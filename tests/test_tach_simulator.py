"""Tests for the tach simulator itself — validates the physical model."""

import pytest
from tach_simulator import TachSimulator, duty_to_steady_state_rpm, STALL_PCT, MAX_RPM, MIN_RPM

pytestmark = pytest.mark.local


class TestDutyToRpm:
    def test_zero_duty(self):
        assert duty_to_steady_state_rpm(0) == 0.0

    def test_below_stall(self):
        assert duty_to_steady_state_rpm(STALL_PCT - 1) == 0.0

    def test_at_stall(self):
        assert duty_to_steady_state_rpm(STALL_PCT) == pytest.approx(MIN_RPM, abs=1)

    def test_full_duty(self):
        assert duty_to_steady_state_rpm(100) == pytest.approx(MAX_RPM, abs=1)

    def test_midpoint(self):
        mid = (STALL_PCT + 100) / 2
        expected = (MIN_RPM + MAX_RPM) / 2
        assert duty_to_steady_state_rpm(mid) == pytest.approx(expected, abs=1)

    def test_monotonic(self):
        rpms = [duty_to_steady_state_rpm(p) for p in range(0, 101)]
        for i in range(1, len(rpms)):
            assert rpms[i] >= rpms[i - 1]


class TestSimulatorBasics:
    def test_starts_at_zero(self):
        sim = TachSimulator()
        assert sim.rpm == 0.0
        assert not sim.is_spinning
        assert sim.get_pulses(10) == 0

    def test_set_duty_updates_target(self):
        sim = TachSimulator()
        sim.set_duty(50)
        assert sim.target_rpm > 0

    def test_clamps_duty(self):
        sim = TachSimulator()
        sim.set_duty(150)
        assert sim.duty_pct == 100
        sim.set_duty(-10)
        assert sim.duty_pct == 0
