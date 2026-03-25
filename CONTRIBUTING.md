# Contributing

This is a small personal IoT project, but contributions and suggestions are welcome.

## Getting Started

1. Fork and clone the repo
2. Read the [design guide](rack_fan_guide_1.md) for full context on hardware and architecture decisions

## Credentials and Site Config

All site-specific values live in `pi/config.yaml`, which is **gitignored**. Never commit credentials, IP addresses, or device-specific settings.

If you add a new configurable value:

1. Add it to `pi/config.example.yaml` with a sensible placeholder
2. Load it in `fan_controller.py` from the `cfg` dict
3. Document it in the config template comments

## Code Style

- **Python**: standard library where possible, minimal dependencies. The Pi Zero W has limited resources — keep it simple.
- **YAML**: Home Assistant config snippets should be copy-paste ready. Mark site-specific values with comments.

## Commits

This project uses [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add tach disable option via config
fix: correct RPM calculation for non-default intervals
docs: add wiring photo to design guide
```

## Pull Requests

- One logical change per PR
- Update the design guide if your change affects hardware, wiring, HA config, or the build checklist
- Test on real hardware if possible (or describe what you tested and what you couldn't)

## Reporting Issues

Open an issue with:

- What you expected vs what happened
- Your hardware (Pi model, fan model, power supply)
- Relevant logs (`journalctl -u fan-controller`)

## Scope

This project targets a specific use case: single-fan PID control for a small rack cabinet. Changes that add significant complexity (multi-fan, web UI, local PID loop) are better as forks unless there's a clear case for inclusion.
