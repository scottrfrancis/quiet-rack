#!/usr/bin/env python3
"""
Host-side MQTT debug client for the rack fan controller.

Reads connection settings from pi/config.yaml (or a path you specify).
Run from your workstation — no Pi required.

Usage:
    # Monitor all rack/fan topics
    python tools/mqtt_debug.py monitor

    # Set fan speed to 50%
    python tools/mqtt_debug.py speed 50

    # Sweep speed from 0 to 100 in steps of 10, 3s between steps
    python tools/mqtt_debug.py sweep --min 0 --max 100 --step 10 --delay 3

    # Read the current retained speed value
    python tools/mqtt_debug.py retained

    # Clear retained speed message (resets to no retained value)
    python tools/mqtt_debug.py clear

    # Use test topics (rack/fan/test/speed, rack/fan/test/rpm)
    python tools/mqtt_debug.py --test monitor

    # Use a different config file
    python tools/mqtt_debug.py --config /path/to/config.yaml monitor
"""

import argparse
import pathlib
import sys
import time

import yaml
import paho.mqtt.client as mqtt

DEFAULT_CONFIG = pathlib.Path(__file__).parent.parent / "pi" / "config.yaml"


def load_config(path):
    if not path.exists():
        print(f"ERROR: config not found at {path}", file=sys.stderr)
        print(f"Copy pi/config.example.yaml to pi/config.yaml and fill in your values.", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


def make_client(cfg):
    client = mqtt.Client()
    client.username_pw_set(cfg["mqtt"]["user"], cfg["mqtt"]["password"])
    client.connect(cfg["mqtt"]["host"], cfg["mqtt"].get("port", 1883))
    return client


def cmd_monitor(cfg, args):
    """Subscribe to all rack/fan topics and print messages as they arrive."""
    def on_message(client, userdata, msg):
        retained = " [retained]" if msg.retain else ""
        print(f"{msg.topic}: {msg.payload.decode()}{retained}")

    client = make_client(cfg)
    client.on_message = on_message

    speed_topic = cfg["topics"]["speed"]
    rpm_topic = cfg["topics"]["rpm"]
    # Subscribe to the parent wildcard
    prefix = speed_topic.rsplit("/", 1)[0]
    client.subscribe(f"{prefix}/#")

    print(f"Monitoring {prefix}/# — Ctrl+C to quit")
    print(f"  speed topic: {speed_topic}")
    print(f"  rpm topic:   {rpm_topic}")
    print()

    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\nDone.")


def cmd_speed(cfg, args):
    """Publish a single speed value."""
    pct = max(0, min(100, args.percent))
    topic = cfg["topics"]["speed"]
    client = make_client(cfg)
    client.publish(topic, str(pct), retain=True)
    print(f"Published {pct}% to {topic} (retained)")
    client.disconnect()


def cmd_sweep(cfg, args):
    """Sweep fan speed from min to max, pausing between steps."""
    topic = cfg["topics"]["speed"]
    client = make_client(cfg)

    values = list(range(args.min, args.max + 1, args.step))
    if args.bounce:
        values = values + values[-2::-1]  # up then back down

    print(f"Sweeping {topic}: {values[0]}%–{values[-1]}%, step={args.step}, delay={args.delay}s")
    if args.bounce:
        print("  (bounce mode: up then back down)")

    try:
        for pct in values:
            client.publish(topic, str(pct), retain=True)
            print(f"  {pct}%")
            time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\nSweep interrupted.")

    print("Done.")
    client.disconnect()


def cmd_retained(cfg, args):
    """Read the current retained speed value and exit."""
    topic = cfg["topics"]["speed"]
    received = []

    def on_message(client, userdata, msg):
        retained = " [retained]" if msg.retain else " [live]"
        print(f"{msg.topic}: {msg.payload.decode()}{retained}")
        received.append(True)
        client.disconnect()

    client = make_client(cfg)
    client.on_message = on_message
    client.subscribe(topic)
    client.loop_start()

    # Wait up to 3 seconds for a retained message
    for _ in range(30):
        if received:
            break
        time.sleep(0.1)
    else:
        print(f"No retained message on {topic}")

    client.loop_stop()


def cmd_clear(cfg, args):
    """Clear the retained speed message by publishing an empty retained payload."""
    topic = cfg["topics"]["speed"]
    client = make_client(cfg)
    client.publish(topic, "", retain=True)
    print(f"Cleared retained message on {topic}")
    client.disconnect()


def main():
    parser = argparse.ArgumentParser(
        description="Host-side MQTT debug client for the rack fan controller."
    )
    parser.add_argument(
        "--config", type=pathlib.Path, default=DEFAULT_CONFIG,
        help=f"Path to config.yaml (default: {DEFAULT_CONFIG})"
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Use test topic namespace (rack/fan/test/*) instead of live topics"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("monitor", help="Subscribe to all rack/fan topics and print messages")

    sp_speed = sub.add_parser("speed", help="Set fan speed (0–100)")
    sp_speed.add_argument("percent", type=int, help="Speed percentage (0–100)")

    sp_sweep = sub.add_parser("sweep", help="Sweep fan speed through a range")
    sp_sweep.add_argument("--min", type=int, default=0, help="Start percent (default: 0)")
    sp_sweep.add_argument("--max", type=int, default=100, help="End percent (default: 100)")
    sp_sweep.add_argument("--step", type=int, default=10, help="Step size (default: 10)")
    sp_sweep.add_argument("--delay", type=float, default=3, help="Seconds between steps (default: 3)")
    sp_sweep.add_argument("--bounce", action="store_true", help="Sweep up then back down")

    sub.add_parser("retained", help="Read current retained speed value")
    sub.add_parser("clear", help="Clear retained speed message")

    args = parser.parse_args()
    cfg = load_config(args.config)

    if args.test:
        for key in ("speed", "rpm"):
            topic = cfg["topics"][key]
            # Insert /test/ before the leaf: rack/fan/speed → rack/fan/test/speed
            parts = topic.rsplit("/", 1)
            cfg["topics"][key] = f"{parts[0]}/test/{parts[1]}"
        print(f"[test mode] speed: {cfg['topics']['speed']}")
        print(f"[test mode] rpm:   {cfg['topics']['rpm']}")
        print()

    commands = {
        "monitor": cmd_monitor,
        "speed": cmd_speed,
        "sweep": cmd_sweep,
        "retained": cmd_retained,
        "clear": cmd_clear,
    }
    commands[args.command](cfg, args)


if __name__ == "__main__":
    main()
