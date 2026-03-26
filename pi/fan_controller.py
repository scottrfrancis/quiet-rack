#!/usr/bin/env python3
"""Rack fan PWM controller — MQTT v5 subscriber that drives an Arctic P12 via pigpio.

Connects to EMQX broker with LWT, persistent sessions, QoS 1 on speed
commands, and automatic reconnection with exponential backoff.
"""

import pathlib
import signal
import sys
import time

import yaml
import paho.mqtt.client as mqtt
from paho.mqtt.properties import Properties
from paho.mqtt.packettypes import PacketTypes


def load_config(path=None):
    """Load config from YAML file. Returns dict."""
    if path is None:
        path = pathlib.Path(__file__).parent / "config.yaml"
    else:
        path = pathlib.Path(path)

    if not path.exists():
        example = path.with_name("config.example.yaml")
        print(f"ERROR: {path} not found.", file=sys.stderr)
        print(f"Copy {example.name} to {path.name} and fill in your values.", file=sys.stderr)
        sys.exit(1)

    with open(path) as f:
        return yaml.safe_load(f)


def set_fan_speed(pi_inst, pwm_gpio, pwm_freq, percent):
    """Clamp percent to 0–100, set hardware PWM duty cycle. Returns clamped value."""
    import math
    if math.isnan(percent) or math.isinf(percent):
        return -1  # reject — caller should ignore
    pct = max(0, min(100, int(percent)))
    duty = pct * 10000  # pigpio: 0–1,000,000
    pi_inst.hardware_PWM(pwm_gpio, pwm_freq, duty)
    print(f"Fan speed: {pct}%")
    return pct


def calc_rpm(pulse_count, interval):
    """Calculate RPM from tach pulse count over a time interval.

    Most 4-pin fans produce 2 pulses per revolution.
    """
    return (pulse_count / 2) * (60 / interval)


def on_connect(client, userdata, flags, reason_code, properties=None):
    """MQTT on_connect — subscribe and publish birth message.

    Called on initial connect AND on every reconnect. Re-subscribes to
    the speed topic and publishes "online" to the status topic (overriding
    any retained LWT "offline" from a previous crash).
    """
    print("MQTT connected, rc=", reason_code)
    if reason_code == 0 or str(reason_code) == "Success":
        client.subscribe(userdata["speed_topic"], qos=1)
        client.publish(
            userdata["status_topic"], "online", qos=1, retain=True,
        )
    else:
        print(f"MQTT connect failed: {reason_code}")


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    """MQTT on_disconnect — log disconnection.

    paho-mqtt v2/v5 callback. Auto-reconnect is handled by paho's
    reconnect_delay_set. This callback is for logging only.
    """
    if reason_code == 0 or str(reason_code) == "Success":
        print("MQTT disconnected cleanly")
    else:
        print(f"MQTT connection lost (rc={reason_code}), reconnecting...")


def on_message(client, userdata, msg):
    """MQTT on_message — parse speed percentage and apply it."""
    try:
        set_fan_speed(
            userdata["pi_inst"],
            userdata["pwm_gpio"],
            userdata["pwm_freq"],
            float(msg.payload.decode()),
        )
    except (ValueError, OverflowError):
        pass


def setup_pigpio(cfg):
    """Connect to pigpiod, configure PWM output and optional tach input.

    Returns (pi_inst, tach_state) where tach_state is a dict with
    'pulse_count' key (mutable list used as counter), or None if tach disabled.
    """
    import pigpio

    pi_inst = pigpio.pi()
    pwm_gpio = cfg["gpio"]["pwm"]
    pwm_freq = cfg["pwm"]["frequency"]

    pi_inst.set_mode(pwm_gpio, pigpio.OUTPUT)
    pi_inst.hardware_PWM(pwm_gpio, pwm_freq, 0)  # start stopped

    tach_gpio = cfg["gpio"].get("tach")
    tach_state = None

    if tach_gpio is not None:
        # Min pulse interval for debounce: 5ms (5000µs).
        # At max RPM (1800), tach is 60Hz → 16.7ms between pulses.
        # PWM crosstalk at 25kHz → 40µs between edges.
        # The 5ms filter rejects PWM noise while passing all real tach pulses.
        MIN_PULSE_US = 5000
        tach_state = {"pulse_count": [0], "last_tick": [0]}

        def tach_pulse(gpio, level, tick):
            dt = pigpio.tickDiff(tach_state["last_tick"][0], tick)
            if dt >= MIN_PULSE_US:
                tach_state["pulse_count"][0] += 1
                tach_state["last_tick"][0] = tick

        pi_inst.set_mode(tach_gpio, pigpio.INPUT)
        pi_inst.set_pull_up_down(tach_gpio, pigpio.PUD_UP)
        # Hardware glitch filter: ignore edges shorter than 100µs
        pi_inst.set_glitch_filter(tach_gpio, 100)
        pi_inst.callback(tach_gpio, pigpio.FALLING_EDGE, tach_pulse)

    return pi_inst, tach_state


def setup_mqtt(cfg, pi_inst):
    """Create MQTT v5 client with LWT, persistent sessions, and QoS 1.

    Features:
    - MQTT v5 protocol with EMQX
    - Fixed client_id for persistent sessions
    - LWT "offline" on rack/fan/status (retained, 30s will delay)
    - Birth message "online" on connect/reconnect
    - QoS 1 on speed subscription
    - 30s keep-alive for fast dead-client detection
    - Session expiry 300s — broker queues QoS 1 messages during disconnects
    - Auto-reconnect with 1–120s exponential backoff
    """
    status_topic = cfg["topics"].get("status", "rack/fan/status")

    userdata = {
        "pi_inst": pi_inst,
        "pwm_gpio": cfg["gpio"]["pwm"],
        "pwm_freq": cfg["pwm"]["frequency"],
        "speed_topic": cfg["topics"]["speed"],
        "status_topic": status_topic,
    }

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="rack-fan-controller",
        protocol=mqtt.MQTTv5,
        userdata=userdata,
    )

    # Credentials (optional — EMQX may use anonymous auth)
    user = cfg["mqtt"].get("user")
    password = cfg["mqtt"].get("password")
    if user:
        client.username_pw_set(user, password)

    # LWT — broker publishes "offline" if we vanish ungracefully
    will_props = Properties(PacketTypes.WILLMESSAGE)
    will_props.WillDelayInterval = 30  # suppress LWT during brief reconnects
    client.will_set(
        topic=status_topic,
        payload="offline",
        qos=1,
        retain=True,
        properties=will_props,
    )

    # Callbacks
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    # Auto-reconnect with exponential backoff: 1s initial, 120s max
    client.reconnect_delay_set(min_delay=1, max_delay=120)

    # Session expiry — broker keeps session for 5 minutes after disconnect
    connect_props = Properties(PacketTypes.CONNECT)
    connect_props.SessionExpiryInterval = 300

    client.connect(
        cfg["mqtt"]["host"],
        cfg["mqtt"].get("port", 1883),
        keepalive=30,
        clean_start=mqtt.MQTT_CLEAN_START_FIRST_ONLY,
        properties=connect_props,
    )

    return client


def run_loop(client, cfg, tach_state):
    """Main loop: publish RPM every tach interval. Blocking."""
    tach_gpio = cfg["gpio"].get("tach")
    tach_interval = cfg.get("tach", {}).get("interval", 10)
    rpm_topic = cfg["topics"]["rpm"]

    # RPM messages expire after 30s — prevents stale readings
    rpm_props = Properties(PacketTypes.PUBLISH)
    rpm_props.MessageExpiryInterval = 30

    client.loop_start()

    while True:
        time.sleep(tach_interval)
        if tach_gpio is not None and tach_state is not None:
            count = tach_state["pulse_count"][0]
            tach_state["pulse_count"][0] = 0
            rpm = calc_rpm(count, tach_interval)
            client.publish(rpm_topic, round(rpm), qos=0, properties=rpm_props)


if __name__ == "__main__":
    cfg = load_config()
    pi_inst, tach_state = setup_pigpio(cfg)
    client = setup_mqtt(cfg, pi_inst)

    # Graceful shutdown — set fan to 0, publish offline, disconnect
    def shutdown(signum, frame):
        print(f"Shutdown (signal {signum})")
        client.publish(
            cfg["topics"].get("status", "rack/fan/status"),
            "offline", qos=1, retain=True,
        )
        set_fan_speed(pi_inst, cfg["gpio"]["pwm"], cfg["pwm"]["frequency"], 0)
        client.disconnect()
        pi_inst.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    run_loop(client, cfg, tach_state)
