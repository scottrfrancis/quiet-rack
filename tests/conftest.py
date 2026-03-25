"""Shared fixtures and test configuration for the rack fan controller test suite."""

import copy
import os
import pathlib
import socket
import sys
from unittest.mock import MagicMock

import pytest
import yaml

# Ensure pi/ is importable
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "pi"))

TEST_CONFIG_PATH = pathlib.Path(__file__).parent / "config_test.yaml"


def _load_test_config():
    """Load test config, applying environment variable overrides."""
    with open(TEST_CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    # Allow env var overrides for broker connection
    cfg["mqtt"]["host"] = os.environ.get("MQTT_TEST_HOST", cfg["mqtt"]["host"])
    cfg["mqtt"]["port"] = int(os.environ.get("MQTT_TEST_PORT", cfg["mqtt"]["port"]))
    cfg["mqtt"]["user"] = os.environ.get("MQTT_TEST_USER", cfg["mqtt"]["user"])
    cfg["mqtt"]["password"] = os.environ.get("MQTT_TEST_PASS", cfg["mqtt"]["password"])

    return cfg


@pytest.fixture
def test_config():
    """Return a deep copy of the test configuration."""
    return copy.deepcopy(_load_test_config())


@pytest.fixture
def mock_pi():
    """Return a MagicMock standing in for a pigpio.pi() instance."""
    pi = MagicMock()
    pi.connected = True
    pi.hardware_PWM = MagicMock()
    pi.set_mode = MagicMock()
    pi.set_pull_up_down = MagicMock()
    pi.callback = MagicMock()
    pi.get_mode = MagicMock(return_value=2)  # ALT5
    pi.stop = MagicMock()
    return pi


@pytest.fixture
def real_pi():
    """Connect to pigpiod. Skip if not available (i.e., not running on a Pi)."""
    try:
        import pigpio
    except ImportError:
        pytest.skip("pigpio not installed (not on a Pi)")

    pi = pigpio.pi()
    if not pi.connected:
        pytest.skip("pigpiod not running")
    yield pi
    # Safety: stop PWM on teardown
    pi.hardware_PWM(18, 25000, 0)
    pi.stop()


@pytest.fixture
def broker_available(test_config):
    """Skip the test if the MQTT broker is not reachable."""
    host = test_config["mqtt"]["host"]
    port = test_config["mqtt"]["port"]
    try:
        s = socket.create_connection((host, port), timeout=2)
        s.close()
    except OSError:
        pytest.skip(f"MQTT broker not reachable at {host}:{port}")


@pytest.fixture
def mqtt_helper_client(test_config, broker_available):
    """Create a real MQTT client connected to the test broker. Disconnects on teardown."""
    import paho.mqtt.client as mqtt

    client = mqtt.Client()
    client.username_pw_set(test_config["mqtt"]["user"], test_config["mqtt"]["password"])
    client.connect(test_config["mqtt"]["host"], test_config["mqtt"]["port"])
    client.loop_start()
    yield client
    client.loop_stop()
    client.disconnect()
