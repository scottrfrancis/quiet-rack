#!/usr/bin/env python3
"""Rack fan PWM controller — MQTT subscriber that drives an Arctic P12 via pigpio."""

import pathlib
import sys

import yaml
import pigpio
import paho.mqtt.client as mqtt
import time

# --- config ---
CONFIG_PATH = pathlib.Path(__file__).parent / "config.yaml"
EXAMPLE_PATH = CONFIG_PATH.with_name("config.example.yaml")

if not CONFIG_PATH.exists():
    print(f"ERROR: {CONFIG_PATH} not found.", file=sys.stderr)
    print(f"Copy {EXAMPLE_PATH.name} to {CONFIG_PATH.name} and fill in your values.", file=sys.stderr)
    sys.exit(1)

with open(CONFIG_PATH) as f:
    cfg = yaml.safe_load(f)

MQTT_HOST = cfg["mqtt"]["host"]
MQTT_PORT = cfg["mqtt"].get("port", 1883)
MQTT_USER = cfg["mqtt"]["user"]
MQTT_PASS = cfg["mqtt"]["password"]
SPEED_TOPIC = cfg["topics"]["speed"]
RPM_TOPIC = cfg["topics"]["rpm"]
PWM_GPIO = cfg["gpio"]["pwm"]
TACH_GPIO = cfg["gpio"].get("tach")      # None disables tach
PWM_FREQ = cfg["pwm"]["frequency"]
TACH_INTERVAL = cfg.get("tach", {}).get("interval", 10)

# --- pigpio setup ---
pi = pigpio.pi()
pi.set_mode(PWM_GPIO, pigpio.OUTPUT)
pi.hardware_PWM(PWM_GPIO, PWM_FREQ, 0)  # start stopped

# --- tach (optional) ---
pulse_count = 0

if TACH_GPIO is not None:
    def tach_pulse(gpio, level, tick):
        global pulse_count
        pulse_count += 1

    pi.set_mode(TACH_GPIO, pigpio.INPUT)
    pi.set_pull_up_down(TACH_GPIO, pigpio.PUD_UP)
    pi.callback(TACH_GPIO, pigpio.FALLING_EDGE, tach_pulse)


def set_fan_speed(percent):
    pct = max(0, min(100, int(percent)))
    duty = pct * 10000  # pigpio: 0-1,000,000
    pi.hardware_PWM(PWM_GPIO, PWM_FREQ, duty)
    print(f"Fan speed: {pct}%")


def on_connect(client, userdata, flags, rc):
    print("MQTT connected, rc=", rc)
    client.subscribe(SPEED_TOPIC)


def on_message(client, userdata, msg):
    try:
        set_fan_speed(float(msg.payload.decode()))
    except ValueError:
        pass


client = mqtt.Client()
client.username_pw_set(MQTT_USER, MQTT_PASS)
client.on_connect = on_connect
client.on_message = on_message
client.connect(MQTT_HOST, MQTT_PORT)
client.loop_start()

# Publish RPM every interval
while True:
    time.sleep(TACH_INTERVAL)
    if TACH_GPIO is not None:
        rpm = (pulse_count / 2) * (60 / TACH_INTERVAL)  # 2 pulses/rev
        pulse_count = 0
        client.publish(RPM_TOPIC, round(rpm))
