# Quiet Rack Fan Controller

A Practical IoT Project: 12U Wall-Mount Cabinet · Pi Zero W · MQTT · PID

Scott Francis · March 2026

---

## 1. Project Overview

A 12U wall-mount network cabinet in the garage came with a noisy 120mm AC-powered muffin fan. The goal: replace it with a quiet, smart fan that is temperature-controlled with a real PID loop — not a simple on/off relay or a lookup table.

The result is an end-to-end IoT system: a 12V DC PWM fan, a Raspberry Pi Zero W running pigpio and a Python MQTT bridge, and a Home Assistant PID controller that feeds the NAS temperature sensor directly into the control loop.

> **TIP:** This document covers everything: the decision chain, full wiring, all code, Home Assistant configuration, PID tuning, and notes for a blog post. It is meant to be the single reference for this project.

## 2. Problem Statement

The cabinet is a standard 12U steel wall-mount enclosure (Amazon B07PGHN8LY). It came populated with a single 120mm AC muffin fan in one of two top knockout positions. The fan is mains-powered (120V AC), single-speed, always on, and loud.

### Existing HA Automation

A smart plug already drives the fan on/off based on a temperature reading from the NAS inside the cabinet. This works but has two problems:

- It is bang-bang control — full speed or off. The fan cycles audibly.
- There is no speed proportioning — the fan cannot run quietly at low load.

### Design Goals

- Replace the AC muffin fan with a quiet 12V DC PWM fan
- Drive fan speed continuously (0–100%) via PWM from a small microcontroller
- Keep the control loop in Home Assistant using the existing NAS temp feed
- Use a real PID controller — proportional, integral, and derivative terms
- Minimise new components, cost, and wiring complexity
- Mount cleanly inside the cabinet — no flying modules, no breadboards

## 3. Hardware Decisions

### 3.1 Fan Selection

The original fan was a 120V AC induction motor. AC muffin fans cannot be speed-controlled by PWM — they require either a TRIAC dimmer (which causes motor whine) or a 0-10V control input (HVAC-class fans only). The solution is to switch to a 12V DC 4-pin PWM fan, which is purpose-built for continuous variable speed control.

#### Fan Shortlist

| Fan | Price | Max Noise | Verdict |
| --- | --- | --- | --- |
| Arctic P12 PWM | ~$9 | 22 dBA | **Selected** — best value |
| Noctua NF-A12x25 G2 | ~$35 | 17 dBA | Overkill for garage |
| Noctua Redux NF-P12 | ~$15 | 25 dBA | Older design, no advantage |
| NavePoint AC rack fan | ~$18 | 43–47 dBA | Rejected — no improvement |

The Arctic P12 PWM is the correct choice for a garage environment. The Noctua NF-A12x25 is engineered to such tolerances that Noctua sells a kit specifically to offset the RPMs of two units to prevent them harmonising — precision that adds zero value in a garage rack. The Arctic P12 is practically silent at 60–65% PWM and carries a 10-year warranty.

> **WARNING:** The Arctic P12 PWM (standard, not Max) is the correct choice. The P12 Max has a rasping noise at all RPMs from its higher-speed design. Stick with the standard P12 PWM.

### 3.2 Controller Selection

The cabinet requires a microcontroller with: (a) 25kHz hardware PWM on a GPIO pin, (b) WiFi for MQTT connectivity to Home Assistant, and (c) a physical form factor that can live inside the cabinet without a rat's nest of adapters. Fourteen boards were evaluated.

#### Board Elimination Table

| Board | WiFi | PWM | Verdict |
| --- | --- | --- | --- |
| Arduino 101 | No | Yes | Rejected — no WiFi, discontinued |
| Teensy 3.6 | No | Yes | Rejected — no WiFi, massive overkill |
| BeagleBone Green | No | Yes | Rejected — Linux, no onboard WiFi |
| nRF7002 DK | WiFi 6 | Yes | Rejected — Nordic SDK, wrong fit |
| Arduino Nano Matter | Thread | Yes | Rejected — needs border router |
| Particle Electron | Cellular | Yes | Rejected — cellular, paid per MB |
| ESP-01 | Yes | Limited | Rejected — 2 GPIO, fragile to flash |
| RP2040 (bare) | No | Yes | Rejected — needs ESP-01 piggyback |
| Circuit Playground Express | No | Yes | Rejected — no WiFi |
| Arduino OSEPP Uno R4 | Yes (some) | Limited | Rejected — limited PWM options |
| Pi Pico W | Yes | Yes | Runner-up — strong candidate |
| Pi Zero W | Yes | Yes (GPIO18) | **Selected** |

#### Why Pi Zero W Won

The Pi Zero W prevailed on practical grounds, not technical ones:

- A Pi Zero W case is available for $5 vs $10 for a Pi Pico W case — and electrical isolation/short protection inside a metal cabinet is non-negotiable
- The board was already owned — zero additional spend
- Python + paho-mqtt + pigpio is more maintainable long-term than MicroPython on Pico W
- SSH access for re-flashing without opening the rack
- The Linux overhead (boot time, SD card) is acceptable for a garage cabinet

> **WARNING:** The one real risk with Pi Zero W is SD card failure in a hot environment. Mitigate with a Samsung PRO Endurance or SanDisk High Endurance card (~$10), designed for dashcam/NVR continuous-write workloads. Mount rootfs read-only and log to tmpfs.

### 3.3 Power Strategy

Two separate purpose-built supplies. No buck converters, no spliced rails, nothing homebrew in the power chain:

- **12V 1A wall adapter** — fan power. Barrel jack connector, tidy termination.
- **USB charger** (existing) → USB-A to micro-USB cable → Pi Zero W. Standard connectors end to end.

The only non-obvious wiring step is sharing ground between the two supplies. Without a common ground, the Pi's PWM signal has no reference and the fan will not respond. This is a single short jumper wire from Pi GND to the 12V supply GND, not a full power rail merge.

## 4. Bill of Materials

| Item | Source | Approx Cost | Notes |
| --- | --- | --- | --- |
| Arctic P12 PWM 120mm fan | Amazon | $9 | Standard, not Max or Max PWM |
| Raspberry Pi Zero W | On hand | $0 | With headers preferred |
| Pi Zero W case | Amazon | $5 | Electrical isolation inside cabinet |
| Samsung PRO Endurance 32GB microSD | Amazon | $10 | Heat-rated for continuous write |
| 12V 1A wall adapter (barrel jack) | Amazon / drawer | $8 | Powers fan |
| USB charger (5V) | On hand | $0 | Powers Pi Zero W |
| Micro-USB cable | On hand | $0 | Pi power |
| 4-pin PWM fan extension / pigtail | Amazon | $1–2 | Clean termination on fan side |
| Short jumper wire | On hand | $0 | Ground bond: Pi GND → 12V GND |
| Velcro or dual-lock strips | On hand | $0 | Mounts Pi Zero W inside cabinet top |

**Total new spend: ~$32–34** (fan + case + SD card + 12V adapter). Everything else from existing stock.

## 5. System Architecture

### 5.1 Data Flow

The control loop is entirely within Home Assistant. The Pi Zero W is a pure actuator — it receives a speed percentage over MQTT and translates it to a PWM duty cycle. It does not run a PID loop locally.

```text
NAS temperature sensor (existing HA entity)
  │
  ▼
simple_pid_controller (HACS custom integration)
  setpoint: 35°C   Kp=5  Ki=0.05  Kd=1
  │
  ▼  PID output (0–100)
input_number.rack_fan_speed (HA Helper entity)
  │
  ▼  Automation trigger (state change)
mqtt.publish → topic: rack/fan/speed
  │
  ▼  WiFi / Mosquitto
Pi Zero W
  paho-mqtt subscriber → fan_controller.py
  pigpio.hardware_PWM(GPIO18, 25000, duty)
  │
  ▼  4-pin header
Arctic P12 PWM fan
  │
  ▼  (optional)
Tach pulse → GPIO24 → RPM calculation
  │
  ▼  mqtt.publish
rack/fan/rpm → HA sensor.rack_fan_rpm
```

### 5.2 Why PID in HA, not on the Pi

Running the PID controller in Home Assistant rather than on the Pi Zero W has several advantages:

- The NAS temperature sensor is already an HA entity — no additional sensor hardware needed
- PID tuning is done live in the HA UI — no SSH, no code restarts
- The P, I, and D terms are visible as HA diagnostic entities for real-time observation
- If the Pi reboots, the fan resumes its last MQTT-retained speed within seconds of reconnect
- The control algorithm can be changed or extended without touching the Pi at all

## 6. Wiring

### 6.1 4-Pin PWM Fan Connector Pinout

| Pin | Function | Wire Color (typical) | Connects to |
| --- | --- | --- | --- |
| 1 | GND | Black | 12V adapter GND + Pi GND (shared) |
| 2 | 12V | Yellow | 12V adapter positive |
| 3 | Tach (RPM output) | Green | Pi GPIO24 (optional — open circuit if unused) |
| 4 | PWM input | Blue | Pi GPIO18 |

### 6.2 Wiring Diagram

![Wiring diagram showing Pi Zero W, Arctic P12 fan, 12V adapter, and USB charger connections](media/image1.png)

*Figure 1 — Complete wiring diagram. Note the shared GND node connecting Pi, 12V adapter, and fan.*

> **WARNING:** The shared GND jumper between Pi and 12V supply is the most commonly forgotten step. Without it, the fan ignores the PWM signal entirely because there is no common voltage reference.

### 6.3 Physical Mounting

- **Pi Zero W in case:** velcro or dual-lock to inside top panel of cabinet, adjacent to the fan knockout
- **12V adapter and USB charger:** plugged into the power strip already in the cabinet
- **Fan wiring:** route inside the cabinet top — keep PWM wire short (under 30cm) to minimise RF pickup
- **Tach wire:** if used, keep away from the PWM wire to avoid false pulse counts
- No wiring exits the cabinet except the two power supply cords — same as before

## 7. Raspberry Pi Zero W Setup

### 7.1 OS and Base Configuration

1. Flash Raspberry Pi OS Lite (32-bit) to the Samsung Endurance SD card using Pi Imager
2. In Pi Imager Advanced Settings: enable SSH, set hostname (e.g. `rack-fan`), set WiFi credentials
3. Boot, SSH in, run: `sudo apt update && sudo apt upgrade -y`
4. Set up read-only filesystem (optional but recommended for garage environment — see Section 7.4)

### 7.2 Install Dependencies

```bash
sudo apt install -y pigpio python3-pip python3-yaml
sudo pip3 install paho-mqtt --break-system-packages
sudo systemctl enable pigpiod
sudo systemctl start pigpiod
```

### 7.3 Fan Controller Script

The controller reads all site-specific values from `config.yaml`. Copy the template and fill in your credentials:

```bash
cp pi/config.example.yaml pi/config.yaml
# edit pi/config.yaml with your MQTT host, user, password
```

`config.yaml` is gitignored — credentials never leave the Pi. The script, systemd unit, and example config are in `pi/` in the repo. See `pi/fan_controller.py` for the full source.

Deploy to the Pi:

```bash
scp pi/fan_controller.py pi/config.yaml pi@rack-fan:/home/pi/
```

### 7.4 Systemd Service

Save as `/etc/systemd/system/fan-controller.service`:

```ini
[Unit]
Description=Rack Fan Controller
After=network.target pigpiod.service

[Service]
ExecStart=/usr/bin/python3 /home/pi/fan_controller.py
Restart=always
RestartSec=10
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable fan-controller
sudo systemctl start fan-controller
sudo systemctl status fan-controller  # verify running
```

### 7.5 SD Card Longevity (Garage Environment)

A hot garage + constant power = elevated SD card failure risk. Two mitigations:

- **Hardware:** Samsung PRO Endurance 32GB — rated for 43,800 hours of continuous write at high temperatures. Designed for dashcams and security cameras.
- **Software:** mount the root filesystem read-only and redirect logs to tmpfs. Search for 'Raspberry Pi read-only filesystem' — there are several well-tested scripts. Key mounts to redirect: `/var/log`, `/tmp`, `/var/tmp`.

> **TIP:** Keep a backup disk image (`dd` or Pi Imager) on the NAS. If the SD card fails, recovery is flash-and-go rather than rebuild-from-scratch.

## 8. Home Assistant Configuration

### 8.1 Install simple_pid_controller via HACS

1. In HACS → Integrations → ⋮ → Custom Repositories
2. Add: `https://github.com/bvweerd/simple_pid_controller` (category: Integration)
3. Click Install, then restart Home Assistant

### 8.2 Create the Output Helper

Settings → Helpers → Add Helper → Number:

| Field | Value |
| --- | --- |
| Name | Rack Fan Speed |
| Minimum | 0 |
| Maximum | 100 |
| Step | 1 |
| Unit of measurement | % |

This creates the entity `input_number.rack_fan_speed` — the bridge between the PID output and the MQTT publish.

### 8.3 Configure the PID Controller

Settings → Devices & Services → Add Integration → Simple PID Controller:

| Parameter | Value | Notes |
| --- | --- | --- |
| Name | Rack Fan PID | |
| Input sensor | `sensor.nas_temperature` | Your existing NAS temp entity |
| Output entity | `input_number.rack_fan_speed` | The helper created above |
| Setpoint | 35 | Target °C — adjust to taste |
| Output min | 15 | Never fully stop while PID is active |
| Output max | 100 | Full speed ceiling |
| Sample time | 30 | Seconds between PID updates |
| Kp | 5 | Starting value — tune from here |
| Ki | 0.05 | Very gentle — see tuning section |
| Kd | 1 | Light damping — thermal systems are slow |

> **TIP:** All PID gains are live-editable in the HA UI — no restarts needed after initial setup. The P, I, and D term values are exposed as diagnostic entities for real-time observation during tuning.

### 8.4 MQTT Configuration (configuration.yaml)

Add the fan RPM sensor and the fan speed number entity:

```yaml
mqtt:
  sensor:
    - name: 'Rack Fan RPM'
      state_topic: 'rack/fan/rpm'
      unit_of_measurement: 'RPM'
  number:
    - name: 'Rack Fan Speed'
      command_topic: 'rack/fan/speed'
      min: 0
      max: 100
      step: 5
      retain: true  # fan resumes last speed on Pi reboot
```

### 8.5 Bridge Automation

This is the only automation needed — it watches the PID output and publishes it to MQTT. Replace the existing smart plug on/off automation with this:

```yaml
alias: Rack Fan PID to MQTT
trigger:
  - platform: state
    entity_id: input_number.rack_fan_speed
condition: []
action:
  - service: mqtt.publish
    data:
      topic: rack/fan/speed
      retain: true
      payload: "{{ states('input_number.rack_fan_speed') | int }}"
mode: single
```

### 8.6 Optional: Fan Failure Alert

Add an automation to alert if the fan stops spinning while speed > 0:

```yaml
alias: Rack Fan Failure Alert
trigger:
  - platform: numeric_state
    entity_id: sensor.rack_fan_rpm
    below: 100
    for: '00:01:00'
condition:
  - condition: numeric_state
    entity_id: input_number.rack_fan_speed
    above: 20
action:
  - service: notify.mobile_app_YOUR_PHONE
    data:
      message: 'Rack fan RPM dropped below 100 while speed > 20%'
```

## 9. PID Tuning Guide

### 9.1 Understanding the Terms

| Term | What it does | Too high | Too low |
| --- | --- | --- | --- |
| Kp (Proportional) | Immediate reaction proportional to error | Oscillation | Sluggish response |
| Ki (Integral) | Accumulates error over time to eliminate steady-state offset | Integral windup, overshoot | Persistent offset from setpoint |
| Kd (Derivative) | Damps rate of change — prevents overshoot | Noise amplification | Overshoot on fast changes |

### 9.2 Why This System is Slow

Thermal systems in a cabinet have a very long time constant — temperature changes unfold over minutes, not seconds. This means:

- Kd contributes almost nothing useful — D reacts to rate of change, and the rate here is very slow
- Ki is the workhorse — it steadily winds up to hold the setpoint against a sustained heat load
- The sample time of 30 seconds is appropriate — sampling faster just adds noise with no benefit

### 9.3 Tuning Procedure

1. Start with Kp=5, Ki=0, Kd=0. Watch the P diagnostic entity in the HA dashboard.
2. If the fan barely reacts when temp is 2–3°C above setpoint, increase Kp. If it oscillates, halve Kp.
3. Once Kp is settled, add Ki=0.01. Watch the I term slowly accumulate and pull the temperature to the setpoint.
4. Increase Ki in steps (0.01 → 0.03 → 0.05) until the temperature holds within ~1°C of setpoint.
5. Leave Kd=0 or set to 1. For this application it will have negligible effect.

### 9.4 Integral Windup

Integral windup occurs when the integrator accumulates a large value while the output is clamped at its minimum or maximum, then causes a large surge when conditions change. The simple_pid_controller integration includes built-in windup protection — the integrator is clamped to the output limits. The output_min of 15 also helps: the fan never fully stops while the PID is active, which prevents the integrator from winding up against a stalled fan.

> **WARNING:** Set output_min to ~15, not 0. A fan that never fully stops while the PID is active avoids a common failure mode: integrator winds up to a very high value while fan is stopped, then surges to 100% when temperature finally rises enough to kick in.

## 10. Build Sequence / Work Log

Step-by-step sequence for executing the build. Use this as a checklist.

### Phase 1 — Parts

- [ ] Order Arctic P12 PWM fan
- [ ] Order Pi Zero W case ($5)
- [ ] Order Samsung PRO Endurance 32GB microSD
- [ ] Order 12V 1A wall adapter with barrel jack
- [ ] Order 4-pin PWM fan pigtail/extension header

### Phase 2 — Pi Zero W Prep

- [ ] Flash Pi OS Lite to SD card via Pi Imager (set hostname, SSH, WiFi in Imager)
- [ ] Boot, SSH in, run apt update/upgrade
- [ ] Install pigpio and paho-mqtt
- [ ] Enable and start pigpiod
- [ ] Deploy fan_controller.py
- [ ] Install and enable systemd service
- [ ] Verify MQTT connectivity: `mosquitto_sub -h HA_IP -t rack/fan/speed`
- [ ] Test: manually publish a speed value, confirm fan responds

### Phase 3 — HA Configuration

- [ ] Install simple_pid_controller via HACS
- [ ] Create input_number.rack_fan_speed Helper
- [ ] Add MQTT sensor and number to configuration.yaml, restart HA
- [ ] Configure PID controller (input sensor, output entity, gains)
- [ ] Create bridge automation (input_number → mqtt.publish)
- [ ] Verify: trigger NAS temp change, confirm fan speed changes
- [ ] (Optional) Add fan failure alert automation

### Phase 4 — Physical Install

- [ ] Remove old AC fan from knockout
- [ ] Mount Arctic P12 in knockout (fan guards if needed)
- [ ] Wire fan: 12V to pin 2, GND to pin 1, GPIO18 to pin 4, optional tach to GPIO24
- [ ] Bond Pi GND to 12V supply GND (short jumper wire)
- [ ] Mount Pi Zero W in case, velcro to inside top panel
- [ ] Route wiring neatly, cable-tie to top panel
- [ ] Plug in USB charger and 12V adapter to cabinet power strip
- [ ] Power up, verify fan spins at expected speed
- [ ] Remove old smart plug from cabinet power strip

### Phase 5 — Tuning

- [ ] Observe NAS temp sensor and fan speed in HA dashboard over 24–48 hours
- [ ] Adjust Kp until response feels appropriately fast without oscillation
- [ ] Add Ki incrementally until temp holds within ~1°C of setpoint
- [ ] Note final Kp, Ki, Kd values in this document

### Final PID Values (fill in after tuning)

| Parameter | Initial | Final (after tuning) | Notes |
| --- | --- | --- | --- |
| Setpoint | 35°C | ____ | |
| Kp | 5 | ____ | |
| Ki | 0.05 | ____ | |
| Kd | 1 | ____ | |
| Output min | 15 | ____ | |
| Sample time | 30s | ____ | |

## 11. Blog Post Notes & Talking Points

Background material and angle notes for a project write-up.

### 11.1 Framing / Hook

- The opening problem is universal: cheap hardware in a home lab that is loud and dumb
- The smart plug on/off approach is how most people solve this — and it's wrong in an interesting way. It looks like it works but it's actually bang-bang control. Introduce that concept.
- 'Bang-bang' vs PID is the central intellectual arc of the post — accessible to non-engineers because everyone has experienced a thermostat that cycles on and off vs one that is always quietly running

### 11.2 Key Narrative Beats

- The board evaluation section is good content — 14 boards, ruled out one by one on practical grounds, not spec-sheet grounds. The interesting decision was Pi Zero W over Pi Pico W, driven entirely by a $5 case.
- 'Where things usually fall down' — the power supply and mounting question. This is the most practical and relatable section for maker readers. The buck converter vs two separate supplies decision is worth expanding.
- The shared GND gotcha is a good 'aha' moment — a one-wire fix that most guides skip over
- AC fan PWM myth — most people assume you can just PWM any fan. You can't. The distinction between AC induction motors and DC brushless 4-pin fans is worth a short explainer.

### 11.3 Technical Depth Options

- **Shallow read:** hardware decisions, BOM, photos of the finished install
- **Medium read:** full wiring walkthrough, Pi setup, HA configuration
- **Deep dive:** PID theory — P vs PI vs PID, integral windup, why D term barely helps in slow thermal systems

The three-layer structure works well for a single post with expandable sections, or as a three-part series.

### 11.4 Potential Titles

- "Killing the Fan Noise in My Home Lab Rack (and Learning PID Control Along the Way)"
- "Why Your Smart Plug Fan Automation is Bang-Bang Control (and How to Fix It)"
- "$34 Quiet Rack Fan with Real PID Control via Home Assistant"
- "From Noisy AC Fan to Quiet IoT Fan Controller: A Complete Build Guide"

### 11.5 Photos / Media to Capture

- Before: the original loud AC fan installed in the knockout
- Parts laid out: Pi Zero W + case, Arctic P12, 12V adapter, SD card
- Wiring detail: the 4-pin connector and ground bond jumper wire
- Pi Zero W in case, velcroed to cabinet top panel
- Finished install: fan in knockout, Pi case mounted, wiring dressed
- HA Dashboard: NAS temp graph, fan speed slider, RPM sensor, PID diagnostic entities
- PID tuning in action: screenshot of P/I/D term graphs during a temperature rise

### 11.6 SEO / Search Terms

- quiet 120mm rack fan home assistant
- pi zero w pwm fan controller
- home assistant PID fan control MQTT
- simple_pid_controller HACS fan
- 12U wall mount rack fan replacement
- raspberry pi pigpio pwm fan

## 12. Troubleshooting

| Symptom | Likely Cause | Fix |
| --- | --- | --- |
| Fan does not spin at all | No 12V supply, or wrong fan pin wiring | Check 12V adapter, verify pin 1/2 connections |
| Fan runs full speed always | PWM pin not reaching fan, or wrong duty cycle scale | Check GPIO18 wiring; verify pigpio duty cycle (0–1,000,000 not 0–100) |
| Fan speed does not respond to MQTT | Missing shared GND between Pi and 12V supply | Add jumper wire from Pi GND to 12V adapter GND |
| fan_controller.py crashes at startup | pigpiod not running | `sudo systemctl start pigpiod` |
| RPM reads 0 when fan is spinning | Tach wire not connected, or missing pull-up resistor | Verify pin 3 wiring; add 10kΩ pull-up to 3.3V |
| PID output oscillates wildly | Kp too high | Halve Kp, wait 5 minutes, observe |
| Temperature stays above setpoint | Ki too low or 0 | Increase Ki gradually (0.01 steps) |
| Fan surges to 100% on startup | Integral windup during stopped period | Set output_min=15, enable PID windup protection in config |
| Pi Zero W not connecting to MQTT | Wrong credentials, or HA MQTT broker not listening on 1883 | Check mosquitto config; test with mosquitto_pub from another host |
| SD card corruption after power cycle | Write in progress at power-off | Implement read-only rootfs; use Samsung Endurance SD card |

## 13. References & Further Reading

- [simple_pid_controller HACS integration](https://github.com/bvweerd/simple_pid_controller)
- [simple-pid Python library](https://simple-pid.readthedocs.io) (underlying PID implementation)
- [pigpio Python library documentation](https://abyz.me.uk/rpi/pigpio/python.html)
- 4-pin PWM fan specification (Intel): defines 25kHz PWM frequency and signal levels
- Samsung PRO Endurance microSD: designed for 43,800 hours continuous write
- [Arctic P12 PWM product page](https://www.arctic.de/P12-PWM)
- [paho-mqtt Python client](https://pypi.org/project/paho-mqtt/)
- [Home Assistant MQTT Fan integration](https://www.home-assistant.io/integrations/fan.mqtt/)
- [Raspberry Pi Zero W read-only filesystem](https://learn.adafruit.com/read-only-raspberry-pi)

---

Scott Francis · The AI Lab · March 2026
