"""Fan condition tests using the tach simulator.

Tests realistic fan behaviors: spin-up, wind-down, stuck, non-responsive,
and failure scenarios. All local — no hardware or broker needed.

Each test simulates a time series of duty commands and verifies that
the tach simulator + fan_controller produce expected RPM readings.
"""

import sys
import pathlib
from unittest.mock import MagicMock, call

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "pi"))
from fan_controller import set_fan_speed, calc_rpm
from tach_simulator import (
    TachSimulator,
    MAX_RPM,
    MIN_RPM,
    STALL_PCT,
    SPINUP_TAU,
    SPINDOWN_TAU,
)

pytestmark = pytest.mark.local

INTERVAL = 10  # tach sampling interval (seconds)
PWM_GPIO = 18
PWM_FREQ = 25000


def run_step(sim, mock_pi, duty_pct, dt=None):
    """Command a duty cycle, advance the simulator, return (rpm, pulses)."""
    if dt is None:
        dt = INTERVAL
    set_fan_speed(mock_pi, PWM_GPIO, PWM_FREQ, duty_pct)
    sim.set_duty(duty_pct)
    sim.advance(dt)
    pulses = sim.get_pulses(INTERVAL)
    rpm = calc_rpm(pulses, INTERVAL)
    return rpm, pulses


# ---------------------------------------------------------------------------
# Spin-up
# ---------------------------------------------------------------------------

class TestSpinUp:
    """Fan accelerates from rest to commanded speed."""

    def test_cold_start_to_50_percent(self, mock_pi):
        """From 0, commanding 50% should ramp RPM up over several seconds."""
        sim = TachSimulator()
        rpms = []
        for _ in range(10):
            rpm, _ = run_step(sim, mock_pi, 50, dt=1.0)
            rpms.append(rpm)

        # First reading should be low (fan just starting)
        assert rpms[0] < rpms[-1]
        # Final reading should be near steady state
        target = sim.target_rpm
        assert rpms[-1] == pytest.approx(target, rel=0.05)

    def test_cold_start_to_100_percent(self, mock_pi):
        """Full speed command from rest."""
        sim = TachSimulator()
        # Let it settle
        for _ in range(20):
            run_step(sim, mock_pi, 100, dt=0.5)

        assert sim.is_spinning
        assert sim.rpm == pytest.approx(MAX_RPM, rel=0.02)

    def test_step_increase(self, mock_pi):
        """Stepping from 30% to 80% should increase RPM."""
        sim = TachSimulator()
        # Settle at 30%
        for _ in range(20):
            run_step(sim, mock_pi, 30, dt=1.0)
        rpm_low = sim.rpm

        # Step to 80%
        for _ in range(20):
            run_step(sim, mock_pi, 80, dt=1.0)
        rpm_high = sim.rpm

        assert rpm_high > rpm_low * 1.5

    def test_rpms_monotonically_increase_during_spinup(self, mock_pi):
        """During spin-up, each reading should be >= the previous."""
        sim = TachSimulator()
        rpms = []
        for _ in range(15):
            rpm, _ = run_step(sim, mock_pi, 75, dt=0.5)
            rpms.append(rpm)

        for i in range(1, len(rpms)):
            assert rpms[i] >= rpms[i - 1] - 1, (
                f"RPM decreased during spin-up: {rpms[i-1]:.0f} → {rpms[i]:.0f}"
            )

    def test_below_stall_stays_at_zero(self, mock_pi):
        """Commanding below stall threshold should produce 0 RPM."""
        sim = TachSimulator()
        for _ in range(10):
            run_step(sim, mock_pi, STALL_PCT - 2, dt=1.0)

        assert not sim.is_spinning
        assert sim.get_pulses(INTERVAL) == 0


# ---------------------------------------------------------------------------
# Wind-down
# ---------------------------------------------------------------------------

class TestWindDown:
    """Fan decelerates when duty is reduced or set to 0."""

    def test_full_stop_from_100(self, mock_pi):
        """Commanding 0% from full speed should coast down to 0."""
        sim = TachSimulator()
        # Spin up to 100%
        for _ in range(20):
            run_step(sim, mock_pi, 100, dt=1.0)
        assert sim.is_spinning

        # Command stop — give enough time to coast (5 tau ≈ 15s)
        rpms = []
        for _ in range(40):
            rpm, _ = run_step(sim, mock_pi, 0, dt=0.5)
            rpms.append(rpm)

        # Should eventually stop
        assert not sim.is_spinning
        # Should have been decelerating
        assert rpms[0] > rpms[-1]

    def test_coast_time_is_realistic(self, mock_pi):
        """Fan should take a few seconds to coast to stop, not instant."""
        sim = TachSimulator()
        for _ in range(20):
            run_step(sim, mock_pi, 100, dt=1.0)

        sim.set_duty(0)
        sim.advance(0.5)
        # After 0.5s, fan should still be spinning significantly
        assert sim.rpm > MAX_RPM * 0.3

        sim.advance(20.0)
        # After ~20s total, should be stopped
        assert not sim.is_spinning

    def test_step_decrease(self, mock_pi):
        """Stepping from 80% to 20% should decrease RPM but not stop."""
        sim = TachSimulator()
        for _ in range(20):
            run_step(sim, mock_pi, 80, dt=1.0)
        rpm_high = sim.rpm

        for _ in range(20):
            run_step(sim, mock_pi, 20, dt=1.0)
        rpm_low = sim.rpm

        assert rpm_low < rpm_high
        assert rpm_low > 0  # 20% is above stall

    def test_rpms_monotonically_decrease_during_winddown(self, mock_pi):
        """During wind-down, each reading should be <= the previous."""
        sim = TachSimulator()
        for _ in range(20):
            run_step(sim, mock_pi, 100, dt=1.0)

        rpms = []
        for _ in range(20):
            rpm, _ = run_step(sim, mock_pi, 0, dt=0.5)
            rpms.append(rpm)

        for i in range(1, len(rpms)):
            assert rpms[i] <= rpms[i - 1] + 1, (
                f"RPM increased during wind-down: {rpms[i-1]:.0f} → {rpms[i]:.0f}"
            )


# ---------------------------------------------------------------------------
# Stuck fan
# ---------------------------------------------------------------------------

class TestStuck:
    """Fan is mechanically seized — 0 RPM regardless of command."""

    def test_stuck_from_start(self, mock_pi):
        """Fan stuck before any command — RPM stays 0."""
        sim = TachSimulator()
        sim.stick()

        for _ in range(10):
            rpm, _ = run_step(sim, mock_pi, 100, dt=1.0)

        assert rpm == 0
        assert not sim.is_spinning

    def test_stuck_while_running(self, mock_pi):
        """Fan seizes while spinning — RPM drops to 0 immediately."""
        sim = TachSimulator()
        for _ in range(20):
            run_step(sim, mock_pi, 80, dt=1.0)
        assert sim.is_spinning

        sim.stick()
        sim.advance(0.1)
        assert not sim.is_spinning
        assert sim.get_pulses(INTERVAL) == 0

    def test_stuck_ignores_duty_changes(self, mock_pi):
        """Changing duty on a stuck fan has no effect on RPM."""
        sim = TachSimulator()
        sim.stick()

        for pct in [0, 25, 50, 75, 100]:
            run_step(sim, mock_pi, pct, dt=1.0)
            assert sim.rpm == 0

    def test_unstick_recovers(self, mock_pi):
        """After unsticking, fan responds to commands again."""
        sim = TachSimulator()
        sim.stick()
        run_step(sim, mock_pi, 80, dt=5.0)
        assert not sim.is_spinning

        sim.unstick()
        for _ in range(20):
            run_step(sim, mock_pi, 80, dt=1.0)
        assert sim.is_spinning

    def test_failure_alert_condition(self, mock_pi):
        """Stuck fan with speed > 20% should trigger failure alert logic."""
        sim = TachSimulator()
        sim.stick()
        run_step(sim, mock_pi, 50, dt=5.0)

        rpm = calc_rpm(sim.get_pulses(INTERVAL), INTERVAL)
        duty = 50
        # This is the condition from automation_fan_failure_alert.yaml
        should_alert = rpm < 100 and duty > 20
        assert should_alert


# ---------------------------------------------------------------------------
# Non-responsive (MQTT commands not reaching fan)
# ---------------------------------------------------------------------------

class TestNonResponsive:
    """Simulates scenarios where MQTT commands don't reach the PWM output."""

    def test_duty_not_applied_rpm_stays_zero(self, mock_pi):
        """If set_fan_speed is never called, RPM stays at 0."""
        sim = TachSimulator()
        # Don't call set_duty — simulate MQTT disconnect
        for _ in range(10):
            sim.advance(1.0)

        assert not sim.is_spinning
        mock_pi.hardware_PWM.assert_not_called()

    def test_stale_duty_fan_holds_speed(self, mock_pi):
        """If MQTT stops updating, fan holds last commanded speed."""
        sim = TachSimulator()
        # Initial command
        run_step(sim, mock_pi, 60, dt=10.0)
        rpm_initial = sim.rpm

        # Simulate MQTT going silent — fan_controller keeps last duty,
        # simulator keeps running at same target
        for _ in range(10):
            sim.advance(1.0)  # no new set_duty call

        assert sim.rpm == pytest.approx(rpm_initial, rel=0.05)

    def test_zero_retained_on_reconnect(self, mock_pi):
        """If broker has retained 0, fan stops on Pi reconnect."""
        sim = TachSimulator()
        for _ in range(20):
            run_step(sim, mock_pi, 80, dt=1.0)
        assert sim.is_spinning

        # Simulate reconnect receiving retained "0"
        for _ in range(30):
            run_step(sim, mock_pi, 0, dt=0.5)
        assert not sim.is_spinning


# ---------------------------------------------------------------------------
# Failure (motor/electrical failure mid-operation)
# ---------------------------------------------------------------------------

class TestFailure:
    """Fan motor fails while running — RPM decays to 0."""

    def test_failure_while_running(self, mock_pi):
        """Fan fails at full speed — RPM decays, doesn't drop instantly."""
        sim = TachSimulator()
        for _ in range(20):
            run_step(sim, mock_pi, 100, dt=1.0)
        rpm_before = sim.rpm
        assert rpm_before > MAX_RPM * 0.9

        sim.fail()

        rpms_after = []
        for _ in range(20):
            sim.advance(1.0)
            rpms_after.append(sim.rpm)

        # RPM should decay, not drop instantly (inertia)
        assert rpms_after[0] > 0, "RPM dropped instantly — should coast"
        assert not sim.is_spinning, "Fan should have stopped by now"

        # Decay should be monotonic
        for i in range(1, len(rpms_after)):
            assert rpms_after[i] <= rpms_after[i - 1] + 1

    def test_failure_duty_still_commanded(self, mock_pi):
        """Even with fan failed, duty commands still execute (PWM pin active)."""
        sim = TachSimulator()
        for _ in range(20):
            run_step(sim, mock_pi, 100, dt=1.0)

        sim.fail()
        # Controller doesn't know about failure — keeps commanding
        set_fan_speed(mock_pi, PWM_GPIO, PWM_FREQ, 100)
        mock_pi.hardware_PWM.assert_called_with(PWM_GPIO, PWM_FREQ, 1000000)

        # RPM decays to 0
        sim.advance(20.0)
        assert not sim.is_spinning

    def test_failure_alert_triggers(self, mock_pi):
        """RPM < 100 while speed > 20% should satisfy alert condition."""
        sim = TachSimulator()
        for _ in range(20):
            run_step(sim, mock_pi, 80, dt=1.0)

        sim.fail()
        sim.advance(10.0)

        rpm = calc_rpm(sim.get_pulses(INTERVAL), INTERVAL)
        commanded_pct = 80
        assert rpm < 100
        assert commanded_pct > 20

    def test_recover_from_failure(self, mock_pi):
        """After recovering from failure, fan responds to commands again."""
        sim = TachSimulator()
        for _ in range(20):
            run_step(sim, mock_pi, 100, dt=1.0)

        sim.fail()
        sim.advance(20.0)
        assert not sim.is_spinning

        sim.recover()
        for _ in range(20):
            run_step(sim, mock_pi, 100, dt=1.0)
        assert sim.is_spinning
        assert sim.rpm == pytest.approx(MAX_RPM, rel=0.05)

    def test_intermittent_failure(self, mock_pi):
        """Fan fails, recovers, fails again — RPM tracks each transition."""
        sim = TachSimulator()

        # Run normally
        for _ in range(20):
            run_step(sim, mock_pi, 60, dt=1.0)
        assert sim.is_spinning
        rpm_normal = sim.rpm

        # First failure
        sim.fail()
        sim.advance(20.0)
        assert not sim.is_spinning

        # Recovery
        sim.recover()
        for _ in range(20):
            run_step(sim, mock_pi, 60, dt=1.0)
        assert sim.rpm == pytest.approx(rpm_normal, rel=0.1)

        # Second failure
        sim.fail()
        sim.advance(20.0)
        assert not sim.is_spinning


# ---------------------------------------------------------------------------
# Integration: full tach_state cycle
# ---------------------------------------------------------------------------

class TestTachStateIntegration:
    """Tests using the same tach_state dict format as fan_controller.py."""

    def test_inject_to_tach_state(self, mock_pi):
        """Simulator writes pulses into tach_state, calc_rpm reads them."""
        sim = TachSimulator()
        tach_state = {"pulse_count": [0]}

        sim.set_duty(50)
        sim.advance(10.0)
        sim.inject_to_tach_state(tach_state, INTERVAL)

        assert tach_state["pulse_count"][0] > 0
        rpm = calc_rpm(tach_state["pulse_count"][0], INTERVAL)
        assert rpm == pytest.approx(sim.rpm, rel=0.1)

    def test_full_run_loop_cycle(self, mock_pi):
        """Simulate what run_loop does: read pulses, reset counter, compute RPM."""
        sim = TachSimulator()
        tach_state = {"pulse_count": [0]}

        sim.set_duty(75)
        sim.advance(INTERVAL)
        sim.inject_to_tach_state(tach_state, INTERVAL)

        # Mimic run_loop logic
        count = tach_state["pulse_count"][0]
        tach_state["pulse_count"][0] = 0
        rpm = calc_rpm(count, INTERVAL)

        assert rpm > 0
        assert tach_state["pulse_count"][0] == 0  # counter was reset

    def test_multiple_intervals(self, mock_pi):
        """Multiple consecutive intervals should produce consistent RPMs at steady state."""
        sim = TachSimulator()
        tach_state = {"pulse_count": [0]}

        sim.set_duty(60)
        # Settle
        sim.advance(20.0)

        rpms = []
        for _ in range(5):
            sim.advance(INTERVAL)
            sim.inject_to_tach_state(tach_state, INTERVAL)
            count = tach_state["pulse_count"][0]
            tach_state["pulse_count"][0] = 0
            rpms.append(calc_rpm(count, INTERVAL))

        # At steady state, RPMs should be very close
        for rpm in rpms:
            assert rpm == pytest.approx(rpms[0], rel=0.02)
