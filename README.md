# Quiet Rack Fan Controller

PID-controlled PWM fan for a 12U wall-mount network cabinet. Replaces a noisy always-on AC muffin fan with a quiet, temperature-proportional DC fan driven by Home Assistant over MQTT.

**~$32 in new parts.** The fan runs at exactly the speed it needs to — no more bang-bang on/off cycling.

## How It Works

```
NAS temp sensor → HA PID controller → MQTT → Pi Zero W → PWM → Arctic P12 fan
                                                  ↑                    │
                                                  └── RPM telemetry ───┘
```

- **Control loop** lives in Home Assistant (HACS `simple_pid_controller`)
- **Pi Zero W** is a pure actuator — subscribes to a speed topic, sets PWM duty cycle via `pigpio`
- **Tach feedback** (optional) publishes RPM back to HA for monitoring and failure alerts

## Hardware

| Component | Role |
| --- | --- |
| Arctic P12 PWM 120mm | Quiet DC fan (22 dBA max) |
| Raspberry Pi Zero W | MQTT-to-PWM bridge |
| 12V 1A wall adapter | Fan power |
| USB charger (5V) | Pi power |

Full BOM with prices in the [design guide](rack_fan_guide_1.md#4-bill-of-materials).

## Quick Start

### 1. Pi Zero W setup

```bash
# Flash Pi OS Lite, SSH in, then:
sudo apt install -y pigpio python3-pip python3-yaml
sudo pip3 install paho-mqtt --break-system-packages
sudo systemctl enable pigpiod && sudo systemctl start pigpiod
```

### 2. Deploy the controller

```bash
cd pi/
cp config.example.yaml config.yaml
# Edit config.yaml with your MQTT broker IP, credentials, GPIO pins
```

```bash
# Copy to the Pi
scp fan_controller.py config.yaml pi@rack-fan:/home/pi/
scp fan-controller.service pi@rack-fan:/tmp/
ssh pi@rack-fan 'sudo mv /tmp/fan-controller.service /etc/systemd/system/ && sudo systemctl enable --now fan-controller'
```

### 3. Home Assistant

1. Install [simple_pid_controller](https://github.com/bvweerd/simple_pid_controller) via HACS
2. Create an `input_number.rack_fan_speed` helper (0–100, step 1)
3. Add the MQTT entities from [`homeassistant/mqtt.yaml`](homeassistant/mqtt.yaml)
4. Configure the PID controller (Kp=5, Ki=0.05, Kd=1, setpoint=35°C)
5. Add the bridge automation from [`homeassistant/automation_pid_to_mqtt.yaml`](homeassistant/automation_pid_to_mqtt.yaml)

Full walkthrough with PID tuning guide in the [design document](rack_fan_guide_1.md).

## Repository Layout

```
quiet-rack/
├── pi/                                        # Runs on the Pi Zero W
│   ├── fan_controller.py                      # MQTT subscriber + PWM driver
│   ├── fan-controller.service                 # systemd unit
│   ├── config.example.yaml                    # Config template (checked in)
│   └── config.yaml                            # Your credentials (gitignored)
├── homeassistant/                             # HA config snippets
│   ├── mqtt.yaml                              # MQTT sensor + number entity
│   ├── automation_pid_to_mqtt.yaml            # PID output → MQTT bridge
│   └── automation_fan_failure_alert.yaml      # RPM watchdog alert
├── rack_fan_guide_1.md                        # Full design document
└── rack_fan_guide_1.docx                      # Original (Word)
```

## Credentials

Site-specific config (MQTT host, credentials, GPIO pins) lives in `pi/config.yaml`, which is **gitignored**. Copy `pi/config.example.yaml` and fill in your values. See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## Documentation

- [Design guide](rack_fan_guide_1.md) — the single reference: hardware decisions, wiring, Pi setup, HA config, PID tuning, build checklist
- [Debugging guide](DEBUGGING.md) — bench testing with an LED fan simulator, oscilloscope probing, MQTT and pigpio diagnostics

## License

[MIT](LICENSE) — Scott Francis, 2026
