# PID Tuning Methodology

How the PID parameters in `pi/config.yaml` were derived.

## 1. Data Sources

### Existing bang-bang thresholds

Inspected the live HA automations at `/Volumes/config/automations.yaml` (HA config mounted via Samba):

| Automation | Entity | Threshold | Action |
| --- | --- | --- | --- |
| Garage rack norm temp | sensor.vault_temperature | below 128°F | Turn off smart plug |
| Garage Rack Fan On High Temp | sensor.vault_temperature | above 132°F | Turn on smart plug |

### Vault temperature history

Extracted 7 days of `sensor.vault_temperature` from the HA SQLite database:

```bash
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

### Key observation

The vault spends most of its time in the 120–130°F range. The old bang-bang system cycled the fan on/off across a 4°F band. The new PID system spreads the fan speed across a wider 15°F band for smoother, quieter operation.

## 2. Dual-Fan Strategy

The cabinet has two fan knockout positions. The new PWM fan occupies one; the old AC fan remains in the other as a backup, controlled by the existing smart plug.

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

The old bang-bang automations stay in place but with adjusted thresholds (HiHi/Hi) so the AC fan only kicks in if the PWM fan can't keep up — a thermal safety net.

## 3. Threshold Derivation

| Threshold | Value | Rationale |
| --- | --- | --- |
| LoLo | 105°F | Well below normal operating range; fan off saves power and noise |
| LO | 115°F | Below the typical vault floor (~117°F); PID setpoint — fan at idle |
| HI | 130°F | Near the top of the typical range; fan at 100% before history max |
| HiHi | 135°F | Above the observed max (134.6°F); old AC fan backup for extreme events |

## 4. PID Parameter Derivation

### Setpoint = 115°F (LO)

The PID holds temperature at the bottom of the operating range. Above LO, the fan ramps up. Below LO, the PID outputs 0 (and below LoLo, the cutoff forces the fan OFF).

### Kp = 6.7

Maps the LO–HI range to 0–100% output:

```text
Kp = output_range / error_range = 100 / (HI - LO) = 100 / 15 ≈ 6.7
```

At HI (130°F): error = 15°F, P-term = 6.7 × 15 = 100%.
At midpoint (122.5°F): error = 7.5°F, P-term = 6.7 × 7.5 ≈ 50%.
At LO (115°F): error = 0, P-term = 0%.

### Ki = 0.02

Very gentle integral. The wider 15°F band means Ki contributes less aggressively than in a narrow band:

- At a sustained 5°F error: 0.02 × 5 × 30 = 3% per sample
- After 10 minutes (20 samples): I-term ≈ 60% — enough for steady-state correction
- Anti-windup clamps the integral to output bounds

### Kd = 0.5

Light derivative damping. Temperature changes slowly (~1°F per 15 minutes). Kd primarily prevents overshoot on NAS load spikes.

### output_min = 0

PID can output 0. The LoLo cutoff handles the "fan off" state independently.

## 5. LoLo Cutoff

The LoLo cutoff is applied **after** the PID output, at the point where speed is published to MQTT:

- If `temp < LoLo` → publish speed = 0 (fan OFF), regardless of PID output
- If `temp >= LoLo` → publish PID output as-is

This prevents the fan from running in a uselessly cold cabinet and provides a clean on/off hysteresis band between LoLo and LO.

## 6. Simulation Validation

The PID simulation tests (`tests/test_pid_simulation.py`) replay 7-day history and synthetic ramps with these parameters:

```bash
conda activate quiet-rack
pytest tests/test_pid_simulation.py -v
```

Key validated behaviors:

- **Below LoLo (105°F):** fan OFF
- **At LO (115°F):** fan at idle (~0%)
- **Midrange (122.5°F):** fan at ~50%
- **At HI (130°F):** fan at 100%
- **Cold→hot ramp:** speed increases monotonically with temperature
- **Real history replay:** no violations (fan never at high speed when temp is well below LO)

## 7. HA Configuration

### New PWM fan PID controller

Configure in HA UI using values from `pi/config.yaml`:

| Parameter | Config key | Value |
| --- | --- | --- |
| Input sensor | `homeassistant.temperature_entity` | sensor.vault_temperature |
| Output entity | `homeassistant.fan_speed_entity` | input_number.rack_fan_speed |
| Setpoint | `pid.setpoint` | 115.0 |
| Kp | `pid.kp` | 6.7 |
| Ki | `pid.ki` | 0.02 |
| Kd | `pid.kd` | 0.5 |
| Output min | `pid.output_min` | 0 |
| Output max | `pid.output_max` | 100 |
| Sample time | `pid.sample_time` | 30 |

### Old AC fan (backup) — update existing automations

| Automation | Change |
| --- | --- |
| Garage Rack Fan On High Temp | Change threshold from `above: 132` to `above: 135` (HiHi) |
| Garage rack norm temp | Change threshold from `below: 128` to `below: 130` (HI) |

The old fan now only fires as emergency backup above 135°F.

## 8. Future Tuning

After installation, observe the HA dashboard for 24–48 hours:

1. If the fan oscillates near LO → reduce Kp (try 5.0)
2. If temperature creeps above 130°F without fan catching up → increase Ki (try 0.03)
3. If fan overreacts to brief NAS spikes → reduce Kd (try 0.2 or 0)
4. If fan cycles on/off near LoLo → lower LoLo (try 100°F) or add hysteresis
5. Record final values back into `pi/config.yaml`
