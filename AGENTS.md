# Agents — Quiet Rack Fan Controller

Instructions for AI coding agents (Claude Code, Copilot, Cursor, etc.) working on this project.

## Project Context

Single-fan PID controller for a 12U rack cabinet. Pi Zero W acts as a dumb MQTT-to-PWM bridge; all control logic lives in Home Assistant. The design guide (`rack_fan_guide_1.md`) is the authoritative reference.

## Architecture Constraints

- **Pi Zero W is a pure actuator.** It subscribes to an MQTT topic and sets a PWM duty cycle. It does not run a PID loop, make control decisions, or read sensors directly.
- **PID lives in Home Assistant.** The `simple_pid_controller` HACS integration owns the control loop. Do not duplicate this logic on the Pi.
- **Two isolated power rails.** 12V for the fan, 5V USB for the Pi, shared GND only. Do not suggest buck converters or power rail merges.
- **Target hardware is Pi Zero W (ARMv6, 512MB RAM).** Keep dependencies minimal. No numpy, no asyncio frameworks, no heavy libraries.

## File Layout

| Path | Purpose | Runs on |
| --- | --- | --- |
| `pi/fan_controller.py` | MQTT subscriber + pigpio PWM driver | Pi Zero W |
| `pi/fan-controller.service` | systemd unit file | Pi Zero W |
| `pi/config.example.yaml` | Config template (checked in) | Reference |
| `pi/config.yaml` | Real credentials (gitignored) | Pi Zero W |
| `homeassistant/*.yaml` | HA config snippets | Home Assistant |
| `rack_fan_guide_1.md` | Full design document | Reference |

## Credential Handling

- **Never hardcode** MQTT credentials, IP addresses, or device names in source files
- All site-specific values come from `pi/config.yaml` (gitignored)
- The checked-in template is `pi/config.example.yaml`
- If adding a new config value: add to both the example template and the loader in `fan_controller.py`

## Code Guidelines

- Python 3 — compatible with Pi OS Lite system Python (3.11+)
- `pigpio` for hardware PWM (25kHz on GPIO18) — do not use RPi.GPIO or gpiozero (neither supports hardware PWM at 25kHz)
- `paho-mqtt` for MQTT — do not switch to asyncio MQTT without good reason
- `PyYAML` for config — already available on Pi OS Lite
- Keep `fan_controller.py` as a single file — no package structure needed for a single-purpose daemon
- Tach is optional — gated on `gpio.tach` being non-null in config

## Home Assistant Config

- YAML snippets in `homeassistant/` are meant to be copied into the user's HA config
- Mark site-specific values (entity names, notification targets) with comments
- Do not generate full HA `configuration.yaml` files — only the fragments this project adds

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/). Prefix: `feat:`, `fix:`, `docs:`, `chore:`.

## Deployment Model

Code is developed on the workstation and deployed to the Pi Zero W over SSH/SCP. The Pi hostname is `rack-fan` (configurable in Pi Imager). Typical workflow:

```bash
# Copy updated files to the Pi
scp pi/fan_controller.py pi/config.yaml pi@rack-fan:/home/pi/

# Restart the service after changes
ssh pi@rack-fan 'sudo systemctl restart fan-controller'

# Tail logs
ssh pi@rack-fan 'journalctl -u fan-controller -f'
```

- The Pi is headless — no monitor, no keyboard. All interaction is over SSH.
- The systemd unit (`fan-controller.service`) is deployed once; code updates only need the `.py` and `.yaml` files copied.
- If asked to "deploy" or "install" something on the Pi, generate the `scp`/`ssh` commands — do not assume local execution on the Pi.

## Home Assistant Access

HA config is accessible via three channels. Connection details are in `pi/config.yaml` under `homeassistant:`.

| Method | How | Use for |
| --- | --- | --- |
| **Samba** | Mount at `/Volumes/config` | Edit YAML config, read SQLite history DB |
| **SSH** | `ssh hassio@homeassistant.local` | Edit config, restart HA, tail logs |
| **REST API** | `curl` with long-lived token | Query/set entity state, create helpers |

- Config files live at `/config/` on the HA filesystem (both Samba and SSH)
- The SQLite DB (`home-assistant_v2.db`) must be opened with `?mode=ro&immutable=1` to bypass HA's write lock
- The `ha` CLI on the SSH session requires an API token for most commands
- HACS integration installs and token generation require the HA browser UI (one-time operations)

## What Not To Do

- Do not add a web UI, REST API, or local dashboard — HA is the UI
- Do not add a local PID loop — the architecture deliberately puts PID in HA
- Do not add Docker, containers, or virtualenvs — this runs as a bare systemd service on Pi OS Lite
- Do not add CI/CD — there are no automated tests for hardware-dependent code
- Do not create files in the project directory for testing — use `/tmp`
