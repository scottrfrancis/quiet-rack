# Quiet Rack: Silencing My Home Lab with a $32 PID Fan Controller

## Series Overview

A multi-part build guide for replacing a loud always-on rack fan with a quiet, proportional PWM fan controlled by a Raspberry Pi and Home Assistant. Written for home hobbyists and makers who know their way around a soldering iron and a terminal prompt but haven't necessarily taken a controls theory class.

**Working title options:**
- "How I Taught My Server Rack to Use Its Indoor Voice"
- "From Bang-Bang to Smooth Operator: PID Fan Control for Home Labs"
- "$32 and a Weekend: Quiet PID Fan Control for Your Home Lab"

**Series structure:** 5 parts, each stands alone but builds on the last.

---

## Part 1: The Problem (and Why Your Smart Plug Is Doing It Wrong)

*Hook the reader. Establish the pain. Show why bang-bang control is caveman engineering.*

### The Rack That Wouldn't Shut Up
- The setup: 12U wall-mount cabinet in the garage, Synology NAS, network gear
- The villain: a 120V AC muffin fan running at full scream 24/7
- The first "fix": a smart plug with a Home Assistant automation — fan ON above 135°F, OFF below 130°F
- Why bang-bang control is like driving with only the gas pedal and the emergency brake — it works, but everyone in the car hates you

### What We Actually Want
- Proportional cooling: fan speed tracks temperature, not a binary threshold
- Quiet at idle, aggressive under load, silent when the vault is cool
- No new noise source — the whole point is *less* noise
- Visible in the existing HA dashboard, not another app to check

### The Plan
- Replace the AC fan with a 4-pin PWM DC fan (Arctic P12, $9)
- Use a Pi Zero W as a dumb MQTT-to-PWM bridge
- Let Home Assistant run the PID loop — it already has the temperature data
- Teaser: "This sounds simple. It was not simple. But it was fun."

### Bill of Materials (~$32)
- Full BOM table with links
- What you probably already own (Pi, USB charger, screwdriver, opinions)
- The one part that saved hours: a $7 fan extension cable (don't solder directly to your fan leads)

---

## Part 2: The Build — Wiring, Pi Setup, and the Ground Bond Nobody Tells You About

*Hands-on hardware. Solder, crimp, deploy. Lots of "don't do what I did" moments.*

### Understanding the 4-Pin Fan Connector
- Pin 1 (GND), Pin 2 (+12V), Pin 3 (tach), Pin 4 (PWM)
- The latch tells you which end is pin 1 — diagram with "this side up"
- Why we care about tach: it's optional, but it's free health monitoring

### Two Power Supplies, One Ground
- Why two rails: 12V for the fan, 5V USB for the Pi
- The ground bond: one jumper wire from Pi GND to 12V supply GND
- "I tried a buck converter. The Pi said no." (Won't boot — still unsolved, probably a ripple issue)
- Without the ground bond, the PWM signal has no reference and the fan ignores you. With it, everything works. This is the step every tutorial skips.

### Building the Wiring Harness
- Start with the fan extension cable — cut it in half, use the female (socket) end
- Step-by-step: strip, tin, solder 12V leads, crimp Dupont connectors for GPIO header
- Heat-shrink everything — this lives inside a metal cabinet
- Keep the PWM wire short (<30cm) — long wires pick up RF noise (foreshadowing the tach crosstalk bug in Part 5)
- Finished harness photo / diagram

### Pi Zero W Setup
- Pi Imager: headless, SSH enabled, WiFi configured, hostname set
- First boot, SSH in, apt update
- Python venv (not conda — ARMv6 doesn't support it. Ask me how I found out.)
- Install deps: `pip install paho-mqtt pigpio PyYAML`
- Enable pigpiod: `sudo systemctl enable pigpiod`
- The config file: `config.yaml` with MQTT broker, GPIO pins, topics — never hardcode credentials

### The Controller Script
- Walk through `fan_controller.py` at a high level (not line-by-line)
- It does four things: connect to MQTT, subscribe to speed topic, set PWM, optionally report RPM
- "250 lines of Python doing the work of one knob on a fan controller. Progress."
- systemd service file: auto-start, auto-restart, journal logging

### Deploy and Smoke Test
- `scp` the files to the Pi, start the service
- Use `mosquitto_pub` or the debug client to send a speed command
- Hear the fan spin up. Feel the dopamine. This is why we do this.

---

## Part 3: Home Assistant — PID Controller, Automations, and the State Trigger Trap

*Software integration. The HA side is where the brains live.*

### Why PID Lives in HA, Not on the Pi
- The sensor data is already there
- Tuning is live in the UI — no SSH, no restarts, no redeployments
- P, I, D terms visible as diagnostic entities — you can literally watch the math
- If the Pi reboots, it picks up the last retained MQTT speed in seconds
- "The Pi is a fan. HA is the brain. Separation of concerns isn't just for enterprise software."

### Installing the PID Controller
- HACS → `simple_pid_controller` integration
- Creating the `input_number.rack_fan_speed` helper (0–100, step 1)
- PID configuration: input sensor, output entity, gains — "we'll tune these in Part 4, don't worry about the numbers yet"

### The Three Automations You Need
1. **Force sensor update** (every 5 min) — Synology only polls every ~15 min, and we're impatient
2. **PID output → helper** (state change + time_pattern /30s) — copies PID output to the input_number
3. **Helper → MQTT** (state change + time_pattern /30s) — publishes to `rack/fan/speed`

### The State Trigger Trap (and How to Escape It)
- HA automations fire on `last_changed`, not `last_updated`
- If the Synology sensor reports 122°F → 122°F, nothing fires. The PID doesn't update. The fan doesn't respond.
- "Your sensor is updating. HA is receiving updates. But nothing is *changing*. This is a philosophy problem disguised as a YAML problem."
- The fix: `time_pattern` as a parallel trigger on every automation. Belt and suspenders.

### The Backup Fan (Old Bang-Bang Lives On)
- The old AC fan automation stays as emergency backup above 135°F (HiHi)
- "It's like keeping a fire extinguisher after installing sprinklers. You hope you never need it."
- Dual-fan zone map diagram: new fan handles 110–130°F, backup kicks in at 135°F

### Optional: Fan Failure Alert
- If RPM < 100 while speed > 20% for 60 seconds → push notification
- Uses MQTT availability topic so a dead Pi shows "unavailable," not stale zero RPM

---

## Part 4: PID Tuning — Four Attempts, Three Failures, and Why I Turned Off the I

*The heart of the series. A debugging detective story with a counterintuitive ending.*

### A 60-Second PID Primer (For People Who Skipped Controls Class)
- P: "How wrong are we right now?" — proportional response
- I: "How wrong have we been over time?" — accumulated error correction
- D: "How fast is the wrongness changing?" — damping / look-ahead
- For our fan: P says "it's hot, spin faster." I says "it's been hot for a while, spin even faster." D says "it's getting hotter fast, spin up NOW."
- "In theory, PID is simple. In practice, PID is a three-knob mystery box that punishes overconfidence."

### Step 0: Know Your Data (InfluxDB Is Your Best Friend)
- Pull 6 months of vault temperature from InfluxDB (11,185 readings)
- The two-cluster discovery: idle at 122–124°F, load at 130–132°F
- Outside temp correlation: r=0.656, vault rises ~0.4°F per °F outdoor
- Seasonal range: 95°F (winter night) to 145°F (summer afternoon)
- "Before you tune a PID, know what normal looks like. Otherwise you're tuning to vibes."

### Attempt 1: The Sign Convention Trap
- Started with positive gains (Kp=+25, Ki=+0.1, Kd=+0.5)
- Fan OFF when hot, ON when cool — exactly backwards
- Two hours of blaming HA, the broker, the wiring, Mercury retrograde
- The breakthrough: reading the `simple-pid` source code. `error = setpoint - measured`
- For cooling: temp above setpoint → negative error → positive gains → negative output → clamped to 0
- **Fix: negate all gains.** Kp=-25, Ki=-0.1, Kd=-0.5
- "The library documentation mentions this in one sentence. I found it after reading the source code, two forum posts, and one existential crisis."

### Attempt 2: Setpoint Too Low
- Setpoint at 115°F — below where the vault ever idles
- Result: fan at 100% for 89% of the night. The integral term kept climbing.
- "The fan was working perfectly. It was also perfectly loud, which is the thing we were trying to fix."
- Lesson: pull your data first. The vault idles at 122°F. Aiming for 115°F is like setting your thermostat to 50°F and wondering why the furnace runs all day.

### Attempt 3: Right Setpoint, Wrong Ki
- Setpoint=122°F, Kp=-12.5, Ki=-0.005, Kd=-0.3
- Looks great in simulation. Looks great for the first 2 hours.
- At hour 4: PID output stuck at 100% even though temp dropped to 123.8°F
- Integral windup: the I-term accumulated because the vault *never* reaches setpoint
- "The integral term is like a grudge. It remembers every degree-minute of wrongness. And it never forgives."

### Attempt 4: Ki=0 (The One That Worked)
- Disabled integral entirely. P+D only.
- 123.8°F → 22.5%. 126°F → 50%. 130°F → 100%. Instant, proportional, correct.
- Why Ki=0 is the *right* answer for this system:
  - The fan can't cool the vault to setpoint. The NAS makes too much heat. There is no equilibrium to converge on.
  - Steady-state offset is fine — we want proportional cooling, not a temperature target
  - Derivative provides useful damping for the 1.8°F sensor steps
- "Every textbook says PID needs all three terms. Every textbook is wrong for this system."
- Zone replay against 6 months of data: 43% in the proportional ramp band — exactly where you want it

### The Final Numbers
- Table: setpoint, gains, zone distribution, overnight behavior
- Tuning iteration comparison: before/after for each attempt
- "Four iterations. Three failures. One weekend. Zero regrets."

---

## Part 5: Making It Bulletproof — MQTT Reliability, the Tach Bug, and Lessons Learned

*Production hardening. The difference between "it works on my bench" and "it works at 3 AM when I'm asleep."*

### MQTT v5: More Than a Version Number
- Why we upgraded from v3.1.1: will delay, session expiry, message expiry
- Topic architecture: speed (QoS 1, retained), rpm (QoS 0, ephemeral), status (QoS 1, retained)
- "QoS 0 for RPM because a missed reading is replaced 10 seconds later. QoS 1 for speed because a missed command leaves your fan at the wrong speed until HA's next publish cycle."

### Last Will and Testament (Your Fan's Dead Man's Switch)
- LWT: if the Pi vanishes, the broker publishes "offline" after 30 seconds
- The 30-second delay: prevents false alarms during WiFi blips and systemd restarts
- Birth message on connect overwrites any stale "offline"
- "It's morbid to name a protocol feature after death planning, but here we are."

### Session Persistence: Don't Lose Messages During a Reboot
- Fixed client ID + 300-second session expiry
- Broker queues QoS 1 messages while Pi is offline
- On reconnect: queued speed commands arrive in order, fan reaches correct speed
- Retained message as belt-and-suspenders for longer outages

### The Recovery Matrix
- Table: failure mode → recovery path → time to recover
- WiFi blip, broker restart, Pi reboot, service crash, pigpiod crash
- "I planned for every failure mode. Then the fan threw one I didn't plan for."

### The Tach Bug: 45,000 RPM (On a Fan Rated for 1,800)
- First overnight with tach enabled: RPM readings of 45,000. Then 366,834.
- "Either I'd accidentally built a jet engine, or something was very wrong with my tach code."
- Root cause: 25kHz PWM on GPIO18 coupling into GPIO24 (tach input) — electromagnetic crosstalk through adjacent traces
- The math that saved us: real tach at 1800 RPM = 60Hz (16.7ms between pulses). PWM noise = 25kHz (40µs between edges). A 5ms debounce filter rejects every fake pulse while passing every real one.
- Before/after: overnight InfluxDB chart showing the spike, then clean readings
- "The fix was three lines of code. The debugging was three hours of staring at InfluxDB."

### Debugging Toolkit (Tips for Your Own Project)
- **InfluxDB is your flight recorder** — query sensor history before and after changes. 30-min aggregates hide the interesting stuff; use point queries when correlating events.
- **Timezone traps** — the Pi, HA, and InfluxDB may all be in different timezones. Ask me how I know. (I spent an entire analysis pass correlating events in the wrong timezone. The 0% fan speed that looked like a bug was the PID correctly responding to a temperature dip I couldn't see until I added 7 hours.)
- **Synology sensor quantization** — 1°C resolution means your 120.2, 122.0, 123.8°F readings are the only values that exist. Your PID output looks stepped on the dashboard because it *is* stepped. The fan is smooth; the sensor is quantized.
- **journalctl on Pi OS** — use absolute timestamps (`"2026-03-27 02:00:00"`), not relative (`"yesterday 20:00"`). The parser doesn't accept relative formats.
- **HA state triggers only fire on `last_changed`** — if the sensor reports the same value twice, your automation doesn't fire. Always add a `time_pattern` fallback.
- **The MQTT debug client** — subscribe to your topics from the workstation, not the Pi. Send test speed commands. Watch retained messages. Save yourself hours.
- **Read your config.yaml, don't guess** — the hostname is `cooler.local`, not `rack-fan`. The SSH user is `scott`, not `pi`. Assumptions kill debugging sessions.

### What I'd Do Differently
- Run the Pi in UTC to match InfluxDB — avoid timezone conversion entirely
- Use shielded wire or physical separation for tach and PWM leads from day one
- Start with Ki=0 and prove you need integral before adding it — not the other way around
- Pull your InfluxDB data *before* designing thresholds, not after tuning fails

### Was It Worth It?
- Before: fan at 100% or OFF, audible from the hallway, 5°F temperature swings
- After: fan spends 43% of its time in proportional ramp, virtually silent at idle, smooth response to load
- Total cost: $32 and a weekend
- "The rack is quiet. The NAS is cool. The dashboard is beautiful. And I have opinions about integral windup now."

---

## Series Metadata

**Estimated length per part:**
| Part | Words | Read time |
|------|-------|-----------|
| 1: The Problem | ~1,200 | 5 min |
| 2: The Build | ~2,500 | 12 min |
| 3: Home Assistant | ~2,000 | 10 min |
| 4: PID Tuning | ~3,000 | 15 min |
| 5: Bulletproofing | ~2,500 | 12 min |
| **Total** | **~11,200** | **~54 min** |

**Visual assets needed:**
- Finished install photo (fan in cabinet, Pi mounted)
- Wiring harness close-up (labeled)
- 4-pin connector pinout diagram
- Pi GPIO pinout (project pins highlighted)
- HA dashboard screenshot (PID output, temp, fan speed, RPM)
- InfluxDB chart: 6-month temperature distribution (two clusters)
- InfluxDB chart: overnight PID tuning comparison (before/after Ki=0)
- InfluxDB chart: tach RPM spike and fix
- Zone map diagram (LoLo → OFF → idle → ramp → FULL → backup)
- PID tuning iteration table (4 rounds, visual comparison)
- MQTT topic architecture diagram
- Recovery matrix table

**Cross-references to existing docs:**
- `rack_fan_guide_1.md` — primary technical source, wiring, BOM
- `docs/pid_tuning.md` — already written as blog-ready narrative (594 lines), source for Part 4
- `.claude/session-logs/2026-03-26-1500-full-build.md` — build timeline, decisions
- Git history (30 commits) — chronological story of the build
