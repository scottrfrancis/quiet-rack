# PID Tuning: From Theory to Practice

A detailed account of tuning a PID-controlled rack fan, starting from textbook
theory, running headfirst into three real-world problems, and arriving at a
proportional-only controller that works better than the "correct" PID.

This is the full engineering story — every wrong turn included — intended for
a project blog post. All data is real, all mistakes are ours.

## The System

A 12U wall-mount network cabinet in a garage. Inside: a Synology NAS that
generates heat continuously. The old cooling: a 120V AC muffin fan on a smart
plug, driven by two Home Assistant automations — on above 132°F, off below
128°F. Classic bang-bang control.

The new system: an Arctic P12 PWM fan, a Raspberry Pi Zero W as an MQTT-to-PWM
bridge, and a PID controller running in Home Assistant. The goal: quiet,
proportional cooling instead of on/off cycling.

```text
sensor.vault_temperature → PID controller → MQTT → Pi → PWM → Arctic P12 fan
```

## Chapter 1: Textbook PID Design

### The naive approach

PID theory says: pick a setpoint, tune Kp for response speed, add Ki to
eliminate steady-state offset, add Kd to dampen oscillation. For a thermal
system with a long time constant, keep Ki low and Kd minimal. Standard advice.

We started with:

```yaml
setpoint: 128.0   # °F — matched the old bang-bang OFF threshold
kp: 25.0          # 100% / (132 - 128) = 25 per °F
ki: 0.1            # gentle integral
kd: 0.5            # light derivative
```

The 4°F band (128–132°F) matched the old bang-bang range. Kp was computed to
give 100% output at the old ON threshold. This looked right on paper.

### Problem 1: The sign convention (hours of debugging)

The PID controller reported its first output: **11.8%**. Encouraging. Then
it climbed to 23.6%, then 44.24%. Then it settled at **0.0%** and stayed there.
For hours. While the vault sat at 127°F, 12°F above what we thought was the
setpoint.

We chased the wrong cause first. Investigated whether the Home Assistant
`DataUpdateCoordinator` was backing off exponentially (a known HA issue).
Added time-pattern fallback triggers to the automations. Checked HA error logs.
Toggled the PID auto_mode. Reset the PID. Nothing helped.

The breakthrough came from reading the `simple-pid` library source code. The
library computes:

```text
error = setpoint - measured
output = Kp × error + Ki × ∫error + Kd × d(error)/dt
```

Note the sign: `error = setpoint - measured`, **not** `measured - setpoint`.

For a **heating** system (thermostat), this is intuitive:

- Room is cold (measured < setpoint) → error is positive → output is positive → heater on

For a **cooling** system (our fan), it's inverted:

- Cabinet is hot (measured > setpoint) → error is **negative** → output is **negative** → clamped to 0 → fan OFF

**The fan was OFF precisely when the cabinet was hottest.** And the early
"encouraging" readings (11.8%, 44.24%) were from moments when the temperature
happened to dip *below* setpoint — the PID was happily "heating" a cold cabinet.

#### The fix

Negate all gains. Negative × negative = positive:

```text
temp = 127°F, setpoint = 128°F
error = 128 - 127 = +1 (positive — "cold")
Kp = -25: output = -25 × 1 = -25 → clamped to 0 → fan OFF ✓

temp = 132°F, setpoint = 128°F
error = 128 - 132 = -4 (negative — "hot")
Kp = -25: output = -25 × -4 = +100 → fan at 100% ✓
```

We verified this with the actual library:

```python
from simple_pid import PID

# WRONG — positive gains for cooling
pid = PID(Kp=6.7, Ki=0.02, Kd=0.5, setpoint=115, output_limits=(0, 100))
print(pid(125.0))  # 0.0 ← fan OFF when it should be ON

# CORRECT — negative gains for cooling
pid = PID(Kp=-6.7, Ki=-0.02, Kd=-0.5, setpoint=115, output_limits=(0, 100))
print(pid(125.0))  # 67.0 ← correct
```

**Lesson:** The `simple_pid_controller` HA integration does not have a "cooling
mode" toggle. If you're controlling a cooling system, you must negate the gains.
This is documented in the [simple-pid README](https://simple-pid.readthedocs.io)
as "reverse-acting PID" but is easy to miss.

## Chapter 2: Choosing the Setpoint from Real Data

### Data source: InfluxDB

Home Assistant exports all sensor data to an InfluxDB v2 instance. We queried
6 months of `sensor.vault_temperature` data (11,185 readings, Sep 2025 –
Mar 2026) using the Flux query language:

```flux
from(bucket: "homeassistant")
  |> range(start: -365d)
  |> filter(fn: (r) => r.entity_id == "vault_temperature" and r._field == "value")
  |> keep(columns: ["_time", "_value"])
```

We also pulled `sensor.outside_temperature` (WeatherUnderground weather station)
to correlate vault temperature with ambient conditions.

### First attempt: setpoint = 115°F (too low)

Our first instinct was to set the setpoint well below the typical operating
range. The idea: the PID would ramp up gradually as temperature climbed.

```yaml
setpoint: 115.0
kp: -6.7     # 100% / 15°F range
ki: -0.02
kd: -0.5
```

We ran this configuration overnight (15 hours, 1,151 samples). The result:

```text
PID output: 100% for 89% of the night
Mean output: 87.3%
Fan at 0%: ~0% of the time
```

The fan ran at full speed almost continuously — exactly the bang-bang behavior
we were trying to eliminate. The setpoint was so far below the vault's natural
resting temperature that the PID could never bring it down, and the integral
term accumulated relentlessly.

### Temperature distribution analysis

We computed weekly quantiles across the full 6-month dataset:

```text
                   Vault Temperature Distribution (6 months, 11,185 readings)

    106-108°F:   10 ( 0.9%)
    112-114°F:   10 ( 0.9%)
    116-118°F:   63 ( 5.5%) █████
    118-120°F:   22 ( 1.9%) █
    120-122°F:   74 ( 6.4%) ██████
    122-124°F:  298 (25.8%) █████████████████████████
    124-126°F:  118 (10.2%) ██████████
    126-128°F:  177 (15.4%) ███████████████
    128-130°F:  110 ( 9.5%) █████████
    130-132°F:  261 (22.6%) ██████████████████████
    132-134°F:   10 ( 0.9%)
```

Two clear clusters: **122–124°F** (NAS idle) and **130–132°F** (NAS under load).
Median: 125.6°F. The old bang-bang thresholds (128/132°F) were in the upper cluster.

### Monthly breakdown reveals seasonality

```text
Month     |  P25   |  P50   |  P75   | Notes
----------+--------+--------+--------+---------------------
2025-09   | 120.2  | 123.8  | 127.4  | Summer (SLO) — hot
2025-10   | 116.6  | 120.2  | 123.8  | Fall transition
2025-11   | 111.2  | 116.6  | 120.2  | Winter — cool
2025-12   | 111.2  | 116.6  | 120.2  | Winter
2026-01   | 109.4  | 113.0  | 116.6  | Coldest month
2026-02   | 109.4  | 113.0  | 118.4  | Late winter
2026-03   | 120.2  | 123.8  | 127.4  | Spring warming
```

Seasonal spread: **10.8°F** between the coldest month (Jan, median 113°F) and
the hottest (Sep, median 124°F). The garage tracks outdoor temperature.

### Correlation with outdoor temperature

We matched 628 six-hour windows of vault temperature against the outdoor
weather station:

```text
Outside temp | Vault median | Fan behavior
-------------+--------------+-------------
    < 50°F   |     109.5°F  | OFF (below LoLo)
   50–60°F   |     114.8°F  | OFF (below setpoint)
   60–70°F   |     119.6°F  | OFF (near setpoint)
   70–80°F   |     121.3°F  | OFF (at setpoint)
   80–90°F   |     124.9°F  | ~37% speed
     90+°F   |     129.5°F  | ~93% speed
```

**Pearson r = 0.656** — strong correlation. Each 1°F warmer outside adds
~0.4°F to the vault. A 20°F hotter day raises vault temperature by ~8°F.

### Second attempt: setpoint = 122°F (the right answer)

The 6-month median was 118.3°F, but the idle cluster centered at 122–124°F.
We chose 122°F as the setpoint — the bottom of the idle cluster:

- Below 122°F: the vault is naturally cool, fan off
- 122–130°F: fan ramps proportionally through the normal operating range
- Above 130°F: fan at 100%, and the backup AC fan kicks in at 135°F

```yaml
setpoint: 122.0
hi: 130.0
lo: 122.0
lolo: 110.0    # fan OFF — well below normal range
hihi: 135.0    # backup AC fan
kp: -12.5      # 100% / (130 - 122) = 12.5 per °F
ki: -0.005
kd: -0.3
```

The 8°F ramp band (122–130°F) was chosen to span from the idle cluster floor
to just below the load cluster ceiling. Kp = -12.5 gives a clean linear ramp:

```text
Predicted response (P-term only):
  122°F →   0%
  124°F →  25%
  126°F →  50%
  128°F →  75%
  130°F → 100%
```

Replaying the overnight data against the new parameters showed a dramatic
improvement:

```text
                     Old (115°F)     New (122°F)
Mean output:           87.3%           51.5%
At 100%:               89%             26%
At 0% (fan off):       ~0%             25%
In ramp (proportional): 74%            61%
```

## Chapter 3: The Integral Term Problem

### What Ki is supposed to do

In textbook PID, the integral term eliminates steady-state offset. If the
proportional term alone can't bring the process variable to the setpoint,
the integral slowly accumulates error and increases the output until it does.

### Why Ki fails in this system

The vault temperature is **always** above the setpoint during warm weather.
The NAS generates heat continuously, and the fan (even at 100%) cannot cool
the cabinet to 122°F. There is a permanent positive error.

With Ki = -0.005:

- Every 30-second sample at +2°F error adds to the integral
- Over hours, the integral saturates at the output maximum
- The output pins at 100% regardless of the proportional term
- When temperature drops toward setpoint, the integral doesn't unwind because
  the temperature never goes *below* setpoint long enough

We observed this three separate times:

**Iteration 1 (Ki = -0.02, setpoint = 115°F):**
Overnight log showed PID at 100% for 89% of the night. Even at 116.6°F
(only 1.6°F above setpoint), the output was 100% due to integral saturation.

**Iteration 2 (Ki = -0.005, setpoint = 122°F):**
After retuning, the PID initially behaved well. At 10:12, vault at 122.0°F
(exactly setpoint), PID output was **76.4%** — should have been ~0%. Pure
integral accumulation. By 10:15, when temp bumped to 125.6°F, it hit 100%
and stayed there even as temp dropped back to 123.8°F.

```text
Monitor log (condensed):
  10:12  122.0°F  PID=76.4%   ← should be ~0%
  10:15  125.6°F  PID=100.0%  ← saturated, never comes back
  10:20  123.8°F  PID=100.0%  ← stuck at 100% despite cooling
  10:37  123.8°F  PID=100.0%  ← 20 minutes later, still stuck
```

**Iteration 3 (Ki = 0):**
Immediately correct. At 123.8°F: PID output = 12.5 × 1.8 = **22.5%**. Exactly
what the P-term predicts. No lag, no accumulation, no saturation.

### The right answer: Ki = 0 (proportional + derivative only)

```yaml
kp: -12.5
ki: 0          # disabled
kd: -0.3
```

This is a PD controller, not a PID controller. And it's the correct choice
for this system because:

1. **The fan cannot reach the setpoint.** Integral control assumes the
   actuator has enough authority to drive the process variable to the
   setpoint. Our fan cannot cool the cabinet to 122°F — the NAS generates
   too much heat and the garage ambient is too warm. The integral term's
   job is impossible.

2. **Steady-state offset is acceptable.** If the vault sits at 124°F, the
   fan runs at 25%. If it sits at 126°F, the fan runs at 50%. This is
   exactly the behavior we want — proportional response. There's no
   "correct" temperature we're trying to achieve; we're trying to provide
   proportional cooling.

3. **Immediate response matters more.** When the NAS starts a heavy job and
   temperature spikes from 124°F to 130°F, we want the fan to jump from
   25% to 100% immediately. An integral term adds lag because it takes time
   to accumulate. The P-term responds instantly.

4. **The derivative term is the right companion.** Kd = -0.3 provides light
   damping on temperature spikes, which smooths out the response to the
   sensor's 1°C quantization steps (1.8°F jumps). It doesn't add lag like
   the integral.

## Chapter 4: The Zone Architecture

The final controller design uses five zones instead of the old two-state
bang-bang:

```text
°F:    110        122                    130       135
        │          │                      │         │
  OFF   │  idle    │    RAMP (new fan)    │  FULL   │  BACKUP
  (0%)  │  (~0%)   │    (0% → 100%)      │ (100%)  │  (old AC fan ON)
        │          │                      │         │
       LoLo       LO/setpoint           HI       HiHi
```

### LoLo (110°F) — Fan OFF

Below this temperature, the fan is completely off. The LoLo cutoff is
implemented in the HA bridge automation, not in the PID controller. When
`sensor.vault_temperature` is below 110°F, the automation publishes speed=0
to MQTT regardless of the PID output.

The threshold was chosen at 110°F because:

- The vault drops below 110°F during winter nights (P5 = 104–109°F in Dec–Feb)
- No active cooling is needed at these temperatures
- The 12°F gap between LoLo and setpoint prevents on/off cycling

### LO / Setpoint (122°F) — Fan at idle

The PID setpoint. At this temperature, the P-term is zero and the fan
produces no output. In practice, the vault crosses 122°F on its way up
during morning NAS activity and on its way down during evening idle.

### HI (130°F) — Fan at 100%

At HI, the P-term reaches 100%: |Kp| × (130 - 122) = 12.5 × 8 = 100.
Above HI, the output is clamped at 100%. The new fan is running as hard
as it can.

### HiHi (135°F) — Backup AC fan

Above 135°F, the old AC muffin fan kicks in via the original smart plug
automations (re-thresholded from the old 128/132 to 130/135). This is a
thermal safety net — two fans running simultaneously for extreme events.

The 135°F threshold was chosen because:

- The 6-month maximum was 141.8°F (March 2026, likely a NAS rebuild)
- The September maximum was 138.2°F
- 5°F above HI gives the new fan a chance to handle the load before the
  backup fires

## Chapter 5: HA Automation Reliability

### The problem

Home Assistant automations triggered by `state` only fire on `last_changed` —
when the entity reports a genuinely new value. If the Synology reports the
same temperature twice (e.g., 120.2°F → 120.2°F), `last_reported` updates
but `last_changed` doesn't, and the automation doesn't fire.

The Synology integration polls approximately every 15 minutes by default.
The temperature sensor has 1°C resolution (1.8°F quantization steps). These
combine to create long gaps where the PID receives no input and the MQTT
output goes stale.

### Remediations

Three automations address this:

1. **Force sensor update every 5 minutes** (`automation_force_sensor_update.yaml`):
   Calls `homeassistant.update_entity` on `sensor.vault_temperature` via a
   `time_pattern` trigger. Reduces the effective poll gap from ~15 minutes to
   5 minutes. The sensor may still report the same value, but at least the
   PID gets a chance to recalculate.

2. **Time-pattern fallback on PID→helper bridge** (`automation_pid_output_to_helper.yaml`):
   Triggers on both `state` change AND `time_pattern: /30s`. Ensures
   `input_number.rack_fan_speed` reflects the latest PID output even when the
   PID sensor's state hasn't technically "changed" (because the rounded integer
   is the same).

3. **Time-pattern fallback on helper→MQTT bridge** (`automation_pid_to_mqtt.yaml`):
   Same dual-trigger pattern. Ensures a fresh MQTT speed command reaches the
   Pi every 30 seconds regardless of whether the helper value changed.

These three automations form a reliability chain:

```text
sensor update (5 min) → PID recalculates → helper updated (30s) → MQTT published (30s)
```

Without them, the system worked when temperature was actively changing but
went stale for minutes at a time when the vault was thermally stable.

## Chapter 6: Monitoring and Validation

### Live pipeline monitor

We built a monitoring script that polls HA, subscribes to MQTT, and logs
everything to CSV:

```bash
# Columns: timestamp, temp, pid_output, helper, mqtt_speed, mqtt_rpm,
#          mqtt_pub_count, zone, temp_changed, pid_changed
tail -f /tmp/rack_fan_tuned_console.log
```

Example output from the tuned system:

```text
    Time |    Temp |    PID |  Hlpr |  MQTT |   Zone | Pubs
-----------------------------------------------------------
08:23:15 |   123.8 |  27.09 |  27.0 |    27 |   ramp | +2
08:23:45 |   123.8 |  27.36 |  27.0 |    27 |   ramp | +1
...
10:12:23 |   122.0 |   0.00 |   0.0 |     0 |   ramp |     ← at setpoint, fan off ✓
...
10:38:02 |   123.8 |  22.50 |  22.0 |    22 |   ramp | +1  ← P-only, no windup ✓
```

### Tach simulator

Before the fan is physically wired, a tach simulator runs on the workstation.
It subscribes to the MQTT speed topic, models the Arctic P12's physical
characteristics (spin-up/spin-down inertia, stall threshold, RPM curve), and
publishes simulated RPM to `rack/fan/rpm`. This feeds the HA dashboard so you
can watch the fan "respond" to PID commands in real time.

```text
Fan model: Arctic P12 PWM
  Max RPM: 1800 at 100% duty
  Min RPM: 300 at stall threshold (12% duty)
  Spin-up tau: 2 seconds
  Spin-down tau: 3 seconds
  Stall below: 12% duty → 0 RPM
```

### InfluxDB queries used

All queries target the `homeassistant` bucket on InfluxDB v2 at `192.168.4.6:8086`.

**Weekly quantiles (for seasonality analysis):**

```flux
from(bucket: "homeassistant")
  |> range(start: -365d)
  |> filter(fn: (r) => r.entity_id == "vault_temperature" and r._field == "value")
  |> keep(columns: ["_time", "_value"])
```

Post-processed in Python to compute P5/P10/P25/P50/P75/P90/P95 per ISO week,
plus zone distribution (OFF/idle/ramp/FULL/emergency) against config thresholds.

**Outside temperature correlation:**

```flux
// Vault temperature, 6-hour averages
from(bucket: "homeassistant")
  |> range(start: -180d)
  |> filter(fn: (r) => r.entity_id == "vault_temperature" and r._field == "value")
  |> aggregateWindow(every: 6h, fn: mean)

// Outside temperature, same windows
from(bucket: "homeassistant")
  |> range(start: -180d)
  |> filter(fn: (r) => r.entity_id == "outside_temperature" and r._field == "value")
  |> aggregateWindow(every: 6h, fn: mean)
```

Matched on overlapping 6-hour windows, binned by outside temperature range,
Pearson correlation computed in Python.

**Overnight data (for tuning validation):**

Real-time CSV collected by a Python monitor script polling the HA REST API
every 30 seconds and subscribing to `rack/fan/#` via MQTT. Stored at
`/tmp/rack_fan_overnight_*.csv`.

## Chapter 7: Final Configuration

```yaml
pid:
  setpoint: 122.0     # vault idle cluster median (from 6-month InfluxDB data)
  hi: 130.0            # NAS load cluster — fan at 100%
  lo: 122.0            # setpoint = LO
  lolo: 110.0          # fan OFF below this
  hihi: 135.0          # backup AC fan threshold
  kp: -12.5            # negative for cooling; 100% / 8°F range
  ki: 0                # disabled — integral winds up in this system
  kd: -0.3             # light derivative damping
  output_min: 0
  output_max: 100
  sample_time: 30
```

### Tuning iterations summary

| Iteration | Setpoint | Kp | Ki | Result | Problem |
| --- | --- | --- | --- | --- | --- |
| 1 | 128°F | +25 | +0.1 | Output always 0% | Wrong sign — positive gains for cooling |
| 2 | 115°F | -6.7 | -0.02 | Output 100% for 89% of night | Setpoint too low, integral windup |
| 3 | 122°F | -12.5 | -0.005 | Output 76% at setpoint | Integral still winding up |
| **4** | **122°F** | **-12.5** | **0** | **22.5% at 123.8°F** | **None — proportional + derivative works** |

### Validation against 6-month data

Replaying all 11,185 readings through the final configuration:

```text
Zone distribution:
  OFF  (< 110°F):   9%  — winter nights
  idle (110–122°F): 32%  — winter days, cool nights
  ramp (122–130°F): 43%  — proportional cooling (the sweet spot)
  FULL (130–135°F): 14%  — NAS under load
  EMRG (> 135°F):   2%  — backup AC fan needed
```

The fan spends 43% of its time in the proportional ramp band — doing exactly
what we designed it to do: running quietly at partial speed instead of
cycling on and off.

## Lessons Learned

1. **Read the library's sign convention before you tune.** The `simple-pid`
   library uses `error = setpoint - measured`. For cooling, negate the gains.
   For heating, use positive gains. This is the single most important thing
   to get right, and it's easy to get wrong.

2. **Plot your actual temperature distribution before picking a setpoint.**
   Don't guess from the bang-bang thresholds. Pull 30 days of history from
   InfluxDB and look at where the process actually sits. Our vault idles at
   122–124°F; setting the setpoint at 115°F (7°F below the idle floor) guaranteed
   the fan would run at 100% all the time.

3. **If the process can't reach the setpoint, disable the integral term.**
   Ki assumes the actuator has enough authority to drive the process variable
   to the setpoint and hold it there. If it can't (because ambient heat
   generation exceeds cooling capacity), the integral only accumulates and
   never unwinds. PD control is the correct choice for systems where
   proportional response is the goal, not setpoint tracking.

4. **Correlation with external variables reveals whether your controller can
   even work.** The 0.656 Pearson r between outdoor and vault temperature tells
   us the vault is significantly affected by ambient conditions. On a 90°F day,
   the vault median is 130°F — at the top of our ramp band. The fan is doing
   all it can. A bigger fan or better insulation would help more than better
   PID tuning.

5. **Add time-pattern fallback triggers to every HA automation in the control
   chain.** HA's `state` trigger only fires on `last_changed`. With slow-polling
   sensors and quantized values, the state can go minutes without "changing"
   even though the PID needs to recalculate. A 30-second time-pattern trigger
   as a second trigger on each automation guarantees freshness.

6. **Monitor the live system, not just simulations.** Our PID simulation tests
   (using the actual `simple-pid` library against recorded data) passed on every
   iteration. But the real system revealed integral windup that only manifests
   over hours of continuous operation. The overnight CSV log was what caught it.
