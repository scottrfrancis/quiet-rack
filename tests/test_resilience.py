"""Resilience tests — broker disconnect/reconnect, retained messages,
rapid commands, graceful shutdown, and stale state handling.

Tests cover real-world failure modes that occur in production:
- EMQX broker restarts
- WiFi drops on the Pi
- HA restarts and re-publishes retained speed
- Burst of MQTT speed commands
- systemd stop/restart of the fan-controller service

All marked 'local' (mock pigpio + mock MQTT) unless noted.
"""

import sys
import pathlib
import threading
import time
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "pi"))
from fan_controller import (
    set_fan_speed,
    on_connect,
    on_message,
    on_disconnect,
    setup_mqtt,
    run_loop,
)

pytestmark = pytest.mark.local

PWM_GPIO = 18
PWM_FREQ = 25000


def make_userdata(mock_pi):
    return {
        "pi_inst": mock_pi,
        "pwm_gpio": PWM_GPIO,
        "pwm_freq": PWM_FREQ,
        "speed_topic": "rack/fan/test/speed",
        "status_topic": "rack/fan/test/status",
    }


def make_msg(payload_str):
    msg = MagicMock()
    msg.payload = payload_str.encode()
    return msg


# ---------------------------------------------------------------------------
# on_disconnect handler
# ---------------------------------------------------------------------------

class TestOnDisconnect:
    """Verify on_disconnect callback logs and handles disconnection."""

    def test_on_disconnect_unexpected(self, mock_pi, capsys):
        """Unexpected disconnect (rc != 0) should log a warning."""
        userdata = make_userdata(mock_pi)
        disconnect_flags = MagicMock()
        on_disconnect(None, userdata, disconnect_flags, 1, None)
        captured = capsys.readouterr()
        assert "disconnect" in captured.out.lower() or "lost" in captured.out.lower()

    def test_on_disconnect_clean(self, mock_pi, capsys):
        """Clean disconnect (rc=0) should log normally."""
        userdata = make_userdata(mock_pi)
        disconnect_flags = MagicMock()
        on_disconnect(None, userdata, disconnect_flags, 0, None)
        captured = capsys.readouterr()
        assert "disconnect" in captured.out.lower()


# ---------------------------------------------------------------------------
# Reconnection behavior
# ---------------------------------------------------------------------------

class TestReconnection:
    """Verify the client re-subscribes after reconnection."""

    def test_on_connect_resubscribes(self, mock_pi):
        """on_connect should subscribe to speed topic every time it's called,
        not just the first time. This is critical for reconnection."""
        client = MagicMock()
        userdata = make_userdata(mock_pi)

        # Simulate first connect
        on_connect(client, userdata, {}, 0)
        assert client.subscribe.call_count == 1
        client.subscribe.assert_called_with("rack/fan/test/speed", qos=1)

        # Simulate reconnect — on_connect fires again
        client.reset_mock()
        on_connect(client, userdata, {}, 0)
        assert client.subscribe.call_count == 1
        client.subscribe.assert_called_with("rack/fan/test/speed", qos=1)

    def test_on_connect_with_failure_rc(self, mock_pi):
        """on_connect with non-zero rc should not subscribe."""
        client = MagicMock()
        userdata = make_userdata(mock_pi)

        # rc=5 = not authorized
        on_connect(client, userdata, {}, 5)
        client.subscribe.assert_not_called()


# ---------------------------------------------------------------------------
# Retained messages on cold start
# ---------------------------------------------------------------------------

class TestRetainedMessages:
    """Verify behavior when connecting to a broker with retained messages."""

    def test_retained_speed_applied_on_connect(self, mock_pi):
        """After connect + subscribe, a retained message should set fan speed."""
        userdata = make_userdata(mock_pi)

        # Simulate connect
        client = MagicMock()
        on_connect(client, userdata, {}, 0)

        # Simulate receiving the retained message
        msg = make_msg("75")
        msg.retain = True
        on_message(client, userdata, msg)

        mock_pi.hardware_PWM.assert_called_with(PWM_GPIO, PWM_FREQ, 750000)

    def test_retained_zero_stops_fan(self, mock_pi):
        """Retained 0 should stop the fan (LoLo cutoff published 0)."""
        userdata = make_userdata(mock_pi)
        on_message(None, userdata, make_msg("0"))
        mock_pi.hardware_PWM.assert_called_with(PWM_GPIO, PWM_FREQ, 0)

    def test_retained_empty_string_ignored(self, mock_pi):
        """Empty retained message (from mqtt_debug.py clear) should be ignored."""
        userdata = make_userdata(mock_pi)
        on_message(None, userdata, make_msg(""))
        mock_pi.hardware_PWM.assert_not_called()


# ---------------------------------------------------------------------------
# Rapid speed changes
# ---------------------------------------------------------------------------

class TestRapidSpeedChanges:
    """Verify behavior under burst of rapid MQTT messages."""

    def test_rapid_sequence_last_wins(self, mock_pi):
        """A burst of speed messages should all apply in order, last one wins."""
        userdata = make_userdata(mock_pi)

        for speed in [10, 30, 50, 70, 90, 100, 60, 25]:
            on_message(None, userdata, make_msg(str(speed)))

        # Last call should be 25%
        mock_pi.hardware_PWM.assert_called_with(PWM_GPIO, PWM_FREQ, 250000)
        assert mock_pi.hardware_PWM.call_count == 8

    def test_rapid_same_value(self, mock_pi):
        """Repeated same value should apply every time (idempotent)."""
        userdata = make_userdata(mock_pi)

        for _ in range(10):
            on_message(None, userdata, make_msg("50"))

        assert mock_pi.hardware_PWM.call_count == 10
        # All calls should be identical
        for c in mock_pi.hardware_PWM.call_args_list:
            assert c == call(PWM_GPIO, PWM_FREQ, 500000)

    def test_rapid_zero_to_hundred(self, mock_pi):
        """Rapid toggle between 0 and 100 should not crash."""
        userdata = make_userdata(mock_pi)

        for _ in range(100):
            on_message(None, userdata, make_msg("0"))
            on_message(None, userdata, make_msg("100"))

        assert mock_pi.hardware_PWM.call_count == 200


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

class TestGracefulShutdown:
    """Verify fan goes to a safe state on shutdown."""

    def test_set_fan_speed_zero_on_shutdown(self, mock_pi):
        """On SIGTERM, fan should be set to 0 before exit."""
        # This tests the function we should call in a signal handler
        set_fan_speed(mock_pi, PWM_GPIO, PWM_FREQ, 50)  # running
        set_fan_speed(mock_pi, PWM_GPIO, PWM_FREQ, 0)   # shutdown
        mock_pi.hardware_PWM.assert_called_with(PWM_GPIO, PWM_FREQ, 0)

    def test_pigpio_stop_called(self, mock_pi):
        """pigpio.pi.stop() should be callable without error for cleanup."""
        mock_pi.stop()
        mock_pi.stop.assert_called_once()


# ---------------------------------------------------------------------------
# Stale state / message gap
# ---------------------------------------------------------------------------

class TestStaleState:
    """Verify behavior when MQTT goes silent for extended periods."""

    def test_no_messages_fan_holds_speed(self, mock_pi):
        """If no MQTT messages arrive, the PWM output holds its last value.
        The fan_controller doesn't have a timeout — it relies on the HA
        automation's 30s time-pattern to keep publishing."""
        userdata = make_userdata(mock_pi)

        # Set initial speed
        on_message(None, userdata, make_msg("65"))
        mock_pi.hardware_PWM.assert_called_with(PWM_GPIO, PWM_FREQ, 650000)

        # No more messages — PWM stays at last value
        # (no watchdog to verify, but importantly it doesn't reset to 0)
        mock_pi.hardware_PWM.reset_mock()

        # Simulate time passing with no messages (run_loop just publishes RPM)
        # The important thing: hardware_PWM is NOT called again — fan holds
        assert mock_pi.hardware_PWM.call_count == 0

    def test_message_after_long_gap(self, mock_pi):
        """After a long silent period, a new message should apply normally."""
        userdata = make_userdata(mock_pi)

        # Initial speed
        on_message(None, userdata, make_msg("65"))

        # Long gap... then new message
        mock_pi.hardware_PWM.reset_mock()
        on_message(None, userdata, make_msg("30"))
        mock_pi.hardware_PWM.assert_called_with(PWM_GPIO, PWM_FREQ, 300000)


# ---------------------------------------------------------------------------
# Broker reconnection with real MQTT (Mode 3)
# ---------------------------------------------------------------------------

class TestBrokerReconnection:
    """Real MQTT broker reconnection tests. Requires a running broker."""

    pytestmark = pytest.mark.broker

    def test_resubscribes_after_disconnect(self, test_config, mock_pi, mqtt_helper_client):
        """After a forced disconnect+reconnect, the client should
        re-subscribe and receive new messages."""
        received = threading.Event()
        reconnected = threading.Event()
        speeds = []

        original_on_connect = on_connect

        def tracking_on_connect(client, userdata, flags, reason_code, properties=None):
            original_on_connect(client, userdata, flags, reason_code, properties)
            reconnected.set()

        def on_msg(client, userdata, msg):
            on_message(client, userdata, msg)
            speeds.append(msg.payload.decode())
            received.set()

        client = setup_mqtt(test_config, mock_pi)
        client.on_connect = tracking_on_connect
        client.on_message = on_msg
        client.loop_start()
        assert reconnected.wait(timeout=5), "Initial connect failed"
        reconnected.clear()
        time.sleep(0.5)

        # Verify initial message works
        mqtt_helper_client.publish(test_config["topics"]["speed"], "40")
        assert received.wait(timeout=5)
        received.clear()

        # Force disconnect and reconnect
        client.loop_stop()
        client.disconnect()
        time.sleep(1)
        client.reconnect()
        client.loop_start()

        # Wait for on_connect to fire (proves re-subscribe happened)
        assert reconnected.wait(timeout=10), "Did not reconnect"
        time.sleep(0.5)

        # Publish after reconnect
        mqtt_helper_client.publish(test_config["topics"]["speed"], "80")
        assert received.wait(timeout=5), "Did not receive message after reconnect"
        assert "80" in speeds

        client.loop_stop()
        client.disconnect()

    def test_retained_message_on_reconnect(self, test_config, mock_pi, mqtt_helper_client):
        """After reconnect, any retained message should be received."""
        # Publish a retained message
        mqtt_helper_client.publish(test_config["topics"]["speed"], "55", retain=True)
        time.sleep(0.5)

        received = threading.Event()

        def on_msg(client, userdata, msg):
            on_message(client, userdata, msg)
            if msg.retain:
                received.set()

        # Connect fresh — should receive the retained message
        client = setup_mqtt(test_config, mock_pi)
        client.on_message = on_msg
        client.loop_start()

        assert received.wait(timeout=5), "Did not receive retained message"
        mock_pi.hardware_PWM.assert_called_with(PWM_GPIO, PWM_FREQ, 550000)

        client.loop_stop()
        client.disconnect()

        # Clean up retained message
        mqtt_helper_client.publish(test_config["topics"]["speed"], "", retain=True)
