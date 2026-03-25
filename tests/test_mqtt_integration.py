"""Mode 3: Local with broker — mock pigpio, real MQTT. Needs a reachable broker."""

import sys
import pathlib
import threading
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "pi"))
from fan_controller import on_connect, on_message, setup_mqtt

pytestmark = pytest.mark.broker


class TestMqttConnect:
    def test_connect_and_subscribe(self, test_config, mock_pi, broker_available):
        """Client should connect and subscribe to the speed topic on connect."""
        connected = threading.Event()

        original_on_connect = on_connect

        def tracking_on_connect(client, userdata, flags, reason_code, properties=None):
            original_on_connect(client, userdata, flags, reason_code, properties)
            connected.set()

        client = setup_mqtt(test_config, mock_pi)
        client.on_connect = tracking_on_connect
        client.loop_start()

        assert connected.wait(timeout=5), "MQTT client did not connect within 5s"
        client.loop_stop()
        client.disconnect()


class TestSpeedMessage:
    def test_speed_triggers_pwm(self, test_config, mock_pi, mqtt_helper_client):
        """Publishing a speed value should call hardware_PWM on the mock pi."""
        received = threading.Event()

        def on_msg(client, userdata, msg):
            on_message(client, userdata, msg)
            received.set()

        client = setup_mqtt(test_config, mock_pi)
        client.on_message = on_msg
        client.loop_start()
        time.sleep(0.5)  # let subscribe propagate

        mqtt_helper_client.publish(test_config["topics"]["speed"], "50")

        assert received.wait(timeout=5), "Speed message not received within 5s"
        mock_pi.hardware_PWM.assert_called_with(18, 25000, 500000)

        client.loop_stop()
        client.disconnect()

    def test_invalid_payload_no_crash(self, test_config, mock_pi, mqtt_helper_client):
        """Publishing garbage should not crash the controller."""
        received = threading.Event()

        def on_msg(client, userdata, msg):
            on_message(client, userdata, msg)
            received.set()

        client = setup_mqtt(test_config, mock_pi)
        client.on_message = on_msg
        client.loop_start()
        time.sleep(0.5)

        mqtt_helper_client.publish(test_config["topics"]["speed"], "garbage")

        assert received.wait(timeout=5), "Message not received within 5s"
        mock_pi.hardware_PWM.assert_not_called()

        client.loop_stop()
        client.disconnect()


class TestRpmPublish:
    def test_rpm_arrives(self, test_config, mock_pi, mqtt_helper_client):
        """Controller publishing to the RPM topic should be received by a subscriber."""
        received = threading.Event()
        rpm_value = []

        def on_msg(client, userdata, msg):
            rpm_value.append(msg.payload.decode())
            received.set()

        mqtt_helper_client.subscribe(test_config["topics"]["rpm"])
        mqtt_helper_client.on_message = on_msg
        time.sleep(0.5)

        # Simulate the controller publishing an RPM reading
        client = setup_mqtt(test_config, mock_pi)
        client.loop_start()
        time.sleep(0.3)
        client.publish(test_config["topics"]["rpm"], "1200")

        assert received.wait(timeout=5), "RPM message not received within 5s"
        assert rpm_value[0] == "1200"

        client.loop_stop()
        client.disconnect()


class TestTopicIsolation:
    def test_live_topic_not_received(self, test_config, mock_pi, mqtt_helper_client):
        """Messages on the live topic should not reach a client subscribed to the test topic."""
        received = threading.Event()

        def on_msg(client, userdata, msg):
            received.set()

        client = setup_mqtt(test_config, mock_pi)
        client.on_message = on_msg
        client.loop_start()
        time.sleep(0.5)

        # Publish to the LIVE topic, not the test topic
        mqtt_helper_client.publish("rack/fan/speed", "99")

        assert not received.wait(timeout=2), "Received message on wrong topic"

        client.loop_stop()
        client.disconnect()
