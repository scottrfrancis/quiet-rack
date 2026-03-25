"""Shared fixtures and test configuration for the rack fan controller test suite.

Broker connection details come from pi/config.yaml (the single source of truth).
Tests only override the topics to a test namespace so they never affect the live fan.
If pi/config.yaml doesn't exist, broker-dependent tests skip automatically.
"""

import copy
import pathlib
import socket
import sys
from unittest.mock import MagicMock

import pytest
import yaml

# Ensure pi/ is importable
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "pi"))

SITE_CONFIG_PATH = pathlib.Path(__file__).parent.parent / "pi" / "config.yaml"

# Test topic namespace — never collides with live topics
TEST_TOPICS = {
    "speed": "rack/fan/test/speed",
    "rpm": "rack/fan/test/rpm",
}


def _load_test_config():
    """Load pi/config.yaml and swap topics to the test namespace.

    Returns None if pi/config.yaml doesn't exist (no broker tests possible).
    """
    if not SITE_CONFIG_PATH.exists():
        return None

    with open(SITE_CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    # Override topics — everything else (host, port, credentials, gpio, pwm) comes
    # from the single config file.
    cfg["topics"] = copy.deepcopy(TEST_TOPICS)

    # Shorten tach interval for faster test cycles
    cfg.setdefault("tach", {})["interval"] = 1

    return cfg


@pytest.fixture
def test_config():
    """Return a deep copy of the test configuration.

    Skips if pi/config.yaml is missing (broker tests can't run without credentials).
    """
    cfg = _load_test_config()
    if cfg is None:
        pytest.skip("pi/config.yaml not found — copy config.example.yaml and fill in values")
    return copy.deepcopy(cfg)


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
