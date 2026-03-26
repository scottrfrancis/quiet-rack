"""Tach signal simulator for testing fan behavior without hardware.

Models the Arctic P12 PWM fan's physical characteristics:
- Max RPM: ~1800 at 100% duty
- Min RPM: ~300 at ~15% duty (below this the fan stalls)
- Stall threshold: ~12% duty — below this, RPM drops to 0
- Spin-up time constant: ~2 seconds to reach target RPM
- Spin-down time constant: ~3 seconds (fan coasts on inertia)
- 2 tach pulses per revolution (standard for 4-pin fans)

The simulator produces pulse counts that would accumulate in the
tach_state["pulse_count"] counter over a given time interval,
matching the interface used by fan_controller.py's run_loop.
"""

import math


# Arctic P12 PWM characteristics
MAX_RPM = 1800.0       # RPM at 100% duty
MIN_RPM = 300.0        # RPM at minimum running duty (~15%)
STALL_PCT = 12.0       # below this duty %, fan stalls to 0 RPM
SPINUP_TAU = 2.0       # seconds — time constant for acceleration
SPINDOWN_TAU = 3.0     # seconds — time constant for deceleration (coasting)
PULSES_PER_REV = 2     # standard 4-pin fan


def duty_to_steady_state_rpm(duty_pct):
    """Map duty cycle percentage to steady-state RPM.

    Below the stall threshold, RPM is 0.
    Above it, RPM scales linearly from MIN_RPM to MAX_RPM.
    """
    if duty_pct < STALL_PCT:
        return 0.0
    # Linear interpolation: stall% → MIN_RPM, 100% → MAX_RPM
    fraction = (duty_pct - STALL_PCT) / (100.0 - STALL_PCT)
    return MIN_RPM + fraction * (MAX_RPM - MIN_RPM)


class TachSimulator:
    """Simulates fan tach behavior with realistic inertia.

    Usage:
        sim = TachSimulator()
        sim.set_duty(50)         # command 50% speed
        sim.advance(10.0)        # advance 10 seconds of simulated time
        pulses = sim.get_pulses(interval=10.0)  # pulses in last 10s window
        rpm = calc_rpm(pulses, 10.0)             # use fan_controller's calc
    """

    def __init__(self):
        self.current_rpm = 0.0
        self.target_rpm = 0.0
        self.duty_pct = 0.0
        self._failed = False
        self._stuck = False
        self._time = 0.0

    def set_duty(self, pct):
        """Set the commanded duty cycle. Updates target RPM."""
        self.duty_pct = max(0, min(100, pct))
        if not self._failed and not self._stuck:
            self.target_rpm = duty_to_steady_state_rpm(self.duty_pct)

    def advance(self, dt):
        """Advance simulation by dt seconds. RPM approaches target with inertia."""
        if self._failed:
            # Failed fan decelerates to 0 — faster than normal coast
            # (motor drag without drive, not just inertia)
            tau = SPINDOWN_TAU / 2
            self.current_rpm *= math.exp(-dt / tau)
            if self.current_rpm < 10.0:
                self.current_rpm = 0.0
            return

        if self._stuck:
            # Stuck fan stays at 0 regardless of command
            self.current_rpm = 0.0
            return

        # Exponential approach to target
        if self.target_rpm > self.current_rpm:
            tau = SPINUP_TAU
        else:
            tau = SPINDOWN_TAU

        diff = self.target_rpm - self.current_rpm
        self.current_rpm += diff * (1 - math.exp(-dt / tau))

        # Below ~10 RPM, consider it stopped
        if self.current_rpm < 10.0 and self.target_rpm == 0:
            self.current_rpm = 0.0

        self._time += dt

    def get_pulses(self, interval):
        """Return the number of tach pulses that would occur over `interval` seconds
        at the current RPM. Uses the average RPM over the interval."""
        revolutions = (self.current_rpm / 60.0) * interval
        return int(revolutions * PULSES_PER_REV)

    def inject_to_tach_state(self, tach_state, interval):
        """Write simulated pulses into a tach_state dict (same format as fan_controller)."""
        tach_state["pulse_count"][0] = self.get_pulses(interval)

    def fail(self):
        """Simulate fan failure — motor stops, RPM decays to 0."""
        self._failed = True
        self.target_rpm = 0.0

    def recover(self):
        """Recover from failure — fan responds to duty again."""
        self._failed = False
        self.target_rpm = duty_to_steady_state_rpm(self.duty_pct)

    def stick(self):
        """Simulate stuck/seized fan — RPM immediately 0, ignores commands."""
        self._stuck = True
        self.current_rpm = 0.0

    def unstick(self):
        """Recover from stuck state."""
        self._stuck = False
        self.target_rpm = duty_to_steady_state_rpm(self.duty_pct)

    @property
    def rpm(self):
        return self.current_rpm

    @property
    def is_spinning(self):
        return self.current_rpm > 10.0

    @property
    def is_at_target(self):
        if self.target_rpm == 0:
            return self.current_rpm < 10.0
        return abs(self.current_rpm - self.target_rpm) / self.target_rpm < 0.02
