#!/usr/bin/env python3
"""Rack fan PWM controller — MQTT subscriber that drives an Arctic P12 via pigpio."""

import pigpio
import paho.mqtt.client as mqtt
import time

MQTT_HOST = 'YOUR_HA_IP'       # e.g. 192.168.1.100
MQTT_USER = 'YOUR_MQTT_USER'
MQTT_PASS = 'YOUR_MQTT_PASSWORD'
SPEED_TOPIC = 'rack/fan/speed'  # HA publishes here
RPM_TOPIC = 'rack/fan/rpm'      # Pi publishes here
PWM_GPIO = 18                   # hardware PWM pin
TACH_GPIO = 24                  # tach input (optional)
PWM_FREQ = 25000                # 25kHz per 4-pin fan spec

pi = pigpio.pi()
pi.set_mode(PWM_GPIO, pigpio.OUTPUT)
pi.hardware_PWM(PWM_GPIO, PWM_FREQ, 0)  # start stopped

# --- tach (optional) ---
pulse_count = 0

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
    print(f'Fan speed: {pct}%')

def on_connect(client, userdata, flags, rc):
    print('MQTT connected, rc=', rc)
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
client.connect(MQTT_HOST, 1883)
client.loop_start()

# Publish RPM every 10 seconds
while True:
    time.sleep(10)
    rpm = (pulse_count / 2) * 6  # 2 pulses/rev, 10s window
    pulse_count = 0
    client.publish(RPM_TOPIC, round(rpm))
