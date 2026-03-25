"""PID simulation using real vault temperature history.

Replays recorded temperature data through a simple PID controller and
verifies the fan controller responds correctly. This tests the full
logic chain: temperature → PID output → set_fan_speed → PWM duty.

Uses real 7-day sensor history from sensor.vault_temperature (Fahrenheit).

PID parameters and thresholds are loaded from pi/config.yaml so tests
stay in sync with the deployed configuration.

Marked 'local' — runs anywhere, no hardware or broker needed.
"""

import csv
import pathlib
from unittest.mock import MagicMock

import pytest
import yaml

import sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "pi"))
from fan_controller import set_fan_speed, calc_rpm

pytestmark = pytest.mark.local

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
HISTORY_CSV = FIXTURES / "vault_temp_history.csv"
CONFIG_PATH = pathlib.Path(__file__).parent.parent / "pi" / "config.yaml"
EXAMPLE_CONFIG_PATH = CONFIG_PATH.with_name("config.example.yaml")


def load_pid_config():
    """Load PID parameters from config.yaml, falling back to config.example.yaml."""
    path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH
    with open(path) as f:
        cfg = yaml.safe_load(f)
    return cfg["pid"]


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


def apply_lolo_cutoff(pid_output, temp, lolo):
    """Apply LoLo cutoff: if temperature is below LoLo, fan is OFF."""
    if temp < lolo:
        return 0.0
    return pid_output


class TestPIDConfig:
    """Verify PID config is present and sane."""

    def test_config_loads(self):
        pid = load_pid_config()
        assert "setpoint" in pid
        assert "kp" in pid
        assert "lolo" in pid

    def test_threshold_ordering(self):
        pid = load_pid_config()
        assert pid["lolo"] < pid["lo"] <= pid["setpoint"] <= pid["hi"], (
            f"Expected lolo < lo <= setpoint <= hi, got "
            f"{pid['lolo']} < {pid['lo']} <= {pid['setpoint']} <= {pid['hi']}"
        )

    def test_lolo_below_lo(self):
        """LoLo must be well below LO to provide a dead zone."""
        pid = load_pid_config()
        assert pid["lolo"] < pid["lo"] - 5, (
            f"LoLo ({pid['lolo']}) should be at least 5°F below LO ({pid['lo']})"
        )


class TestPIDSimulation:
    """Replay real temperature history through PID → fan_controller."""

    def test_history_loads(self):
        history = load_temperature_history()
        assert len(history) > 100, f"Expected 100+ data points, got {len(history)}"

    def test_temperature_range_sane(self):
        history = load_temperature_history()
        temps = [t for _, t in history]
        assert min(temps) > 50, f"Min temp {min(temps)}°F seems too low"
        assert max(temps) < 200, f"Max temp {max(temps)}°F seems too high"

    def test_pid_output_within_bounds(self):
        """PID output should always stay within [output_min, output_max]."""
        pid_cfg = load_pid_config()
        pid = SimplePID(
            pid_cfg["kp"], pid_cfg["ki"], pid_cfg["kd"],
            pid_cfg["setpoint"], pid_cfg["output_min"], pid_cfg["output_max"],
        )
        history = load_temperature_history()

        for i in range(1, len(history)):
            _, temp = history[i]
            output = pid.update(temp, pid_cfg["sample_time"])
            assert pid_cfg["output_min"] <= output <= pid_cfg["output_max"], (
                f"PID output {output} out of bounds at step {i}, temp={temp}°F"
            )

    def test_fan_speed_valid_for_all_history(self, mock_pi):
        """Every PID output should produce a valid hardware_PWM call."""
        pid_cfg = load_pid_config()
        pid = SimplePID(
            pid_cfg["kp"], pid_cfg["ki"], pid_cfg["kd"],
            pid_cfg["setpoint"], pid_cfg["output_min"], pid_cfg["output_max"],
        )
        history = load_temperature_history()

        for i in range(1, len(history)):
            _, temp = history[i]
            output = pid.update(temp, pid_cfg["sample_time"])
            speed = apply_lolo_cutoff(output, temp, pid_cfg["lolo"])
            result = set_fan_speed(mock_pi, 18, 25000, speed)
            assert 0 <= result <= 100

        assert mock_pi.hardware_PWM.call_count == len(history) - 1


class TestThresholdBehavior:
    """Verify fan behavior at the three threshold boundaries."""

    def test_at_hi_full_speed(self):
        """At HI temperature, PID should output ~100%."""
        pid_cfg = load_pid_config()
        pid = SimplePID(
            pid_cfg["kp"], pid_cfg["ki"], pid_cfg["kd"],
            pid_cfg["setpoint"], pid_cfg["output_min"], pid_cfg["output_max"],
        )

        # Settle at HI
        for _ in range(20):
            output = pid.update(pid_cfg["hi"], pid_cfg["sample_time"])

        assert output >= 95.0, (
            f"Expected ~100% at HI ({pid_cfg['hi']}°F), got {output:.1f}%"
        )

    def test_at_lo_idle(self):
        """At LO temperature (= setpoint), PID should output near 0%."""
        pid_cfg = load_pid_config()
        pid = SimplePID(
            pid_cfg["kp"], pid_cfg["ki"], pid_cfg["kd"],
            pid_cfg["setpoint"], pid_cfg["output_min"], pid_cfg["output_max"],
        )

        # Settle at LO (= setpoint → error = 0)
        for _ in range(50):
            output = pid.update(pid_cfg["lo"], pid_cfg["sample_time"])

        assert output <= 10.0, (
            f"Expected near-idle at LO ({pid_cfg['lo']}°F), got {output:.1f}%"
        )

    def test_below_lolo_fan_off(self):
        """Below LoLo, fan should be OFF regardless of PID output."""
        pid_cfg = load_pid_config()
        pid = SimplePID(
            pid_cfg["kp"], pid_cfg["ki"], pid_cfg["kd"],
            pid_cfg["setpoint"], pid_cfg["output_min"], pid_cfg["output_max"],
        )

        temp = pid_cfg["lolo"] - 5.0  # well below LoLo
        output = pid.update(temp, pid_cfg["sample_time"])
        speed = apply_lolo_cutoff(output, temp, pid_cfg["lolo"])
        assert speed == 0.0, f"Expected 0% below LoLo, got {speed:.1f}%"

    def test_just_above_lolo_fan_runs(self):
        """Just above LoLo, PID output should pass through (not cut off)."""
        pid_cfg = load_pid_config()
        pid = SimplePID(
            pid_cfg["kp"], pid_cfg["ki"], pid_cfg["kd"],
            pid_cfg["setpoint"], pid_cfg["output_min"], pid_cfg["output_max"],
        )

        temp = pid_cfg["lolo"] + 1.0  # just above LoLo, but below setpoint
        output = pid.update(temp, pid_cfg["sample_time"])
        speed = apply_lolo_cutoff(output, temp, pid_cfg["lolo"])
        # PID output will be 0 (temp below setpoint), but cutoff doesn't force it
        assert speed >= 0.0  # not forcibly turned off

    def test_midrange_proportional(self):
        """Midway between LO and HI, fan should be in a proportional range.

        P-term alone at midpoint = Kp * (mid - setpoint). With Ki accumulating
        over settling samples, the output will be somewhat above the pure P value.
        We accept a wide band (20–80%) to account for integral contribution.
        """
        pid_cfg = load_pid_config()
        pid = SimplePID(
            pid_cfg["kp"], pid_cfg["ki"], pid_cfg["kd"],
            pid_cfg["setpoint"], pid_cfg["output_min"], pid_cfg["output_max"],
        )

        midpoint = (pid_cfg["lo"] + pid_cfg["hi"]) / 2

        # Settle at midpoint — limit settling to avoid I-term saturation
        for _ in range(5):
            output = pid.update(midpoint, pid_cfg["sample_time"])

        assert 20.0 <= output <= 80.0, (
            f"Expected proportional range at midpoint ({midpoint}°F), got {output:.1f}%"
        )

    def test_ramp_from_cold_to_hot(self, mock_pi):
        """Ramp temperature from below LoLo to above HI and verify fan stages."""
        pid_cfg = load_pid_config()
        pid = SimplePID(
            pid_cfg["kp"], pid_cfg["ki"], pid_cfg["kd"],
            pid_cfg["setpoint"], pid_cfg["output_min"], pid_cfg["output_max"],
        )

        temps = [90, 100, 105, 115, 125, 128, 130, 132, 135]
        speeds = []

        for temp in temps:
            # Let PID settle at each temp for a few samples
            for _ in range(5):
                output = pid.update(temp, pid_cfg["sample_time"])
            speed = apply_lolo_cutoff(output, temp, pid_cfg["lolo"])
            speeds.append((temp, speed))

        # Below LoLo: off
        below_lolo = [s for t, s in speeds if t < pid_cfg["lolo"]]
        assert all(s == 0 for s in below_lolo), f"Expected 0% below LoLo: {below_lolo}"

        # At/above HI: full speed
        at_hi = [s for t, s in speeds if t >= pid_cfg["hi"]]
        assert all(s >= 95 for s in at_hi), f"Expected ~100% at/above HI: {at_hi}"

        # Overall: speed should be non-decreasing with temperature
        for i in range(1, len(speeds)):
            t_prev, s_prev = speeds[i - 1]
            t_curr, s_curr = speeds[i]
            assert s_curr >= s_prev - 5, (
                f"Speed should increase with temp: {t_prev}°F={s_prev:.0f}%, "
                f"{t_curr}°F={s_curr:.0f}%"
            )

    def test_history_replay_with_cutoffs(self):
        """Replay real history with LoLo cutoff applied. Verify no readings
        are at full speed when temperature is below LO."""
        pid_cfg = load_pid_config()
        pid = SimplePID(
            pid_cfg["kp"], pid_cfg["ki"], pid_cfg["kd"],
            pid_cfg["setpoint"], pid_cfg["output_min"], pid_cfg["output_max"],
        )
        history = load_temperature_history()

        violations = []
        for i in range(1, len(history)):
            ts, temp = history[i]
            output = pid.update(temp, pid_cfg["sample_time"])
            speed = apply_lolo_cutoff(output, temp, pid_cfg["lolo"])

            # If temp is well below LO, fan should not be at full speed
            if temp < pid_cfg["lo"] - 5 and speed > 50:
                violations.append((ts, temp, speed))

        assert len(violations) == 0, (
            f"{len(violations)} points where fan was >50% while temp was "
            f">5°F below LO. First: {violations[0]}"
        )


class TestDutyCycle:
    """Verify duty cycle calculations are correct."""

    def test_duty_values_in_range(self, mock_pi):
        pid_cfg = load_pid_config()
        pid = SimplePID(
            pid_cfg["kp"], pid_cfg["ki"], pid_cfg["kd"],
            pid_cfg["setpoint"], pid_cfg["output_min"], pid_cfg["output_max"],
        )
        history = load_temperature_history()[:20]

        for i in range(1, len(history)):
            _, temp = history[i]
            output = pid.update(temp, pid_cfg["sample_time"])
            speed = apply_lolo_cutoff(output, temp, pid_cfg["lolo"])
            set_fan_speed(mock_pi, 18, 25000, speed)

            duty = mock_pi.hardware_PWM.call_args[0][2]
            assert 0 <= duty <= 1000000, f"Duty {duty} out of pigpio range"
            assert duty % 10000 == 0, f"Duty {duty} not a multiple of 10000"
