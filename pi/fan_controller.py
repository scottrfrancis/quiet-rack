#!/usr/bin/env python3
"""Rack fan PWM controller — MQTT subscriber that drives an Arctic P12 via pigpio."""

import pathlib
import sys
import time

import yaml
import paho.mqtt.client as mqtt


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
    """MQTT on_connect — subscribe to the speed topic.

    Called on initial connect AND on every reconnect. The subscribe here
    ensures we re-subscribe after broker restarts or network drops.
    """
    print("MQTT connected, rc=", reason_code)
    if reason_code == 0 or str(reason_code) == "Success":
        client.subscribe(userdata["speed_topic"])
    else:
        print(f"MQTT connect failed: {reason_code}")


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    """MQTT on_disconnect — log disconnection.

    paho-mqtt v2 callback signature: (client, userdata, disconnect_flags, reason_code, properties).
    paho-mqtt handles reconnection automatically (via reconnect_delay_set).
    This callback is for logging only.
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
        # Use a mutable list as a counter so the callback can increment it
        tach_state = {"pulse_count": [0]}

        def tach_pulse(gpio, level, tick):
            tach_state["pulse_count"][0] += 1

        pi_inst.set_mode(tach_gpio, pigpio.INPUT)
        pi_inst.set_pull_up_down(tach_gpio, pigpio.PUD_UP)
        pi_inst.callback(tach_gpio, pigpio.FALLING_EDGE, tach_pulse)

    return pi_inst, tach_state


def setup_mqtt(cfg, pi_inst):
    """Create MQTT client, set callbacks, connect. Returns client.

    The pi_inst and config values are passed to callbacks via userdata.
    """
    userdata = {
        "pi_inst": pi_inst,
        "pwm_gpio": cfg["gpio"]["pwm"],
        "pwm_freq": cfg["pwm"]["frequency"],
        "speed_topic": cfg["topics"]["speed"],
    }

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, userdata=userdata)
    user = cfg["mqtt"].get("user")
    password = cfg["mqtt"].get("password")
    if user:
        client.username_pw_set(user, password)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    # Auto-reconnect with exponential backoff: 1s initial, 120s max
    client.reconnect_delay_set(min_delay=1, max_delay=120)
    client.connect(cfg["mqtt"]["host"], cfg["mqtt"].get("port", 1883))
    return client


def run_loop(client, cfg, tach_state):
    """Main loop: publish RPM every tach interval. Blocking."""
    tach_gpio = cfg["gpio"].get("tach")
    tach_interval = cfg.get("tach", {}).get("interval", 10)
    rpm_topic = cfg["topics"]["rpm"]

    client.loop_start()

    while True:
        time.sleep(tach_interval)
        if tach_gpio is not None and tach_state is not None:
            count = tach_state["pulse_count"][0]
            tach_state["pulse_count"][0] = 0
            rpm = calc_rpm(count, tach_interval)
            client.publish(rpm_topic, round(rpm))


if __name__ == "__main__":
    cfg = load_config()
    pi_inst, tach_state = setup_pigpio(cfg)
    client = setup_mqtt(cfg, pi_inst)
    run_loop(client, cfg, tach_state)
