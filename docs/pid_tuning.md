# PID Tuning Methodology

How the PID parameters in `pi/config.yaml` were derived, including a critical
sign convention issue for cooling applications and the full debugging story.

## 1. Data Sources

### Existing bang-bang thresholds

Inspected the live HA automations via the Samba mount (`/Volumes/config/automations.yaml`):

```bash
# Mount HA config (one-time)
# Finder: Cmd+K → smb://homeassistant.local/config
# Or: mount_smbfs //user:pass@homeassistant.local/config /Volumes/config
```

| Automation | Entity | Threshold | Action |
| --- | --- | --- | --- |
| Garage rack norm temp | sensor.vault_temperature | below 128°F | Turn off smart plug |
| Garage Rack Fan On High Temp | sensor.vault_temperature | above 132°F | Turn on smart plug |

### Vault temperature history

Extracted 7 days of `sensor.vault_temperature` from the HA SQLite database.
The database is locked while HA is running — use `immutable=1` to read it safely:

```bash
# Find the metadata_id for the sensor
sqlite3 "file:/Volumes/config/home-assistant_v2.db?mode=ro&immutable=1" \
  "SELECT metadata_id, entity_id FROM states_meta WHERE entity_id LIKE '%vault%temp%';"
# Result: metadata_id = 2481

# Export 7 days to CSV
sqlite3 -csv -header "file:/Volumes/config/home-assistant_v2.db?mode=ro&immutable=1" "
SELECT
  datetime(last_updated_ts, 'unixepoch', 'localtime') as timestamp,
  CAST(state AS REAL) as temp_f
FROM states
WHERE metadata_id = 2481
  AND state NOT IN ('unavailable', 'unknown', '')
  AND last_updated_ts > unixepoch('now', '-7 days')
ORDER BY last_updated_ts ASC;
" > tests/fixtures/vault_temp_history.csv
```

Results (514 data points, ~15-minute intervals):

| Statistic | Value |
| --- | --- |
| Min | 116.6°F |
| Max | 134.6°F |
| Mean | ~126°F |
| Typical range | 120–131°F |

### Key observations

- The vault spends most of its time in the 120–130°F range
- Temperature is reported in Fahrenheit by the Synology integration
- The sensor quantizes at ~1.8°F steps (1°C resolution: 47°C=116.6°F, 48°C=118.4°F, etc.)
- The Synology integration polls approximately every 15 minutes by default

## 2. Dual-Fan Strategy

The cabinet has two fan knockout positions. The new PWM fan occupies one; the old AC fan
remains in the other as a backup, controlled by the existing smart plug.

```text
°F:    105        115                    130       135
        │          │                      │         │
  OFF   │  idle    │    RAMP (new fan)    │  FULL   │  BACKUP
  (0%)  │  (~0%)   │    (0% → 100%)      │ (100%)  │  (old AC fan ON)
        │          │                      │         │
       LoLo       LO                     HI       HiHi
```

| Zone | Temperature | New PWM fan | Old AC fan |
| --- | --- | --- | --- |
| Cold | < 105°F (LoLo) | OFF | OFF |
| Cool | 105–115°F | Idle (~0%) | OFF |
| Normal | 115–130°F | Proportional ramp | OFF |
| Hot | 130°F+ (HI) | 100% | OFF |
| Emergency | 135°F+ (HiHi) | 100% | ON (bang-bang backup) |

## 3. Threshold Derivation

| Threshold | Value | Rationale |
| --- | --- | --- |
| LoLo | 105°F | Well below normal operating range; fan off saves power and noise |
| LO | 115°F | Below the typical vault floor (~117°F); PID setpoint — fan at idle |
| HI | 130°F | Near the top of the typical range; fan at 100% before history max |
| HiHi | 135°F | Above the observed max (134.6°F); old AC fan backup for extreme events |

## 4. PID Parameter Derivation

### The `simple-pid` library sign convention (critical)

The `simple-pid` library (used by the `simple_pid_controller` HA integration) computes:

```text
error = setpoint - measured
output = Kp * error + Ki * integral(error) + Kd * derivative(error)
```

This means:

- **Heating application** (thermostat): temp below setpoint → positive error → positive output (turn heater on). **Use positive gains.**
- **Cooling application** (our fan): temp above setpoint → negative error → with positive gains, output goes negative → clamped to 0. **The fan never turns on.**

**The fix: use negative gains for cooling.** This inverts the relationship:
- temp above setpoint → negative error × negative Kp → positive output → fan on

This was confirmed experimentally with the actual library:

```python
from simple_pid import PID

# WRONG — positive gains for cooling
pid = PID(Kp=6.7, Ki=0.02, Kd=0.5, setpoint=115, output_limits=(0, 100))
print(pid(125.0))  # Output: 0.0  ← fan OFF when it should be ON

# CORRECT — negative gains for cooling
pid = PID(Kp=-6.7, Ki=-0.02, Kd=-0.5, setpoint=115, output_limits=(0, 100))
print(pid(125.0))  # Output: 67.0  ← fan at 67%, correct
```

This is documented in the [simple-pid README](https://simple-pid.readthedocs.io) under
"Reverse-acting PID", but is easily missed. The `simple_pid_controller` HA integration
does not have a "cooling mode" toggle — you must negate the gains manually.

### Setpoint = 115°F (LO)

The PID holds temperature at the bottom of the operating range. Above LO, the fan ramps up.
Below LO, the PID outputs 0 (and below LoLo, the cutoff forces the fan OFF).

### Kp = -6.7

Maps the LO–HI range to 0–100% output (negated for cooling):

```text
|Kp| = output_range / error_range = 100 / (HI - LO) = 100 / 15 ≈ 6.7
Kp = -6.7 (negative for cooling)
```

At HI (130°F): error = 115-130 = -15, P = -6.7 × -15 = 100%.
At midpoint (122.5°F): error = -7.5, P = -6.7 × -7.5 ≈ 50%.
At LO (115°F): error = 0, P = 0%.

### Ki = -0.02

Very gentle integral (negated for cooling). The wider 15°F band means Ki contributes
less aggressively than in a narrow band:

- At a sustained 5°F above setpoint: integral accumulates at 0.02 × 5 × 30 = 3% per sample
- After 10 minutes (20 samples): I-term contribution ≈ 60% — enough for steady-state correction
- Anti-windup: the `simple_pid_controller` integration clamps the integral to output bounds
- Windup protection is enabled by default (switch entity `switch.rack_fan_pid_windup_protection`)

### Kd = -0.5

Light derivative damping (negated for cooling). Temperature changes slowly in a
cabinet (~1°F per 15 minutes under load). Kd primarily prevents overshoot on
rapid transitions (e.g., NAS starts a rebuild). Too high → noise amplification
from the sensor's coarse 1°C quantization.

### output_min = 0, output_max = 100

PID can output 0. The LoLo cutoff handles the "fan off" state independently.

## 5. LoLo Cutoff

The LoLo cutoff is applied **after** the PID output, in the HA bridge automation
(`automation_pid_to_mqtt.yaml`). When `sensor.vault_temperature` is below 105°F,
the automation publishes speed=0 to MQTT regardless of the PID output:

```yaml
actions:
- choose:
  - conditions:
    - condition: numeric_state
      entity_id: sensor.vault_temperature
      below: 105
    sequence:
    - action: mqtt.publish
      data:
        topic: rack/fan/speed
        retain: true
        payload: '0'
  default:
  - action: mqtt.publish
    data:
      topic: rack/fan/speed
      retain: true
      payload: "{{ states('input_number.rack_fan_speed') | int }}"
```

## 6. HA Automation Reliability

### The problem

HA automations triggered by `state` only fire on `last_changed` — when the sensor
reports a genuinely new value. If the Synology reports the same temperature twice
(e.g., 120.2°F → 120.2°F), `last_reported` updates but `last_changed` doesn't,
and the automation doesn't fire. Combined with the ~15-minute default Synology
poll interval and 1°C quantization, this means the PID can go minutes without
a fresh calculation.

### Remediations deployed

Three automations address this:

1. **Force sensor update every 5 minutes** (`automation_force_sensor_update.yaml`):
   Calls `homeassistant.update_entity` on a `time_pattern` trigger to reduce the
   effective poll interval from ~15 minutes to 5 minutes.

2. **Time-pattern fallback on PID→helper bridge** (`automation_pid_output_to_helper.yaml`):
   Fires on both `state` change AND `time_pattern: /30s`. Ensures the input_number
   helper gets the latest PID output even when the PID sensor's state hasn't technically
   "changed".

3. **Time-pattern fallback on helper→MQTT bridge** (`automation_pid_to_mqtt.yaml`):
   Same dual-trigger pattern. Ensures MQTT gets a fresh speed command every 30 seconds
   regardless of whether the helper value changed.

### Verifying the automations are firing

```bash
# Check automation status via the REST API
TOKEN="your_long_lived_token"
curl -s -H "Authorization: Bearer $TOKEN" \
  http://homeassistant.local:8123/api/states/automation.rack_fan_pid_to_mqtt \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'state: {d[\"state\"]}, last_triggered: {d[\"attributes\"][\"last_triggered\"]}')"
```

## 7. Simulation Validation

The PID simulation tests (`tests/test_pid_simulation.py`) use the **actual `simple-pid` library**
(same version as the HA integration) with parameters loaded from `pi/config.yaml`:

```bash
conda activate quiet-rack
pytest tests/test_pid_simulation.py -v
```

### Test categories (19 tests)

| Category | What's verified |
| --- | --- |
| **Config validation** | Threshold ordering, gains are negative, LoLo below LO |
| **History replay** | All 514 data points produce valid output within bounds |
| **Threshold behavior** | Correct output at HI (100%), LO (0%), LoLo (OFF), midpoint (~50%) |
| **Cooling convention** | Hot → positive output, cold → 0, proportional to error |
| **Duty cycle** | Values in pigpio range (0–1,000,000), correct multiples |

### Running the tach simulator for dashboard testing

Before the fan is physically wired, the tach simulator generates realistic RPM values
on the HA dashboard based on live PID commands:

```bash
# Start the simulator (runs until killed)
nohup python -u /tmp/tach_sim_live.py > /tmp/tach_sim_live.log 2>&1 &

# Monitor output
tail -f /tmp/tach_sim_live.log

# Kill when the real fan is wired
kill $(cat /tmp/tach_sim_live.pid)
```

The simulator models the Arctic P12 PWM fan:

- Max RPM: 1800 at 100% duty
- Min RPM: 300 at stall threshold (12%)
- Spin-up time constant: 2 seconds
- Spin-down time constant: 3 seconds

## 8. HA PID Controller Configuration

### Installation

The `simple_pid_controller` integration was installed by downloading from GitHub and
copying directly to HA's `custom_components/` directory via SSH:

```bash
# Download on the HA host
ssh hassio@homeassistant.local 'cd /tmp && \
  curl -sL https://github.com/bvweerd/simple_pid_controller/archive/refs/heads/main.tar.gz | tar xz'

# Copy to custom_components (requires sudo — owned by root)
ssh hassio@homeassistant.local 'sudo cp -r \
  /tmp/simple_pid_controller-main/custom_components/simple_pid_controller \
  /config/custom_components/'

# Restart HA to load
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://homeassistant.local:8123/api/services/homeassistant/restart
```

### Configuration via REST API

The integration uses a config flow. Create the entry via the API:

```bash
# Step 1: Start the config flow
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://homeassistant.local:8123/api/config/config_entries/flow \
  -d '{"handler":"simple_pid_controller"}' | python3 -m json.tool
# Note the flow_id from the response

# Step 2: Submit the form
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "http://homeassistant.local:8123/api/config/config_entries/flow/FLOW_ID" \
  -d '{
    "name": "Rack Fan PID",
    "sensor_entity_id": "sensor.vault_temperature",
    "input_range_min": 105.0,
    "input_range_max": 140.0,
    "output_range_min": 0.0,
    "output_range_max": 100.0
  }'
```

### Setting the PID gains

After the entry is created, set gains via the number entities:

```bash
for entity_value in \
  "number.rack_fan_pid_setpoint:115.0" \
  "number.rack_fan_pid_kp:-6.7" \
  "number.rack_fan_pid_ki:-0.02" \
  "number.rack_fan_pid_kd:-0.5" \
  "number.rack_fan_pid_sample_time:30" \
  "number.rack_fan_pid_output_min:0" \
  "number.rack_fan_pid_output_max:100"; do
  entity=$(echo $entity_value | cut -d: -f1)
  value=$(echo $entity_value | cut -d: -f2)
  curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    http://homeassistant.local:8123/api/services/number/set_value \
    -d "{\"entity_id\":\"${entity}\",\"value\":${value}}"
done
```

### Resetting the PID

If the integral term needs clearing (e.g., after changing gains):

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://homeassistant.local:8123/api/services/simple_pid_controller/set_output \
  -d '{"entity_id":"sensor.rack_fan_pid_pid_output","preset":"zero_start"}'
```

### Monitoring the PID

```bash
# Live pipeline monitor (workstation)
# Shows temp, PID output, helper, MQTT speed, zone classification
python -u /tmp/rack_fan_monitor.py

# Check individual entities
curl -s -H "Authorization: Bearer $TOKEN" \
  http://homeassistant.local:8123/api/states/sensor.rack_fan_pid_pid_output \
  | python3 -m json.tool

# MQTT messages
python tools/mqtt_debug.py monitor
```

## 9. Debugging Timeline

This section records the actual debugging sequence for future reference.

### Initial symptom: PID output stuck at 0%

After deploying the PID controller with Kp=6.7, Ki=0.02, Kd=0.5, the output
worked briefly (saw 11.8%, 23.6%, 44.24%) then went to 0.0 and stayed there,
even with vault temperature at 125–130°F (well above the 115°F setpoint).

### Wrong diagnosis: DataUpdateCoordinator stall

Initial investigation focused on the HA `DataUpdateCoordinator` backing off after
`UpdateFailed` exceptions — a known HA-wide issue. This led to adding time-pattern
fallback triggers on the bridge automations (which are useful regardless) and
investigating the `simple_pid_controller` GitHub issues. No matching reports found.

### Root cause: PID sign convention for cooling

The `simple-pid` library computes `error = setpoint - measured`. For a cooling
application where temp (125°F) > setpoint (115°F):

```text
error = 115 - 125 = -10
P = 6.7 × (-10) = -67
output = -67, clamped to 0  ← fan OFF when it should be at 67%
```

With positive gains, the PID thinks it's a heating controller — it outputs power
when the system is COLD (below setpoint), not when it's HOT.

### Why the initial readings looked correct

The early readings (11.8%, 44.24%) were taken when the temperature happened to be
BELOW the setpoint:

- At 109.4°F: error = 115 - 109.4 = +5.6, P = 6.7 × 5.6 = +37.5 → output ~44%

This was the PID "heating" — running the fan when it was cool and stopping it when
hot. Exactly backwards.

### Fix

Negate all three gains:

```text
Kp = -6.7  (was 6.7)
Ki = -0.02 (was 0.02)
Kd = -0.5  (was 0.5)
```

After this change: temp 118.4°F → PID output 24.8% → MQTT speed 25 → tach sim 519 RPM.
Correct behavior confirmed on the HA dashboard.

## 10. Future Tuning

After installation, observe the HA dashboard for 24–48 hours:

1. If the fan oscillates near LO → reduce |Kp| (try -5.0)
2. If temperature creeps above 130°F without fan catching up → increase |Ki| (try -0.03)
3. If fan overreacts to brief NAS spikes → reduce |Kd| (try -0.2 or 0)
4. If fan cycles on/off near LoLo → lower LoLo (try 100°F) or add hysteresis
5. Record final values back into `pi/config.yaml`

All gains can be adjusted live in the HA UI (Settings → Devices → Rack Fan PID → number entities).
No restarts needed — the PID recalculates on the next sample cycle.
