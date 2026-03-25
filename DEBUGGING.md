# Debugging Guide

Bench-testing and instrumentation techniques for the rack fan controller. Use these before installing in the cabinet — it's much easier to probe signals on a desk than inside a 12U enclosure.

## 1. LED Fan Simulator

An LED + resistor on the PWM pin provides a visual proxy for fan speed during bench testing. No fan, no 12V supply needed — just the Pi.

### Circuit

```
Pi GPIO18 (pin 12) ──── 330Ω resistor ──── LED anode (+) ──── LED cathode (−) ──── Pi GND (pin 6)
```

```
      GPIO18                           GND
  (Pi pin 12)                      (Pi pin 6)
       │                                │
       │    ┌────────┐    ┌────────┐    │
       ├────┤ 330Ω R ├────┤  LED   ├────┤
       │    └────────┘    └────────┘    │
       │                  (+)    (-)    │
```

### Parts

- 1x LED (any color — green or blue is easiest to read in daylight)
- 1x 330Ω resistor (anything 220Ω–1kΩ works; lower = brighter)

### What to Expect

| PWM % | LED behavior |
| --- | --- |
| 0% | Off |
| 10–30% | Dim — may appear to flicker at low duty cycles |
| 50% | Medium brightness |
| 80–100% | Full brightness — indistinguishable above ~80% due to persistence of vision |

At 25kHz the LED will not visibly flicker — the switching frequency is far above the ~60Hz threshold of human perception. Brightness tracks duty cycle linearly.

### Quick Test (no MQTT needed)

SSH into the Pi and drive the LED directly with `pigs`:

```bash
# Start pigpiod if not running
sudo systemctl start pigpiod

# Set GPIO18 to 50% duty cycle at 25kHz
# pigpio duty range: 0–1,000,000
pigs hp 18 25000 500000    # 50%
pigs hp 18 25000 100000    # 10% — dim
pigs hp 18 25000 900000    # 90% — bright
pigs hp 18 25000 0         # off
```

This tests the full pigpio → GPIO → hardware PWM path without `fan_controller.py` or MQTT in the loop. If the LED responds correctly here, the hardware layer is solid.

### End-to-End Test with MQTT

With the LED circuit in place, run the full stack:

```bash
# Terminal 1: start the controller
python3 fan_controller.py

# Terminal 2: publish a speed value
mosquitto_pub -h YOUR_HA_IP -u YOUR_MQTT_USER -P YOUR_MQTT_PASS \
  -t rack/fan/speed -m 65
```

The LED should light to approximately 65% brightness. Sweep from 0 to 100 in steps to verify the full range.

## 2. Oscilloscope on PWM Output

Probing GPIO18 with a scope gives you the ground truth on duty cycle, frequency, and signal integrity.

### Probe Connection

```
                        Scope
                     ┌──────────┐
Pi GPIO18 ───────────┤ CH1 tip  │
(pin 12)             │          │
                     │          │
Pi GND ──────────────┤ CH1 GND  │
(pin 6)              │ (clip)   │
                     └──────────┘
```

- **CH1 probe tip** → GPIO18 (physical pin 12 on the Pi header)
- **CH1 ground clip** → any Pi GND pin (pin 6, 9, 14, 20, 25, 30, 34, or 39)

> **WARNING:** Connect the scope ground clip to **Pi GND only**, not to the 12V supply rail. The scope ground is earth-referenced through the mains plug — clipping it to 12V will short the supply through earth and may damage the adapter, the scope, or both.

### Scope Settings

| Setting | Value | Why |
| --- | --- | --- |
| Timebase | 10–20 µs/div | 25kHz = 40µs period; 2–4 full cycles on screen |
| Voltage | 1 V/div | 3.3V logic level; signal fills ~3 divisions |
| Trigger | Rising edge, CH1, ~1.6V | Clean trigger at mid-level |
| Coupling | DC | Preserves the DC offset; AC coupling would center the waveform |
| Probe attenuation | 1x or 10x | Match the probe switch to the scope setting |

### Expected Waveform

```
3.3V ┤ ┌──────┐          ┌──────┐          ┌──────┐
     │ │      │          │      │          │      │
     │ │      │          │      │          │      │
0.0V ┤─┘      └──────────┘      └──────────┘      └───
     │
     ├──── 40µs period (25kHz) ────┤
     │    ↑                        │
     │  duty cycle                 │
     │  (proportional to          │
     │   fan speed %)             │
```

- **Frequency:** 25.000 kHz ± 0.1%. pigpio uses hardware timers — this will be precise.
- **Amplitude:** 0V low, 3.3V high (Pi GPIO logic level). The 4-pin fan spec accepts 3.3V or 5V PWM.
- **Duty cycle:** matches `pct * 10000 / 1,000,000`. At 50% speed, the scope should read 50.0% duty.

### What to Look For

| Observation | Meaning |
| --- | --- |
| Clean square wave, correct frequency and duty | PWM hardware is working — problem is elsewhere |
| Correct frequency but wrong duty cycle | Check the duty calculation in `fan_controller.py` (0–1,000,000 scale, not 0–100) |
| No signal (flat 0V) | pigpiod not running, wrong GPIO pin, or `hardware_PWM` not called |
| Flat 3.3V (always high) | Duty set to 1,000,000 (100%) — check MQTT payload values |
| Ringing/overshoot on edges | Normal for long leads; keep probe wire short. Not a problem for the fan. |
| Noisy baseline | Check ground clip connection. Use the shortest ground path available. |

### Measuring Duty Cycle Sweep

Use `pigs` to sweep duty cycle while watching the scope:

```bash
for pct in 0 10 25 50 75 100; do
  duty=$((pct * 10000))
  echo "Setting ${pct}% (duty=${duty})"
  pigs hp 18 25000 $duty
  sleep 3
done
```

The scope's automatic duty cycle measurement should track within ±0.1% of the commanded value.

## 3. Tach Signal Verification

If using the optional tach output (GPIO24), the scope can verify the fan is generating pulses.

### Probe Connection

- **CH2 probe tip** → GPIO24 (physical pin 18)
- **CH2 ground clip** → same GND as CH1

### Expected Signal

- **Frequency:** varies with RPM. At 1000 RPM with 2 pulses/rev: 2000 pulses/60s ≈ 33 Hz.
- **Shape:** open-collector output pulled up to 3.3V by the Pi's internal pull-up. Signal swings from ~0V to ~3.3V.
- **If no pulses:** check the fan pin 3 wiring, verify the pull-up is enabled (`pi.set_pull_up_down(TACH_GPIO, pigpio.PUD_UP)`), and confirm the fan is actually spinning.

## 4. Host-Side MQTT Debug Client

`tools/mqtt_debug.py` is a workstation-side debug client that reads credentials from `pi/config.yaml` — no hardcoded IPs, no remembering `mosquitto_pub` flags.

### Prerequisites (workstation)

```bash
pip install paho-mqtt pyyaml
```

### Commands

```bash
# Monitor all rack/fan topics in real time (Ctrl+C to quit)
python tools/mqtt_debug.py monitor

# Set fan speed to 50%
python tools/mqtt_debug.py speed 50

# Sweep 0→100% in steps of 10, 3 seconds between steps
python tools/mqtt_debug.py sweep --min 0 --max 100 --step 10 --delay 3

# Sweep up then back down (useful with scope or LED)
python tools/mqtt_debug.py sweep --bounce

# Read the current retained speed value
python tools/mqtt_debug.py retained

# Clear a stale retained message
python tools/mqtt_debug.py clear
```

The `sweep --bounce` command paired with the LED simulator or a scope is the fastest way to verify the full MQTT → Pi → PWM pipeline end to end.

### Raw mosquitto Commands

If you prefer `mosquitto_pub`/`mosquitto_sub` directly (e.g., on a machine without Python):

```bash
# Subscribe to all project topics
mosquitto_sub -h YOUR_HA_IP -u YOUR_MQTT_USER -P YOUR_MQTT_PASS \
  -t 'rack/fan/#' -v

# Publish a test speed
mosquitto_pub -h YOUR_HA_IP -u YOUR_MQTT_USER -P YOUR_MQTT_PASS \
  -t rack/fan/speed -m 50

# Read the current retained value and exit
mosquitto_sub -h YOUR_HA_IP -u YOUR_MQTT_USER -P YOUR_MQTT_PASS \
  -t rack/fan/speed -C 1
```

If the fan starts at an unexpected speed on reboot, a stale retained message is the likely cause. Use `mqtt_debug.py clear` or publish an empty retained message to reset it.

## 5. pigpio Diagnostics

```bash
# Is pigpiod running?
systemctl status pigpiod

# Query GPIO18 mode and level
pigs mg 18        # mode: should return 1 (ALT5 for hardware PWM)
pigs r 18         # read level: 0 or 1 (instantaneous sample)

# Query current hardware PWM settings
pigs hp 18 0 0    # resets to off — use only if you want to stop the fan
```

## 6. Service Log Inspection

```bash
# Live tail
ssh pi@rack-fan 'journalctl -u fan-controller -f'

# Last 50 lines
ssh pi@rack-fan 'journalctl -u fan-controller -n 50'

# Since last boot
ssh pi@rack-fan 'journalctl -u fan-controller -b'
```

Look for:
- `MQTT connected, rc= 0` — successful broker connection
- `Fan speed: XX%` — speed commands being received and applied
- Python tracebacks — usually pigpiod not running or MQTT auth failure

## 7. Common Debug Scenarios

### "I changed the config but nothing happened"

The systemd service caches the running process. After editing `config.yaml`:

```bash
ssh pi@rack-fan 'sudo systemctl restart fan-controller'
```

### "The LED works but the fan doesn't respond"

The fan ignores the PWM signal. Almost always a missing ground bond:

1. Verify the jumper wire from Pi GND to 12V supply GND is connected
2. Check 12V supply is powered and delivering voltage (multimeter on fan pin 1/2)
3. Verify the PWM wire is on fan **pin 4** (blue), not pin 3 (tach)

### "The scope shows the right waveform but the fan runs full speed"

The fan's PWM input may be floating — check the connector is fully seated on all 4 pins. A partially inserted connector can make contact on pins 1/2 (power) but not pin 4 (PWM), causing the fan to default to 100%.
