"""Mode 1: Local unit tests — mock pigpio, mock MQTT, no hardware, no broker."""

import pathlib
from unittest.mock import MagicMock

import pytest

from fan_controller import (
    calc_rpm,
    load_config,
    on_connect,
    on_message,
    set_fan_speed,
)

pytestmark = pytest.mark.local


# --- load_config ---


class TestLoadConfig:
    def test_reads_yaml(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "mqtt:\n  host: 1.2.3.4\n  port: 1883\n  user: u\n  password: p\n"
            "topics:\n  speed: t/s\n  rpm: t/r\n"
            "gpio:\n  pwm: 18\n  tach: 24\n"
            "pwm:\n  frequency: 25000\n"
            "tach:\n  interval: 10\n"
        )
        cfg = load_config(cfg_file)
        assert cfg["mqtt"]["host"] == "1.2.3.4"
        assert cfg["gpio"]["pwm"] == 18
        assert cfg["topics"]["speed"] == "t/s"

    def test_missing_file_exits(self, tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            load_config(tmp_path / "nonexistent.yaml")
        assert exc_info.value.code == 1


# --- set_fan_speed ---


class TestSetFanSpeed:
    def test_midpoint(self, mock_pi):
        result = set_fan_speed(mock_pi, 18, 25000, 50)
        mock_pi.hardware_PWM.assert_called_with(18, 25000, 500000)
        assert result == 50

    def test_clamps_low(self, mock_pi):
        result = set_fan_speed(mock_pi, 18, 25000, -10)
        mock_pi.hardware_PWM.assert_called_with(18, 25000, 0)
        assert result == 0

    def test_clamps_high(self, mock_pi):
        result = set_fan_speed(mock_pi, 18, 25000, 150)
        mock_pi.hardware_PWM.assert_called_with(18, 25000, 1000000)
        assert result == 100

    def test_boundary_zero(self, mock_pi):
        result = set_fan_speed(mock_pi, 18, 25000, 0)
        mock_pi.hardware_PWM.assert_called_with(18, 25000, 0)
        assert result == 0

    def test_boundary_100(self, mock_pi):
        result = set_fan_speed(mock_pi, 18, 25000, 100)
        mock_pi.hardware_PWM.assert_called_with(18, 25000, 1000000)
        assert result == 100

    def test_truncates_float(self, mock_pi):
        result = set_fan_speed(mock_pi, 18, 25000, 33.7)
        mock_pi.hardware_PWM.assert_called_with(18, 25000, 330000)
        assert result == 33

    @pytest.mark.parametrize("pct", [0, 10, 25, 50, 75, 100])
    def test_full_range(self, mock_pi, pct):
        result = set_fan_speed(mock_pi, 18, 25000, pct)
        expected_duty = pct * 10000
        mock_pi.hardware_PWM.assert_called_with(18, 25000, expected_duty)
        assert result == pct


# --- calc_rpm ---


class TestCalcRpm:
    def test_standard_interval(self):
        # 100 pulses in 10s, 2 pulses/rev → 50 rev/10s → 300 RPM
        assert calc_rpm(100, 10) == 300.0

    def test_one_second_interval(self):
        # 10 pulses in 1s → 5 rev/s → 300 RPM
        assert calc_rpm(10, 1) == 300.0

    def test_zero_pulses(self):
        assert calc_rpm(0, 10) == 0.0

    def test_single_pulse(self):
        assert calc_rpm(1, 10) == 3.0


# --- on_connect ---


class TestOnConnect:
    def test_subscribes_to_speed_topic(self):
        client = MagicMock()
        userdata = {"speed_topic": "rack/fan/test/speed", "status_topic": "rack/fan/test/status"}
        on_connect(client, userdata, {}, 0)
        client.subscribe.assert_called_once_with("rack/fan/test/speed", qos=1)


# --- on_message ---


class TestOnMessage:
    def _make_msg(self, payload_str):
        msg = MagicMock()
        msg.payload = payload_str.encode()
        return msg

    def test_valid_payload(self, mock_pi):
        userdata = {"pi_inst": mock_pi, "pwm_gpio": 18, "pwm_freq": 25000}
        on_message(None, userdata, self._make_msg("65"))
        mock_pi.hardware_PWM.assert_called_with(18, 25000, 650000)

    def test_float_payload(self, mock_pi):
        userdata = {"pi_inst": mock_pi, "pwm_gpio": 18, "pwm_freq": 25000}
        on_message(None, userdata, self._make_msg("72.9"))
        mock_pi.hardware_PWM.assert_called_with(18, 25000, 720000)

    def test_invalid_payload_ignored(self, mock_pi):
        userdata = {"pi_inst": mock_pi, "pwm_gpio": 18, "pwm_freq": 25000}
        on_message(None, userdata, self._make_msg("not_a_number"))
        mock_pi.hardware_PWM.assert_not_called()

    def test_empty_payload_ignored(self, mock_pi):
        userdata = {"pi_inst": mock_pi, "pwm_gpio": 18, "pwm_freq": 25000}
        on_message(None, userdata, self._make_msg(""))
        mock_pi.hardware_PWM.assert_not_called()
