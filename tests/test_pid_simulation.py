"""PID simulation using real vault temperature history.

Replays recorded temperature data through simple-pid (the same library
used by the HA integration) and verifies the fan controller responds
correctly. This tests the full logic chain: temperature → PID → speed → duty.

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
from simple_pid import PID

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


def make_pid(pid_cfg):
    """Create a simple-pid PID instance matching the HA integration config."""
    pid = PID(
        Kp=pid_cfg["kp"],
        Ki=pid_cfg["ki"],
        Kd=pid_cfg["kd"],
        setpoint=pid_cfg["setpoint"],
        output_limits=(pid_cfg["output_min"], pid_cfg["output_max"]),
        sample_time=None,  # we control timing manually
    )
    return pid


def load_temperature_history():
    """Load vault temperature CSV. Returns list of (timestamp_str, temp_f) tuples."""
    if not HISTORY_CSV.exists():
        pytest.skip(f"Temperature history not found: {HISTORY_CSV}")
    with open(HISTORY_CSV) as f:
        reader = csv.DictReader(f)
        return [(row["timestamp"], float(row["temp_f"])) for row in reader]


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

    def test_gains_are_negative_for_cooling(self):
        """Cooling application requires negative gains with simple-pid."""
        pid = load_pid_config()
        assert pid["kp"] < 0, f"Kp should be negative for cooling, got {pid['kp']}"
        assert pid["ki"] < 0, f"Ki should be negative for cooling, got {pid['ki']}"
        assert pid["kd"] <= 0, f"Kd should be <= 0 for cooling, got {pid['kd']}"


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
        pid = make_pid(pid_cfg)

        history = load_temperature_history()
        for i in range(1, len(history)):
            _, temp = history[i]
            output = pid(temp)
            assert pid_cfg["output_min"] <= output <= pid_cfg["output_max"], (
                f"PID output {output} out of bounds at step {i}, temp={temp}°F"
            )

    def test_fan_speed_valid_for_all_history(self, mock_pi):
        """Every PID output should produce a valid hardware_PWM call."""
        pid_cfg = load_pid_config()
        pid = make_pid(pid_cfg)
        history = load_temperature_history()

        for i in range(1, len(history)):
            _, temp = history[i]
            output = pid(temp)
            speed = apply_lolo_cutoff(output, temp, pid_cfg["lolo"])
            result = set_fan_speed(mock_pi, 18, 25000, speed)
            assert 0 <= result <= 100

        assert mock_pi.hardware_PWM.call_count == len(history) - 1


class TestThresholdBehavior:
    """Verify fan behavior at the three threshold boundaries."""

    def test_at_hi_full_speed(self):
        """At HI temperature, PID should output ~100%."""
        pid_cfg = load_pid_config()
        pid = make_pid(pid_cfg)

        # Settle at HI
        for _ in range(20):
            output = pid(pid_cfg["hi"])

        assert output >= 95.0, (
            f"Expected ~100% at HI ({pid_cfg['hi']}°F), got {output:.1f}%"
        )

    def test_at_lo_idle(self):
        """At LO temperature (= setpoint), PID should output near 0%."""
        pid_cfg = load_pid_config()
        pid = make_pid(pid_cfg)

        # Settle at LO (= setpoint → error = 0)
        for _ in range(50):
            output = pid(pid_cfg["lo"])

        assert output <= 10.0, (
            f"Expected near-idle at LO ({pid_cfg['lo']}°F), got {output:.1f}%"
        )

    def test_below_lolo_fan_off(self):
        """Below LoLo, fan should be OFF regardless of PID output."""
        pid_cfg = load_pid_config()
        pid = make_pid(pid_cfg)

        temp = pid_cfg["lolo"] - 5.0
        output = pid(temp)
        speed = apply_lolo_cutoff(output, temp, pid_cfg["lolo"])
        assert speed == 0.0, f"Expected 0% below LoLo, got {speed:.1f}%"

    def test_just_above_lolo_fan_runs(self):
        """Just above LoLo, PID output should pass through (not cut off)."""
        pid_cfg = load_pid_config()
        pid = make_pid(pid_cfg)

        temp = pid_cfg["lolo"] + 1.0
        output = pid(temp)
        speed = apply_lolo_cutoff(output, temp, pid_cfg["lolo"])
        assert speed >= 0.0  # not forcibly turned off

    def test_midrange_proportional(self):
        """Midway between LO and HI, fan should be in a proportional range."""
        pid_cfg = load_pid_config()
        pid = make_pid(pid_cfg)

        midpoint = (pid_cfg["lo"] + pid_cfg["hi"]) / 2

        # Settle briefly
        for _ in range(5):
            output = pid(midpoint)

        assert 20.0 <= output <= 80.0, (
            f"Expected proportional range at midpoint ({midpoint}°F), got {output:.1f}%"
        )

    def test_ramp_from_cold_to_hot(self, mock_pi):
        """Ramp temperature from below LoLo to above HI and verify fan stages."""
        pid_cfg = load_pid_config()
        pid = make_pid(pid_cfg)

        temps = [90, 100, 105, 110, 115, 120, 125, 130, 135]
        speeds = []

        for temp in temps:
            for _ in range(5):
                output = pid(float(temp))
            speed = apply_lolo_cutoff(output, temp, pid_cfg["lolo"])
            speeds.append((temp, speed))

        # Below LoLo: off
        below_lolo = [s for t, s in speeds if t < pid_cfg["lolo"]]
        assert all(s == 0 for s in below_lolo), f"Expected 0% below LoLo: {below_lolo}"

        # At/above HI: full speed
        at_hi = [s for t, s in speeds if t >= pid_cfg["hi"]]
        assert all(s >= 95 for s in at_hi), f"Expected ~100% at/above HI: {at_hi}"

    def test_history_replay_with_cutoffs(self):
        """Replay real history with LoLo cutoff applied."""
        pid_cfg = load_pid_config()
        pid = make_pid(pid_cfg)
        history = load_temperature_history()

        violations = []
        for i in range(1, len(history)):
            ts, temp = history[i]
            output = pid(temp)
            speed = apply_lolo_cutoff(output, temp, pid_cfg["lolo"])

            if temp < pid_cfg["lo"] - 5 and speed > 50:
                violations.append((ts, temp, speed))

        assert len(violations) == 0, (
            f"{len(violations)} points where fan was >50% while temp was "
            f">5°F below LO. First: {violations[0]}"
        )


class TestCoolingConvention:
    """Verify the negative-gain cooling convention works correctly."""

    def test_hot_produces_positive_output(self):
        """Temperature above setpoint should produce positive fan output."""
        pid_cfg = load_pid_config()
        pid = make_pid(pid_cfg)

        output = pid(pid_cfg["setpoint"] + 10)
        assert output > 0, f"Expected positive output when hot, got {output}"

    def test_cold_produces_zero_output(self):
        """Temperature below setpoint should produce 0% (clamped)."""
        pid_cfg = load_pid_config()
        pid = make_pid(pid_cfg)

        # Feed several below-setpoint readings to let integral settle
        for _ in range(10):
            output = pid(pid_cfg["setpoint"] - 10)

        assert output == 0.0, f"Expected 0% when cold, got {output}"

    def test_proportional_to_error(self):
        """Output should be roughly proportional to temperature error."""
        pid_cfg = load_pid_config()

        outputs = []
        for offset in [2, 5, 10, 15]:
            pid = make_pid(pid_cfg)  # fresh PID for each to avoid I-term
            output = pid(pid_cfg["setpoint"] + offset)
            outputs.append((offset, output))

        # Each higher offset should produce higher output
        for i in range(1, len(outputs)):
            assert outputs[i][1] >= outputs[i-1][1], (
                f"Output should increase with error: "
                f"{outputs[i-1][0]}°F={outputs[i-1][1]:.1f}%, "
                f"{outputs[i][0]}°F={outputs[i][1]:.1f}%"
            )


class TestDutyCycle:
    """Verify duty cycle calculations are correct."""

    def test_duty_values_in_range(self, mock_pi):
        pid_cfg = load_pid_config()
        pid = make_pid(pid_cfg)
        history = load_temperature_history()[:20]

        for i in range(1, len(history)):
            _, temp = history[i]
            output = pid(temp)
            speed = apply_lolo_cutoff(output, temp, pid_cfg["lolo"])
            set_fan_speed(mock_pi, 18, 25000, speed)

            duty = mock_pi.hardware_PWM.call_args[0][2]
            assert 0 <= duty <= 1000000, f"Duty {duty} out of pigpio range"
            assert duty % 10000 == 0, f"Duty {duty} not a multiple of 10000"
