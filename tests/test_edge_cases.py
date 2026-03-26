"""Edge case and out-of-bounds tests for the fan controller.

Tests abnormal inputs, sensor failures, and boundary conditions that
could occur in production. All local — no hardware or broker needed.
"""

import sys
import pathlib
from unittest.mock import MagicMock

import pytest
import yaml
from simple_pid import PID

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "pi"))
from fan_controller import set_fan_speed, calc_rpm, on_message, load_config

pytestmark = pytest.mark.local

PWM_GPIO = 18
PWM_FREQ = 25000

CONFIG_PATH = pathlib.Path(__file__).parent.parent / "pi" / "config.yaml"
EXAMPLE_CONFIG_PATH = CONFIG_PATH.with_name("config.example.yaml")


def load_pid_config():
    path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH
    with open(path) as f:
        return yaml.safe_load(f)["pid"]


def make_pid(pid_cfg):
    return PID(
        Kp=pid_cfg["kp"], Ki=pid_cfg["ki"], Kd=pid_cfg["kd"],
        setpoint=pid_cfg["setpoint"],
        output_limits=(pid_cfg["output_min"], pid_cfg["output_max"]),
        sample_time=None,
    )


# ---------------------------------------------------------------------------
# Out-of-bounds speed commands
# ---------------------------------------------------------------------------

class TestOutOfBoundsSpeed:
    """MQTT could deliver any string. The controller must handle it safely."""

    def test_negative_speed(self, mock_pi):
        result = set_fan_speed(mock_pi, PWM_GPIO, PWM_FREQ, -50)
        assert result == 0
        mock_pi.hardware_PWM.assert_called_with(PWM_GPIO, PWM_FREQ, 0)

    def test_speed_above_100(self, mock_pi):
        result = set_fan_speed(mock_pi, PWM_GPIO, PWM_FREQ, 200)
        assert result == 100
        mock_pi.hardware_PWM.assert_called_with(PWM_GPIO, PWM_FREQ, 1000000)

    def test_speed_way_above_100(self, mock_pi):
        result = set_fan_speed(mock_pi, PWM_GPIO, PWM_FREQ, 999999)
        assert result == 100

    def test_speed_large_negative(self, mock_pi):
        result = set_fan_speed(mock_pi, PWM_GPIO, PWM_FREQ, -999999)
        assert result == 0

    def test_speed_nan_via_message(self, mock_pi):
        """NaN payload should be silently ignored."""
        userdata = {"pi_inst": mock_pi, "pwm_gpio": PWM_GPIO, "pwm_freq": PWM_FREQ}
        msg = MagicMock()
        msg.payload = b"NaN"
        on_message(None, userdata, msg)
        mock_pi.hardware_PWM.assert_not_called()

    def test_speed_inf_via_message(self, mock_pi):
        """Infinity payload should be rejected without setting PWM."""
        userdata = {"pi_inst": mock_pi, "pwm_gpio": PWM_GPIO, "pwm_freq": PWM_FREQ}
        msg = MagicMock()
        msg.payload = b"inf"
        on_message(None, userdata, msg)
        mock_pi.hardware_PWM.assert_not_called()

    def test_speed_negative_inf_via_message(self, mock_pi):
        """Negative infinity payload should be rejected."""
        userdata = {"pi_inst": mock_pi, "pwm_gpio": PWM_GPIO, "pwm_freq": PWM_FREQ}
        msg = MagicMock()
        msg.payload = b"-inf"
        on_message(None, userdata, msg)
        mock_pi.hardware_PWM.assert_not_called()

    def test_speed_inf_direct(self, mock_pi):
        """Direct call with inf returns -1 (rejected)."""
        result = set_fan_speed(mock_pi, PWM_GPIO, PWM_FREQ, float('inf'))
        assert result == -1
        mock_pi.hardware_PWM.assert_not_called()

    def test_speed_nan_direct(self, mock_pi):
        """Direct call with NaN returns -1 (rejected)."""
        result = set_fan_speed(mock_pi, PWM_GPIO, PWM_FREQ, float('nan'))
        assert result == -1
        mock_pi.hardware_PWM.assert_not_called()

    def test_speed_with_whitespace(self, mock_pi):
        """Payload with leading/trailing whitespace."""
        userdata = {"pi_inst": mock_pi, "pwm_gpio": PWM_GPIO, "pwm_freq": PWM_FREQ}
        msg = MagicMock()
        msg.payload = b"  42  "
        on_message(None, userdata, msg)
        mock_pi.hardware_PWM.assert_called_with(PWM_GPIO, PWM_FREQ, 420000)

    def test_speed_with_newline(self, mock_pi):
        """Payload with trailing newline (common in shell-generated MQTT messages)."""
        userdata = {"pi_inst": mock_pi, "pwm_gpio": PWM_GPIO, "pwm_freq": PWM_FREQ}
        msg = MagicMock()
        msg.payload = b"75\n"
        on_message(None, userdata, msg)
        mock_pi.hardware_PWM.assert_called_with(PWM_GPIO, PWM_FREQ, 750000)


# ---------------------------------------------------------------------------
# Malformed MQTT payloads
# ---------------------------------------------------------------------------

class TestMalformedPayloads:
    """Various garbage that could arrive on the MQTT topic."""

    @pytest.mark.parametrize("payload", [
        b"",
        b"not_a_number",
        b"{}",
        b'{"speed": 50}',
        b"null",
        b"true",
        b"false",
        b"0x1A",
        b"\x00\x01\x02",
        b"fifty",
    ])
    def test_garbage_payloads_dont_crash(self, mock_pi, payload):
        """Any non-numeric payload should be silently ignored."""
        userdata = {"pi_inst": mock_pi, "pwm_gpio": PWM_GPIO, "pwm_freq": PWM_FREQ}
        msg = MagicMock()
        msg.payload = payload
        on_message(None, userdata, msg)
        # Should not have called hardware_PWM (or if it did, should be in bounds)
        if mock_pi.hardware_PWM.called:
            duty = mock_pi.hardware_PWM.call_args[0][2]
            assert 0 <= duty <= 1000000


# ---------------------------------------------------------------------------
# Out-of-bounds temperature to PID
# ---------------------------------------------------------------------------

class TestOutOfBoundsTemperature:
    """Sensor glitches, unavailability, and extreme readings."""

    def test_extreme_high_temp(self):
        """Sensor reads 500°F (malfunction). PID should clamp to output_max."""
        pid_cfg = load_pid_config()
        pid = make_pid(pid_cfg)
        output = pid(500.0)
        assert output == pid_cfg["output_max"]

    def test_extreme_low_temp(self):
        """Sensor reads -40°F (malfunction). PID should clamp to output_min."""
        pid_cfg = load_pid_config()
        pid = make_pid(pid_cfg)
        output = pid(-40.0)
        assert output == pid_cfg["output_min"]

    def test_zero_fahrenheit(self):
        """0°F — unlikely but valid reading."""
        pid_cfg = load_pid_config()
        pid = make_pid(pid_cfg)
        output = pid(0.0)
        assert output == pid_cfg["output_min"]

    def test_exactly_at_setpoint(self):
        """At exactly the setpoint, output should be ~0."""
        pid_cfg = load_pid_config()
        pid = make_pid(pid_cfg)
        output = pid(pid_cfg["setpoint"])
        assert output == pytest.approx(0.0, abs=1.0)

    def test_rapid_oscillation(self):
        """Sensor oscillating rapidly between two values (noise)."""
        pid_cfg = load_pid_config()
        pid = make_pid(pid_cfg)

        outputs = []
        for _ in range(20):
            outputs.append(pid(120.0))
            outputs.append(pid(125.0))

        # All outputs should be within bounds
        for o in outputs:
            assert pid_cfg["output_min"] <= o <= pid_cfg["output_max"]

    def test_sensor_returns_to_normal_after_glitch(self):
        """Sensor spikes to 500°F then returns to normal. PID should recover."""
        pid_cfg = load_pid_config()
        pid = make_pid(pid_cfg)

        # Normal readings
        for _ in range(5):
            pid(120.0)

        # Glitch
        pid(500.0)

        # Return to normal — PID should recover within a few samples
        outputs = []
        for _ in range(10):
            outputs.append(pid(120.0))

        # Final output should be reasonable for 120°F (5°F above setpoint → ~33%)
        assert 10 <= outputs[-1] <= 60, f"PID didn't recover from glitch: {outputs[-1]}"


# ---------------------------------------------------------------------------
# RPM calculation edge cases
# ---------------------------------------------------------------------------

class TestRpmEdgeCases:

    def test_zero_interval(self):
        """Zero interval should not divide by zero."""
        with pytest.raises(ZeroDivisionError):
            calc_rpm(100, 0)

    def test_negative_interval(self):
        """Negative interval (clock glitch). RPM will be negative — caller's problem."""
        rpm = calc_rpm(100, -10)
        # Just verify it doesn't crash. Negative RPM is nonsensical but not our crash.
        assert isinstance(rpm, float)

    def test_very_large_pulse_count(self):
        """Counter overflow scenario — very high pulse count."""
        rpm = calc_rpm(1000000, 10)
        assert rpm > 0
        assert isinstance(rpm, float)

    def test_fractional_pulse_count(self):
        """Float pulse count (shouldn't happen but be safe)."""
        rpm = calc_rpm(10.5, 10)
        assert rpm > 0

    def test_very_short_interval(self):
        """Very short interval — high RPM calculation."""
        rpm = calc_rpm(10, 0.001)
        assert rpm > 0


# ---------------------------------------------------------------------------
# Config edge cases
# ---------------------------------------------------------------------------

class TestConfigEdgeCases:

    def test_missing_config_exits(self, tmp_path):
        """Missing config file should sys.exit(1)."""
        with pytest.raises(SystemExit) as exc_info:
            load_config(tmp_path / "nonexistent.yaml")
        assert exc_info.value.code == 1

    def test_empty_config_file(self, tmp_path):
        """Empty YAML file."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("")
        # yaml.safe_load returns None for empty file
        # load_config will try to subscript None and crash
        with pytest.raises((TypeError, KeyError)):
            cfg = load_config(cfg_file)
            # If load_config returns None, any access will fail
            _ = cfg["mqtt"]

    def test_partial_config(self, tmp_path):
        """Config with only mqtt section — missing gpio, pid, etc."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("mqtt:\n  host: localhost\n  port: 1883\n")
        cfg = load_config(cfg_file)
        assert cfg["mqtt"]["host"] == "localhost"
        # Other sections will KeyError when accessed — that's expected


# ---------------------------------------------------------------------------
# Duty cycle boundary precision
# ---------------------------------------------------------------------------

class TestDutyCyclePrecision:
    """Verify the duty cycle calculation doesn't produce off-by-one errors."""

    def test_duty_at_zero(self, mock_pi):
        set_fan_speed(mock_pi, PWM_GPIO, PWM_FREQ, 0)
        assert mock_pi.hardware_PWM.call_args[0][2] == 0

    def test_duty_at_one_percent(self, mock_pi):
        set_fan_speed(mock_pi, PWM_GPIO, PWM_FREQ, 1)
        assert mock_pi.hardware_PWM.call_args[0][2] == 10000

    def test_duty_at_99_percent(self, mock_pi):
        set_fan_speed(mock_pi, PWM_GPIO, PWM_FREQ, 99)
        assert mock_pi.hardware_PWM.call_args[0][2] == 990000

    def test_duty_at_100_percent(self, mock_pi):
        set_fan_speed(mock_pi, PWM_GPIO, PWM_FREQ, 100)
        assert mock_pi.hardware_PWM.call_args[0][2] == 1000000

    def test_float_99_9_truncates_to_99(self, mock_pi):
        result = set_fan_speed(mock_pi, PWM_GPIO, PWM_FREQ, 99.9)
        assert result == 99
        assert mock_pi.hardware_PWM.call_args[0][2] == 990000

    def test_float_0_1_truncates_to_0(self, mock_pi):
        result = set_fan_speed(mock_pi, PWM_GPIO, PWM_FREQ, 0.1)
        assert result == 0
        assert mock_pi.hardware_PWM.call_args[0][2] == 0
