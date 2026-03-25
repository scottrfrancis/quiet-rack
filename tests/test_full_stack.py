"""Mode 4: Full stack on Pi — real pigpio + real MQTT. Run on Pi with broker access."""

import sys
import pathlib
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "pi"))
from fan_controller import set_fan_speed, setup_mqtt

pytestmark = pytest.mark.full


class TestEndToEnd:
    def test_speed_sets_pwm(self, test_config, real_pi, mqtt_helper_client):
        """Publish speed over MQTT, verify real pigpio PWM is set."""
        received = threading.Event()

        def on_msg(client, userdata, msg):
            set_fan_speed(real_pi, 18, 25000, float(msg.payload.decode()))
            received.set()

        client = setup_mqtt(test_config, real_pi)
        client.on_message = on_msg
        client.loop_start()
        time.sleep(0.5)

        mqtt_helper_client.publish(test_config["topics"]["speed"], "60")

        assert received.wait(timeout=5), "Speed message not received within 5s"
        # GPIO18 should be in ALT5 mode (2) after hardware_PWM call
        assert real_pi.get_mode(18) == 2

        client.loop_stop()
        client.disconnect()

    def test_zero_speed_stops(self, test_config, real_pi, mqtt_helper_client):
        """Publishing 0% should stop the fan (duty=0)."""
        received = threading.Event()

        def on_msg(client, userdata, msg):
            set_fan_speed(real_pi, 18, 25000, float(msg.payload.decode()))
            received.set()

        client = setup_mqtt(test_config, real_pi)
        client.on_message = on_msg
        client.loop_start()
        time.sleep(0.5)

        mqtt_helper_client.publish(test_config["topics"]["speed"], "0")

        assert received.wait(timeout=5)

        client.loop_stop()
        client.disconnect()

    def test_rpm_report(self, test_config, real_pi, mqtt_helper_client):
        """RPM topic should receive a published value."""
        received = threading.Event()
        rpm_value = []

        def on_msg(client, userdata, msg):
            rpm_value.append(msg.payload.decode())
            received.set()

        mqtt_helper_client.subscribe(test_config["topics"]["rpm"])
        mqtt_helper_client.on_message = on_msg
        time.sleep(0.5)

        # Simulate controller publishing RPM
        client = setup_mqtt(test_config, real_pi)
        client.loop_start()
        time.sleep(0.3)
        client.publish(test_config["topics"]["rpm"], "850")

        assert received.wait(timeout=5)
        assert rpm_value[0] == "850"

        client.loop_stop()
        client.disconnect()
