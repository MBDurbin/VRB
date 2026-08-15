# Resistor Bank Master — Project Context

Self-contained summary of the system, its validated constants, and its open
questions. Written to be dropped into a Claude Project (or read cold by anyone
new to the repo).

Last updated: 2026-07-25. Reflects `master` through the cell-level and
sensor-integrity safety work.

---

## What this system does

An FSAE electrical-load test rig. It discharges **one 12S4P module** through a
relay-switched resistor bank, following a recorded lap speed profile, so that
module can be characterized under realistic race loads without a car. The car
itself carries nine such modules in series; the rig reproduces what one of them
would experience. See Battery topology below — this distinction matters.

The lap profile drives a road-load physics model (drag, downforce, rolling
resistance, translational and rotational inertia, drivetrain loss) to compute
required power for each row. That power is converted to a target resistance,
quantized to the bank's 0.25 Ω steps, and sent to the resistor Arduino as an
8-bit binary word.

**Playback follows the profile's own `Time (s)` column**, via
`lap_row_interval()`. It used to advance one row per wall-clock second
regardless, which happens to match the shipped 1 Hz profile but would run a
10 Hz log ten times too slow *and* hold each power demand ten times too long,
over-draining the pack by the same factor. `compute_lap_physics()` already
derived acceleration from those timestamps, so fixed playback also made the two
disagree about how much time a row represents.

**Lap boundaries are continuations, not restarts.** `is_first_row` means the
standing start of the *run* (lap 1, row 0) — tying it to `row_idx == 0` alone
made every subsequent lap begin from rest, discarding the car's carried velocity
and dropping the acceleration term from that frame's power demand. And because
the profile's `Time (s)` restarts each lap, the raw difference at the boundary
is large and negative (`0.0 - 60.0`); the non-positive guard silently replaced
it with 1.0 s, dividing a real velocity change by a fabricated interval. That
frame now uses `lap_row_interval()` — the profile's own sampling rate, not a
literal, so it stays correct at any log rate.

Note the shipped profile begins and ends at 0 mph, so its own boundary is
benign. A multi-lap run on it is N repeats of a stop-to-stop lap rather than a
continuous stint. The fix matters for profiles that are loop-closed at speed.

`load_lap_profile()` drops rows with non-finite speed or time. Exported
telemetry commonly carries trailing blanks — the shipped profile has seven — and
NaN propagates to a NaN power demand, which loses every comparison in
`compute_target_resistance()` so `min(MAX_RESISTANCE, nan)` quietly returns
`MAX_RESISTANCE`. The bank sat at minimum load for the tail of every lap with
nothing reported.

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
| `rig_config.py` | Vehicle model, pack spec, DAQ mapping and safety limits, with JSON persistence. Everything a future team retargets lives here. |
| `theme.py` | Colour, type and spacing for the whole interface. One global stylesheet; widgets carry a `variant` property rather than inline CSS. |
| `hardware_manager.py` | Reads NI cDAQ (current + cumulative cell voltage taps) and the temperature Arduino (OneWire buses of DS18B20s). Channel map and sensor layout come from config. Self-healing watchdog thread re-detects the temperature Arduino. Falls back to simulated data, sized to the configured pack, if no NI-DAQ is present. |
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

- **Module under test**: 12S4P Molicel INR-21700-P45B, 48 cells, ~50 V. This is
  what the bench actually loads.
- **Car's battery** (reference only): 9 modules in series, 432 cells, ~450 V.
- **Resistor bank**: binary ladder of 8 relay-switched steps —
  0.25, 0.5, 1, 2, 4, 8, 16, 32 Ω. 0.25 Ω resolution, 63.75 Ω total, 255 steps
  in an 8-bit word. Wired across the module under test.
- **Resistor Arduino**: 9600 baud, answers `RESISTOR_CTRL` to `?WHOAMI`. Has an
  independent 2 s serial watchdog that opens the main contactor and sheds all
  load if the host goes quiet.
- **Temperature Arduino**: 115200 baud, answers `TEMP_SENSOR`. 6 buses × 8
  sensors, 9-bit resolution for speed, non-blocking state machine.
- **NI cDAQ**: modules 5–8, 10:1 dividers (`VOLTAGE_MULTIPLIER = 11.0`).

Both Arduinos and the SIL dongle are discovered by COM-port sweep using the
`?WHOAMI` handshake — no fixed port assignments.

**One process owns each device, and only that process sweeps for it.**
`control_logic` owns the resistor controller; `hardware_manager` owns the
temperature Arduino. Both used to hunt for `RESISTOR_CTRL`, which meant
`hardware_manager` could never claim the port `control_logic` was holding, so
its "still looking" flag never cleared and the sweep ran every 3 s forever —
blocking 2 s per baud per port, and DTR-resetting any Arduino it managed to
open. If a sweep landed inside `control_logic`'s reconnect cooldown it could
grab the resistor controller and reset it out from under the process driving the
bank. Keep discovery single-owner.

**[docs/hardware_topology.md](docs/hardware_topology.md) is the physical
reference**: what each ladder step is actually built from, how the bank is
arranged for cooling, the DS18B20 ROM address map, and the gaps between what the
hardware needs and what the software enforces. Two of those gaps matter before
any run — **there is no fan interlock**, and the bank has **no thermal
protection of its own**.

## Interface conventions

All styling lives in `theme.py` and is applied as one global stylesheet via
`apply_theme(app)`. Widgets declare intent with a `variant` property
(`primary`, `success`, `warning`, `danger`, `card`, `section`, `caption`,
`mono`) instead of carrying their own CSS, so the look stays coherent as the
interface grows. Three conventions are worth preserving:

- **Colour carries meaning.** Red is a fault or a hard limit, amber is a warning
  or an armed-but-not-running state, green is healthy or active. Nothing
  decorative uses those three, so an operator can trust what a colour means.
- **Live numbers are monospaced.** Proportional digits change width as values
  update, which makes readouts visibly twitch. Metric cards also hold a fixed
  width and static captions for the same reason — a caption that grows with its
  value makes the card reflow mid-run.
- **The E-STOP is deliberately the loudest control on screen** and sits apart
  from the routine controls, so it cannot be hit while reaching for Run or
  Reset. Do not tone it down for visual balance.

The thermal map ramp (`theme.HEAT_STOPS`) routes cool → teal → amber → red
rather than interpolating blue straight to red, which passes through a muddy
purple midpoint and makes mid-range cells impossible to rank.

## Retargeting the rig for a new car or new cells

**Start here if you have inherited this rig.** Click **⚙ CONFIGURE** in the GUI.
Nothing below requires editing Python.

The dialog has two tabs. **Vehicle** describes the car — mass, aero, tyres,
drivetrain. **Battery Pack** takes the numbers straight off your cell's
datasheet plus your series/parallel counts. Pack limits are *derived* from those
values, so entering `45 A` continuous and `4P` produces a 180 A pack rating
automatically; you should never be hand-computing a pack limit.

A live panel shows the derived pack figures and the resulting trip points as you
type, and warns in red if a limit would exceed what the cells are rated for.
Settings persist to `rig_config.json` beside the source, so they survive
restarts and travel with the repo.

One safety rule is enforced in the derivation: **the E-STOP fires at
`max_amps + amp_buffer`, so the buffer is taken out of the cell rating, not
added on top of it.** With a 180 A pack and a 5 A buffer the operating limit
derives to 175 A and the trip lands exactly on 180 A. Deriving the limit as the
raw rating would put the real trip at 185 A — above the cells — which is exactly
the bug that shipped originally as 182 A / 187 A.

Hand-editing a threshold in the sidebar sets `derive_from_pack = False`, so your
override is not reverted the next time the config loads. Re-enable derivation by
editing that flag in `rig_config.json`.

**Editing `rig_config.json` by hand is different.** If you change a limit there
and leave `derive_from_pack: true`, derivation still wins — that is deliberate,
because a pack change must move the limits with it, or new cells inherit the old
pack's ceiling. What is *not* acceptable is doing it quietly: the reversion can
run in the unsafe direction, turning a deliberate derate from 120 A back into
175 A. So every discarded value is now reported on the console at startup and in
a dialog when the GUI opens. If you meant the edit, set `derive_from_pack` to
`false`.

The **DAQ / Sensors** tab holds the channel mapping and sensor layout: which
cDAQ channel reads current, the ordered list of cumulative voltage taps, divider
and transducer scaling, analog input range, OneWire bus/sensor counts, and the
loop period. These describe physical wiring, so they cannot be *derived* from
S/P counts — but they must agree with them, and the dialog says so in red when
they do not (e.g. "12 voltage channels configured but the pack is 14S").

DAQ changes only take effect on restart, because the NI task and sensor buffers
are built once at process start. The GUI tells you this after saving rather than
letting you believe a rewiring change is already live.

### What retargeting does NOT cover

- **Arduino firmware.** The temperature sketch hardcodes 48 DS18B20 ROM
  addresses across 6 buses. A different sensor count means editing and
  re-flashing the sketch — the Python side will read whatever layout you
  configure, but the addresses themselves live in firmware.
- **Resistor bank hardware.** 8 relays, 0.25 Ω resolution, 63.75 Ω max are
  physical properties of the bank.
- **Physically rewiring the DAQ.** Configuring 14 channels does not create them;
  you need the modules and the taps.

Everything on the Python side now follows the config: the DAQ derives cell
voltages by differencing however many taps are configured, sizes its temperature
grid from the bus/sensor counts, and the GUI adapts its cell list, thermal map
and CSV columns to match.

## Vehicle model

All road-load parameters live in the `VehicleParams` dataclass in
`rig_config.py` (re-exported from `control_logic` for convenience) — nothing
physics-related is hardcoded in the maths any more. Edit these from the GUI's
Configure dialog, or pass a variant per call:

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

## Battery topology — read this before touching the physics

**The bench loads ONE module. The car has nine of them in series.** Almost every
mistake in this codebase has come from conflating the two.

- A module is `series_count` × `parallel_count` cells (default 12S4P, ~50 V).
- The car's battery is `modules_in_series` (default 9) of those in series, ~450 V.
- The DAQ reads taps on **one** module, and the resistor bank is wired across
  **that one module only**. So `voltage` in the telemetry packet is ~50 V, and
  the bank never sees 450 V.
- Series modules all carry the same current, so **180 A is simultaneously the
  per-module limit and the whole-battery limit**. Current limits do not scale
  with module count.

The lap physics computes power for the **whole car**, drawn from the 450 V
battery. Reproducing that on one module means reproducing its **current**:

```
I_car = P_car / V_battery = P_car / (N · V_module)
R     = V_module / I_car  = N · V_module² / P_car
```

So the module count enters the power term **linearly**, and both current clamps
divide the **module** voltage, because that is what is actually across the bank.

This was the source of a real bug. The term read `(voltage * 9) ** 2` — N²
rather than N, which is the resistance for a bank spanning the entire battery.
On a single-module bench that **under-loaded by a factor of 9** across the whole
lap: an 81 kW demand drew 20 A instead of 180 A, and no plausible car power could
reach the ladder's 0.25 Ω step (it would have needed 810 kW). With the fix, 81 kW
lands at 180 A and 0.278 Ω — just above the smallest step, which is what the
hardware was sized for.

Sanity check any change here against the ladder: 0.25 Ω ↔ 200 A ↔ ~90 kW.

## Safety layers

Three independent layers. They must all be verified separately.

1. **Software trips** (`evaluate_safety` in `control_logic.py`) — over-temp,
   over-current, module undervoltage, **per-cell undervoltage**, cell sense
   fault, and **temperature data staleness**. Measured dangers are reported
   ahead of data-integrity faults, so an over-current with a dead thermal link
   reports the current. Sends `KILL` to the resistor Arduino and latches
   `FAULT`, which only a manual `RESET` clears.

   Two of these exist because pack-level checks alone were not enough:

   - **Per-cell undervoltage.** Eleven cells at 3.40 V plus one at 1.00 V totals
     38.4 V — above a 36.0 V module trip — while that one cell is destroyed. A
     module-total trip cannot see it. Readings below `cell_sense_floor` report
     as a sense fault rather than undervoltage, because a flat cell and an
     unplugged sense lead are indistinguishable and both must stop the run.
   - **Temperature staleness.** The DAQ republishes its last temperature array
     when the sensor link drops, so a dead sensor looks like a steady pack. A
     link lost at 52 °C froze the reading at 52 °C and the overtemp trip could
     never fire while the cells kept heating. Readings now carry an age and are
     refused past `temp_stale_timeout_s`.

   **Over-temperature and over-current are checked in every state.** Everything
   else — undervoltage, per-cell, and all data-integrity checks — applies only in
   `ARMED`/`RUNNING`, because before arming those readings describe a bench that
   is not loaded yet. A rig powered up before the battery is plugged in reads
   0.0 V; tripping on that in `IDLE` latched a fault `RESET` could not clear
   (back to `IDLE`, instantly re-trips) and locked the operator out of software
   bring-up entirely.

   Once armed, missing data is a **fault**, not a reason to skip a check.
   `check_cell_safety` reports `NO CELL DATA` on an empty array rather than
   returning safe — a broken harness or empty channel list would otherwise run a
   high-power profile blind to exactly what that check exists for.

   **The logic loop never skips its body.** It previously did `continue` when the
   DAQ queue was empty, which jumped past GUI commands, every safety check, the
   resistor heartbeat and telemetry forwarding. A hung DAQ therefore paralysed
   the logic process: the operator's E-STOP sat unread in the queue and the GUI
   kept displaying `RUNNING`. The Arduino's own 2 s watchdog still shed the load,
   but nothing in software noticed or reported it. The loop now carries the last
   packet forward tagged with its age, and `DAQ DATA STALE` faults on it.
2. **Operator E-STOP** — GUI button, routes through `send_command_nonblocking()`
   so a backed-up command queue can never freeze it.
3. **Arduino serial watchdog** — 2 s of host silence opens the main contactor
   and sheds all load, independent of the host entirely.

Layer 3 is the backstop when layer 1 is stalled (see Open Questions).

## Testing

```bash
python -m pytest
```

194 unit tests, no hardware required. They cover the pure decision logic extracted
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

**0. Coulomb count carries across runs, and only rebaselines on restart.**
`RUN` used to reset `remaining_ah` to full, so two manually-triggered
back-to-back laps both started at 100% and the counter forgot what the first
drew — overstating remaining charge, the dangerous direction. It now carries
over, and the starting SOC is printed when a run begins. It rebaselines on a
pack/config change or a process restart. If you want an explicit "fresh battery"
reset in the GUI, that is a deliberate affordance someone should add; silently
doing it on every `RUN` was not.

**1. SOC reads optimistically high.** `coulomb_step` integrates against a fixed
18.0 Ah nameplate figure. Two problems: it uses *typical* capacity (4500 mAh)
rather than *minimum* (4300 mAh, → 17.2 Ah), overstating by up to 4.65 %; and it
ignores rate dependence, though 180 A is a 10 C discharge where real deliverable
capacity is meaningfully lower. Quantifying the second needs the datasheet's
Discharge Rate Characteristics curve, which is a plotted graph — the values have
to be read off by eye.

**2. The regen branch of the power model is currently inert.**
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
