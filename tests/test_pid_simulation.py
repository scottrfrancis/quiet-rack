"""PID simulation using real vault temperature history.

Replays recorded temperature data through a simple PID controller and
verifies the fan controller responds correctly. This tests the full
logic chain: temperature → PID output → set_fan_speed → PWM duty.

Uses real 7-day sensor history from sensor.vault_temperature (Fahrenheit).

Marked 'local' — runs anywhere, no hardware or broker needed.
"""

import csv
import pathlib
from unittest.mock import MagicMock

import pytest

import sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "pi"))
from fan_controller import set_fan_speed, calc_rpm

pytestmark = pytest.mark.local

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
HISTORY_CSV = FIXTURES / "vault_temp_history.csv"

# PID parameters from the design guide (converted to Fahrenheit)
SETPOINT_F = 95.0    # 35°C = 95°F
KP = 5.0
KI = 0.05
KD = 1.0
OUTPUT_MIN = 15.0
OUTPUT_MAX = 100.0
SAMPLE_TIME = 30     # seconds


def load_temperature_history():
    """Load vault temperature CSV. Returns list of (timestamp_str, temp_f) tuples."""
    if not HISTORY_CSV.exists():
        pytest.skip(f"Temperature history not found: {HISTORY_CSV}")
    with open(HISTORY_CSV) as f:
        reader = csv.DictReader(f)
        return [(row["timestamp"], float(row["temp_f"])) for row in reader]


class SimplePID:
    """Minimal PID controller matching simple_pid_controller behavior."""

    def __init__(self, kp, ki, kd, setpoint, output_min, output_max):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.setpoint = setpoint
        self.output_min = output_min
        self.output_max = output_max
        self._integral = 0.0
        self._last_error = None

    def update(self, measurement, dt):
        error = measurement - self.setpoint
        self._integral += error * dt
        # Anti-windup: clamp integral
        self._integral = max(
            self.output_min / max(self.ki, 1e-9),
            min(self.output_max / max(self.ki, 1e-9), self._integral),
        )

        derivative = 0.0
        if self._last_error is not None and dt > 0:
            derivative = (error - self._last_error) / dt
        self._last_error = error

        output = self.kp * error + self.ki * self._integral + self.kd * derivative
        return max(self.output_min, min(self.output_max, output))


class TestPIDSimulation:
    """Replay real temperature history through PID → fan_controller."""

    def test_history_loads(self):
        """Verify we have temperature data to work with."""
        history = load_temperature_history()
        assert len(history) > 100, f"Expected 100+ data points, got {len(history)}"

    def test_temperature_range_sane(self):
        """Sanity check: vault temps should be in a reasonable range (°F)."""
        history = load_temperature_history()
        temps = [t for _, t in history]
        assert min(temps) > 50, f"Min temp {min(temps)}°F seems too low"
        assert max(temps) < 200, f"Max temp {max(temps)}°F seems too high"

    def test_pid_output_within_bounds(self):
        """PID output should always stay within [OUTPUT_MIN, OUTPUT_MAX]."""
        history = load_temperature_history()
        pid = SimplePID(KP, KI, KD, SETPOINT_F, OUTPUT_MIN, OUTPUT_MAX)

        for i in range(1, len(history)):
            _, temp = history[i]
            output = pid.update(temp, SAMPLE_TIME)
            assert OUTPUT_MIN <= output <= OUTPUT_MAX, (
                f"PID output {output} out of bounds at step {i}, temp={temp}°F"
            )

    def test_fan_speed_set_for_each_pid_output(self, mock_pi):
        """Every PID output should produce a valid hardware_PWM call."""
        history = load_temperature_history()
        pid = SimplePID(KP, KI, KD, SETPOINT_F, OUTPUT_MIN, OUTPUT_MAX)

        for i in range(1, len(history)):
            _, temp = history[i]
            output = pid.update(temp, SAMPLE_TIME)
            result = set_fan_speed(mock_pi, 18, 25000, output)
            assert 0 <= result <= 100

        # Should have been called for every data point
        assert mock_pi.hardware_PWM.call_count == len(history) - 1

    def test_all_temps_above_setpoint_means_fan_active(self):
        """Since vault temps (~120-130°F) are well above setpoint (95°F),
        PID should command max fan speed for essentially all data points."""
        history = load_temperature_history()
        pid = SimplePID(KP, KI, KD, SETPOINT_F, OUTPUT_MIN, OUTPUT_MAX)

        max_speed_count = 0
        total = 0
        for i in range(1, len(history)):
            _, temp = history[i]
            output = pid.update(temp, SAMPLE_TIME)
            if output >= 99.0:
                max_speed_count += 1
            total += 1

        # With temps 25-35°F above setpoint, PID should saturate quickly
        assert max_speed_count > total * 0.8, (
            f"Expected >80% at max speed, got {max_speed_count}/{total} "
            f"({100*max_speed_count/total:.0f}%)"
        )

    def test_pid_reacts_to_temp_drop(self, mock_pi):
        """Simulate a temperature drop below setpoint and verify PID reduces fan speed.

        After running hot (130°F), temperature drops below setpoint (to 90°F).
        The negative error unwinds the integral and the output should settle
        to output_min, since there's no reason to run the fan hard when cool.
        """
        pid = SimplePID(KP, KI, KD, SETPOINT_F, OUTPUT_MIN, OUTPUT_MAX)

        # Warm up at 130°F — builds integral
        for _ in range(10):
            pid.update(130.0, SAMPLE_TIME)

        # Temperature drops below setpoint — negative error unwinds integral
        outputs_at_low_temp = []
        for _ in range(500):
            output = pid.update(90.0, SAMPLE_TIME)
            outputs_at_low_temp.append(output)

        # After sustained below-setpoint temp, output should settle to minimum
        final_outputs = outputs_at_low_temp[-10:]
        avg = sum(final_outputs) / len(final_outputs)
        assert avg <= OUTPUT_MIN + 1, (
            f"Expected fan near minimum when below setpoint, got avg={avg:.1f}%"
        )

    def test_duty_cycle_values_correct(self, mock_pi):
        """Verify the actual duty cycle values passed to hardware_PWM."""
        history = load_temperature_history()[:20]  # first 20 points
        pid = SimplePID(KP, KI, KD, SETPOINT_F, OUTPUT_MIN, OUTPUT_MAX)

        for i in range(1, len(history)):
            _, temp = history[i]
            output = pid.update(temp, SAMPLE_TIME)
            set_fan_speed(mock_pi, 18, 25000, output)

            # Verify the duty cycle calculation
            call_args = mock_pi.hardware_PWM.call_args
            duty = call_args[0][2]
            assert 0 <= duty <= 1000000, f"Duty {duty} out of pigpio range"
            assert duty % 10000 == 0, f"Duty {duty} not a multiple of 10000"
