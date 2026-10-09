# VRB — Variable Resistor Bank

A battery-module dyno for BYU FSAE. It discharges **one 12S4P module** through a
relay-switched binary resistor ladder (0.25–63.75 Ω), following a recorded lap
speed profile, so the module can be characterised under race loads without a car.

The design, the physics and the safety layers are explained in
[PROJECT_CONTEXT.md](PROJECT_CONTEXT.md). This page is the map.

## Running it

| What | Command |
|---|---|
| The rig | `python main_v2.py` |
| Desk test without hardware | plug in the SIL dongle, then `python main_v2.py`; follow [tests/SIL_CHECKLIST.md](tests/SIL_CHECKLIST.md) |
| Unit tests | `python -m pytest` (the firmware bank-map test also needs `g++` or `clang++`; it skips without one) |
| Relay chatter check (no hardware) | `python tools/relay_timing_check.py` |

Run everything from this folder. `rig_config.json` holds the car, pack, wiring and
limits, and is edited from the GUI's **Configure** button. The resistor trip
temperatures are edited in the file itself.

## What's where

| Path | What it is |
|---|---|
| `main_v2.py` | Entry point. Starts the DAQ, logic and GUI processes, or SIL mode if the dongle is present. |
| `control_logic.py` | The safety-critical process: state machine, every trip, lap physics, resistor commands. |
| `hardware_manager.py` | Reads the NI-DAQ (current, cell taps, resistor thermocouples) and the temperature Arduino. |
| `gui_layout.py`, `theme.py` | The PyQt6 interface. |
| `sil_simulator.py` | Desk-test plant model that stands in for the DAQ. |
| `rig_config.py`, `rig_config.json` | All configuration, and the file it is saved to. |
| `queue_util.py` | `put_latest()`: publishes DAQ packets and telemetry onto the bounded queues without ever blocking. |
| `profiles/` | Lap speed profiles (CSV). The GUI's **Load CSV** dialog opens here. |
| `logs/` | Recordings from the GUI's **Record** button. Not tracked by git. |
| `tools/` | Bench and desk tools, run by hand: see below. |
| `analysis/` | Offline cooling analysis of the bank. Not used by the running rig. |
| `arduino/` | Firmware for the resistor controller and the temperature array. See its README. |
| `datasheets/` | The cell and resistor datasheets the limits come from. |
| `docs/` | Hardware topology and the packaged audit snapshot. |
| `tests/` | Unit tests and the SIL desk-test checklist. |
| `legacy/` | Superseded prototypes, kept for reference only. Not maintained. |

### tools/

| Script | Use |
|---|---|
| `bench_relay_check.py` | Command resistances and meter each relay. Battery disconnected. |
| `relay_timing_check.py` | Replays the control loop's serial traffic against the firmware's relay rules. No hardware. |
| `thermocouple_check.py` | Reads the resistor thermocouples once (Bryce's bench test). |
| `discover DAQ.py` | Lists every NI-DAQ channel name, for filling in `rig_config.json`. |
| `Current Sensor.py` | Streams the current transducer reading. |

### analysis/

`CFM Calculator.py` sizes the fan for the bank's worst case. `resistor_thermal.py`
models how hot the elements get over a full discharge. Both write their figures to
`analysis/plots/`.

### legacy/

The single-file v1 controller (`Master_Code_v1.py`) and its tests, the tkinter GUI
prototypes, and early serial bench scripts. The current rig replaced all of them.
Some still hold absolute paths from the machine they were written on.
