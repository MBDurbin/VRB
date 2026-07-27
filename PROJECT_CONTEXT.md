# Resistor Bank Master — Project Context

Self-contained summary of the system, its validated constants, and its open
questions. Written to be dropped into a Claude Project (or read cold by anyone
new to the repo).

Last updated: 2026-07-25. Reflects `master` through the vehicle-model
parameterisation commit.

---

## What this system does

An FSAE electrical-load test rig. It discharges a 12S4P lithium pack through a
relay-switched resistor bank, following a recorded lap speed profile, so the
pack can be characterized under realistic race loads without a car.

The lap profile drives a road-load physics model (drag, downforce, rolling
resistance, translational and rotational inertia, drivetrain loss) to compute
required power each second. That power is converted to a target resistance,
quantized to the bank's 0.25 Ω steps, and sent to the resistor Arduino as an
8-bit binary word.

## Architecture

Four processes communicating over bounded `multiprocessing.Queue`s. `main_v2.py`
orchestrates and picks the data source at startup.

```
                 daq_queue              telemetry_queue
hardware_manager ──────────> control_logic ──────────> gui_layout
  (or sil_simulator)              ^                        │
                                  └────────────────────────┘
                                       gui_cmd_queue
```

| Module | Role |
|---|---|
| `main_v2.py` | Entry point. Probes COM ports for a `SIL_KEY` dongle; if present runs SIL mode, otherwise starts the real DAQ process. |
| `hardware_manager.py` | Reads NI cDAQ (current + 12 cumulative cell voltages) and the temperature Arduino (6 OneWire buses × 8 DS18B20 = 48 sensors). Self-healing watchdog thread re-detects unplugged Arduinos. Falls back to simulated data if no NI-DAQ is present. |
| `control_logic.py` | The safety-critical process. FSM, safety trips, lap physics, coulomb counting, resistor bank serial commands. |
| `gui_layout.py` | PyQt6 telemetry UI — live voltage/current plots, 12S cell voltages, 48-sensor thermal heatmap, threshold controls, CSV recording. |
| `sil_simulator.py` | Desk-test plant model. Replaces the DAQ entirely with operator-driven sliders (load, temperature, pack OCV) plus a hardware-fault toggle. |

### Finite state machine

```
DISCONNECTED ──> IDLE ──ARM──> ARMED ──RUN──> RUNNING
                  ^                              │
                  └──────────(laps complete)─────┘

any state ──STOP/trip──> FAULT ──RESET──> IDLE
```

Guards live in `is_valid_transition()`. `ARM` only from `IDLE`, `RUN` only from
`ARMED`, `RESET` only from `FAULT`, `STOP` always accepted.

## Hardware

- **Pack**: 12S4P Molicel INR-21700-P45B, 48 cells.
- **Resistor bank**: 8 relay-switched banks (1 × 0.25 Ω + 7 binary-weighted),
  0.25 Ω resolution, 63.75 Ω max = 255 steps in an 8-bit word.
- **Resistor Arduino**: 9600 baud, answers `RESISTOR_CTRL` to `?WHOAMI`. Has an
  independent 2 s serial watchdog that opens the main contactor and sheds all
  load if the host goes quiet.
- **Temperature Arduino**: 115200 baud, answers `TEMP_SENSOR`. 6 buses × 8
  sensors, 9-bit resolution for speed, non-blocking state machine.
- **NI cDAQ**: modules 5–8, 10:1 dividers (`VOLTAGE_MULTIPLIER = 11.0`).

Both Arduinos and the SIL dongle are discovered by COM-port sweep using the
`?WHOAMI` handshake — no fixed port assignments.

## Vehicle model

All road-load parameters live in the `VehicleParams` dataclass in
`control_logic.py` — nothing physics-related is hardcoded in the maths any more.
Retune by mutating `DEFAULT_VEHICLE`, or pass a variant per call:

```python
fast = VehicleParams(drag_coefficient=0.55, rotational_mass_factor=1.08)
compute_required_power(velocity_ms, acceleration, params=fast)
```

| Field | Default | Notes |
|---|---|---|
| `mass_car_kg` / `mass_driver_kg` | 260 / 70 | 330 kg total |
| `rotational_mass_factor` | 1.05 | Wheels, rotors, axles, drivetrain as virtual mass |
| `air_density_kgm3` | 1.2255 | ISA sea level, 15 °C |
| `gravity_ms2` | 9.81 | |
| `drag_coefficient` / `drag_area_m2` | 0.6 / 2.224 | |
| `lift_coefficient` / `downforce_area_m2` | −1.0 / 2.224 | Negative Cl = downforce |
| `rolling_resistance_coeff` | 0.015 | Applied to the *dynamic* normal force |
| `drivetrain_efficiency` | 0.95 | Chain/gears/bearings |

Two modelling subtleties that are easy to get wrong and are locked by tests:

- **`rotational_mass_factor` applies to acceleration only.** Spinning components
  resist changes in speed but do not press the tyres into the track, so the
  factor must not reach the normal force behind rolling resistance. It has zero
  effect at steady speed.
- **Drivetrain efficiency is asymmetric.** Driving divides by η (the powertrain
  must produce more than reaches the road); braking multiplies by η (friction
  eats part of what would return). η is clamped to [0.01, 1.0] at the use site
  so a mistyped value cannot divide by zero inside the safety-critical process.

`compute_road_load_forces()` returns the individual force terms (drag,
downforce, rolling, accel, total) so the breakdown can be inspected and tested;
`compute_required_power()` builds on it and applies the efficiency correction.

Effect of the rotational-mass and efficiency terms versus the earlier model:
**+5.26 %** at steady speed (efficiency alone), rising to **+10.3 %** under hard
acceleration. At 10 m/s and a = +8 m/s² that moves the bank from 6.73 Ω to
6.10 Ω — roughly two 0.25 Ω steps, so it is resolvable by the hardware rather
than lost in quantisation.

## Datasheet-validated constants

All verified against the **Molicel INR-21700-P45B Product Data Sheet v1.2** for
the 12S4P configuration.

| Quantity | Value | Derivation |
|---|---|---|
| Pack capacity | 18.0 Ah | 4500 mAh typical × 4P |
| Pack full charge | 50.4 V | 4.2 V/cell × 12S |
| Pack cutoff (absolute) | 30.0 V | 2.5 V/cell × 12S |
| Pack nominal | 43.2 V | 3.6 V/cell × 12S |
| Pack DC internal resistance | 45 mΩ | 15 mΩ/cell @50%SOC ÷ 4P × 12S |
| Max continuous current | 180 A | 45 A/cell × 4P |
| Discharge temperature ceiling | 60 °C | datasheet discharge range −40 to 60 °C |
| Undervoltage trip default | 36.0 V | 3.0 V/cell — above the 30.0 V floor, leaving room for IR sag |
| Pack energy | 777.6 Wh | 16.2 Wh × 48 |

**Why the undervoltage trip sits at 36.0 V and not 30.0 V:** 45 mΩ of pack IR
sags 8.1 V at 180 A. A pack resting at a healthy-looking 3.2 V/cell (38.4 V)
reads **2.499 V/cell under load** — through the cell cutoff. The trip threshold
has to leave room for sag or it only fires after damage.

At 180 A the pack is at a **10 C** discharge rate.

## Safety layers

Three independent layers. They must all be verified separately.

1. **Software trips** (`check_safety_trip` in `control_logic.py`) — over-temp,
   over-current, undervoltage. Evaluated in that priority order so the reported
   cause is deterministic. Sends `KILL` to the resistor Arduino and latches
   `FAULT`, which only a manual `RESET` clears.
2. **Operator E-STOP** — GUI button, routes through `send_command_nonblocking()`
   so a backed-up command queue can never freeze it.
3. **Arduino serial watchdog** — 2 s of host silence opens the main contactor
   and sheds all load, independent of the host entirely.

Layer 3 is the backstop when layer 1 is stalled (see Open Questions).

## Testing

```bash
python -m pytest
```

48 unit tests, no hardware required. They cover the pure decision logic extracted
from the control loop: trip thresholds at/around boundaries, FSM transition
guards, coulomb counting and SOC clamping, the thermal derate curve, road-load
power, and the E-STOP command dispatch path.

`tests/SIL_CHECKLIST.md` is the manual desk-test procedure for what unit tests
structurally cannot reach — process/queue plumbing, GUI responsiveness, timing,
and shutdown hygiene. Requires the SIL dongle; no battery or DAQ needed.

## Bugs found and fixed (2026-07-23 → 25)

| Bug | Consequence |
|---|---|
| DAQ process never started in physical mode | `daq_queue` stayed empty, control loop never ran without the SIL dongle — bank and safety monitors effectively dead |
| Arduino watchdog never reset `MainRelay` after the first timeout | One serial hiccup stranded the main contactor open until power cycle |
| `auto_detect_resistor()` probed at 115200 vs the Arduino's 9600 | `?WHOAMI` handshake could never succeed |
| Reconnect scans ran every DAQ tick, no cooldown | Telemetry loop stalled seconds at a time (~2–4 s per COM port) |
| Spinboxes used Qt default `keyboardTracking` | Typing `100` into Max Temp emitted 1 → 10 → 100; the intermediate `1.0` applied as a live limit and tripped a spurious OVERTEMP kill mid-run |
| GUI buttons used blocking `.put()` on a bounded queue | A full queue froze the UI including E-STOP |
| Derate divided by `(max_safe_temp - derate_start_temp)` unguarded | `ZeroDivisionError` crashed the safety-critical process on misconfigured thresholds |
| **No undervoltage protection existed** | `V Warn`/`V Crit` only moved dashed lines on the plot; they were never sent to the logic process. Nothing prevented discharge past the 2.5 V/cell cutoff |
| Current limit 182 A, temp limit 65 °C | Both exceeded datasheet ratings (180 A / 60 °C). E-STOP only fired at 187 A = 46.75 A/cell, 3.89 % over rated |

## Open questions and known gaps

**1. SOC reads optimistically high.** `coulomb_step` integrates against a fixed
18.0 Ah nameplate figure. Two problems: it uses *typical* capacity (4500 mAh)
rather than *minimum* (4300 mAh, → 17.2 Ah), overstating by up to 4.65 %; and it
ignores rate dependence, though 180 A is a 10 C discharge where real deliverable
capacity is meaningfully lower. Quantifying the second needs the datasheet's
Discharge Rate Characteristics curve, which is a plotted graph — the values have
to be read off by eye.

**2. The `(voltage * 9) ** 2` term in `compute_target_resistance()` is
unexplained.** Standard road-load would be `R = V² / P`. The ×9 implies a
voltage nine times pack voltage. This may be correct for the bank's actual
topology, but nothing in the code documents why. Worth confirming — it changes
target resistance by 81×. The current-limit clamp bounds the downside, so it has
not caused a visible failure.

**2b. The regen branch of the power model is currently inert.**
`compute_target_resistance()` maps any `req_power <= 0` to `MAX_RESISTANCE`, so
braking power of −51.7 kW and −52.2 kW command the same 63.75 Ω — the bank is
purely dissipative and cannot recover energy. The braking efficiency correction
is still physically correct and matters if computed power is ever used for
energy prediction, but it does not change bank behaviour today. Coulomb counting
uses measured current, not this figure, so it does not propagate there either.

**3. E-STOP latency during port scanning.** The queue fix guarantees the GUI
never freezes, but the logic process still cannot *act* on a STOP while blocked
inside `auto_detect_resistor()`. This is tolerable only because `res_ser` is
`None` during a scan — the resistor Arduino is disconnected, so its own 2 s
watchdog has already shed load in hardware. **This means load-shedding in that
window rests entirely on the Arduino watchdog, not on software.** Verify on the
bench.

**4. The Arduino sketches are not in this repo.** `Arduino_Slave_Code_v3.ino`
lives under `OneDrive\Documents\Arduino\`. The watchdog contactor fix is
therefore unversioned and unbacked-up, despite being a safety layer.

**5. No cross-validation between threshold spinboxes.** Nothing prevents setting
`V Warn` below `V Crit`, or `Derate Start` above `Max Temp`. The derate case now
fails safe rather than crashing, but the UI still accepts nonsense combinations.

**6. Everything so far is desk-tested only.** No validation against real
hardware, real cells, or real current has been performed.
