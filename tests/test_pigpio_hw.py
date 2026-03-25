"""Mode 2: Remote hardware tests — real pigpio, mock MQTT. Run on the Pi only."""

import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "pi"))
from fan_controller import set_fan_speed

pytestmark = pytest.mark.remote


class TestPigpioHardware:
    def test_connected(self, real_pi):
        assert real_pi.connected

    def test_hardware_pwm_sets_mode(self, real_pi):
        """After setting PWM, GPIO18 should be in ALT5 mode (2)."""
        set_fan_speed(real_pi, 18, 25000, 50)
        mode = real_pi.get_mode(18)
        assert mode == 2  # ALT5

    def test_hardware_pwm_zero(self, real_pi):
        """Setting 0% should not raise."""
        set_fan_speed(real_pi, 18, 25000, 0)

    @pytest.mark.parametrize("pct", [0, 25, 50, 75, 100])
    def test_full_range_no_error(self, real_pi, pct):
        """All standard duty cycles should apply without error."""
        result = set_fan_speed(real_pi, 18, 25000, pct)
        assert result == pct

    def test_tach_callback_registers(self, real_pi):
        """Registering a tach callback should return a callback object."""
        import pigpio

        real_pi.set_mode(24, pigpio.INPUT)
        real_pi.set_pull_up_down(24, pigpio.PUD_UP)
        cb = real_pi.callback(24, pigpio.FALLING_EDGE, lambda g, l, t: None)
        assert cb is not None
        cb.cancel()
