# Resistor Bank Master — Project Context

Self-contained summary of the system, its validated constants, and its open
questions. Written to be dropped into a Claude Project (or read cold by anyone
new to the repo).

Last updated: 2026-11-16. Reflects the move to Reliance RS50 cells, the
October 2026 safety-review fixes, Mooch's measured RS50 figures, and the
November 2026 audit: the load check, the controller read-back, per-tap
calibration and the bus-bar temperature sensors.

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

Rows are due on the profile's timeline, not the loop's: each is due one hold
after the row before it *was due* (`next_row_due()`), so the few tens of
milliseconds each pass arrives late do not add up down the lap. Timed from the
sending, a 10 Hz profile ran 13% long in the timing harness; it now runs 0.7%
long, which is the loop's own pace. An evenly sampled profile holds its last
row for one sample (`last_row_interval()`); a bench profile's last row only
ends the hold before it and keeps the 1 s default.

**The ladder is coarse where the heavy rows sit, and the recording shows by how
much.** A row asking for 0.36 Ω gets the 0.25 Ω step: 43% more current. Over the
shipped lap the charge drawn lands within 5% of what the car model asks for, but
I²t, the cell heating, runs 3–17% high depending on module voltage. No rounding
rule removes it: nearest *current* reads 1–15% low instead, and carrying the
error forward makes single rows worse. So the rounding is unchanged, and every
packet carries `requested_resistance` (the ask, before any floor or step)
beside `target_resistance` (the step sent). Use the two when comparing bench
heat with the car.

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
| `hardware_manager.py` | Reads NI cDAQ (current + cumulative cell voltage taps, plus resistor-bank thermocouples on banks 1–4 in their own task and thread) and the temperature Arduino (OneWire buses of DS18B20s). Channel map and sensor layout come from config. Self-healing watchdog thread re-detects the temperature Arduino. Without the NI-DAQ it publishes zero current and voltage flagged `ni_daq` False, never simulated data. |
| `control_logic.py` | The safety-critical process. FSM, safety trips, lap physics, coulomb counting, resistor bank serial commands. |
| `gui_layout.py` | PyQt6 telemetry UI — live voltage/current plots, 12S cell voltages, 48-sensor thermal heatmap, resistor bank map laid out as built, threshold controls, CSV recording. |
| `sil_simulator.py` | Desk-test plant model. Replaces the DAQ entirely with operator-driven sliders (load, temperature, pack OCV) plus a hardware-fault toggle. |

These run the rig and stay at the top level. Lap profiles, bench tools, the
cooling analysis and the superseded prototypes each have their own folder; the
map is in [README.md](README.md).

### Finite state machine

```
DISCONNECTED ──> IDLE ──ARM──> ARMED ──RUN──> RUNNING
                  ^                              │
                  └──────────(laps complete)─────┘

any state ──STOP/trip──> FAULT ──RESET──> IDLE
```

Guards live in `is_valid_transition()`. `ARM` only from `IDLE`, `RUN` only from
`ARMED`, `RESET` only from `FAULT`, `STOP` always accepted. `ARM` is also refused,
staying in `IDLE`, while `arm_refusals()` is non-empty: the NI-DAQ is offline or
the configuration has ARM blockers (see Retargeting below).

`ARM`, `RUN` and `RESET` carry the time they were pressed
(`transition_command()`), and the loop ignores one pressed before the most
recent stop or fault (`stale_transition()`). Without that, a `RESET`, `ARM` and
`RUN` already waiting in the queue were processed straight after an E-STOP, in
the same pass, and the rig was `RUNNING` again with the next resistance command
reconnecting the load. A fault is cleared only by a `RESET` pressed after it.
The stamps are `time.perf_counter()` (`press_clock()`): `time.monotonic()`
steps every 15.6 ms on Windows before Python 3.13, so a `RESET` pressed in the
same step as its fault tied with it and was dropped.

`DISCONNECTED` is where the rig waits for its resistor controller or its
temperature board. The trips that apply in every state latch from there too; it
used to be skipped, so a waiting rig showed nothing with every cell at 95 °C.
A profile is not swapped under a run, any more than a config is.

## Hardware

- **Module under test**: 12S4P Reliance RS50 (21700, 5000 mAh, 70 A), 48 cells,
  ~50 V, 280 A. This is what the bench actually loads. It replaced the Molicel
  INR-21700-P45B the modules were first built with; see Cell constants below.
- **Car's battery** (reference only): 9 modules in series, 432 cells, ~450 V.
- **Resistor bank**: binary ladder of 8 relay-switched steps —
  0.25, 0.5, 1, 2, 4, 8, 16, 32 Ω. 0.25 Ω resolution, 63.75 Ω total, 255 steps
  in an 8-bit word. Wired across the module under test.
- **Resistor Arduino**: 9600 baud, answers `RESISTOR_CTRL` to `?WHOAMI`. Has an
  independent 2 s serial watchdog that opens the main contactor and sheds all
  load if the host goes quiet, and (in the repo's sketch, on the classic AVR
  boards) the chip's own watchdog timer behind that, for a sketch that hangs.
- **Temperature Arduino**: 115200 baud, answers `TEMP_SENSOR`. 6 buses × 8
  cell sensors, 9-bit resolution for speed, non-blocking state machine. Room
  for a DS18B20 on each bus bar between the series groups, eleven on a 12S
  module, sent on a `B,...` line of their own; none fitted as shipped (see
  Bus-bar temperatures below).
- **NI cDAQ**: slot 6 an NI 9212 (bank thermocouples), slot 7 an NI 9215
  (current), slot 8 an NI 9205 (the 12 taps), 10:1 dividers
  (`voltage_multiplier = 11.0`, with an optional gain per tap).

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
values, so entering `70 A` continuous and `4P` produces a 280 A pack rating
automatically; you should never be hand-computing a pack limit.

A live panel shows the derived pack figures and the resulting trip points as you
type, and warns in red if a limit would exceed what the cells are rated for.
Settings persist to `rig_config.json` beside the source, so they survive
restarts and travel with the repo.

One safety rule is enforced in the derivation: **the E-STOP fires at
`max_amps + amp_buffer`, so the buffer is taken out of the cell rating, not
added on top of it.** With the RS50's 280 A pack and a 5 A buffer the operating
limit derives to 275 A and the trip lands exactly on 280 A. Deriving the limit
as the raw rating would put the real trip at 285 A — above the cells — which is
exactly the bug that shipped originally, on the P45B's 180 A pack, as
182 A / 187 A.

Hand-editing a threshold in the sidebar that derivation computes (V crit, cell
min, max current, max temp, derate start) sets `derive_from_pack = False`, so
your override is not reverted the next time the config loads. Tick **Derive
limits from pack** in the sidebar to go back to the derived values; that used to
mean editing the flag in `rig_config.json`.

**While the limits are hand-set, the main window says `LIMITS HAND-SET` in
amber**, with the figures and the way back in its tooltip, and `LOAD CHECK OFF`
likewise while the load check is switched off. The window saves the config when
it closes, so a session on bench supplies once left the file at 60 A with the
per-cell trip on the 2.5 V cutoff, waiting unseen for the first module.
A per-cell trip sitting exactly on the cutoff is also an advisory now. The other sidebar controls
are not overrides and leave derivation on: **V warn** only moves a dashed line on
the plot and never reaches the controller (it used to switch derivation off for
every limit), and changing the **E-stop buffer** or the **thermal derate**
switch re-derives, so max current follows a new buffer and the trip stays on the
cells' rating.

**What the window shows is checked against what the controller is using.** The
logic process reports its limits in force and a fingerprint of its vehicle, pack
and wiring (`settings_fingerprint()`) in every telemetry packet. If they differ
from the window's for more than a second, a red `NOT IN FORCE ON THE
CONTROLLER` line appears beside the car and pack label, naming the setting. That
catches a config the controller rejected and a `SET_LIMITS` or `SET_CONFIG` the
bounded command queue dropped. Configure is refused outright while RUNNING,
because the controller ignores a new config mid-run; it used to apply and save
it in the window anyway, so a lowered max temp could look active when it was
not. The Configure preview also works on a copy of the limits now: it used to
re-derive into the live limits on every keystroke, so Cancel undid nothing.

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

**Configuration problems come in two classes, and one of them blocks ARM.**
`RigConfig.arm_blockers()` lists the ones that leave part of the module or bank
unwatched: a voltage-tap count that does not match the series count, the
current channel or a thermocouple on a voltage input, duplicate channels, an
invalid thermocouple type or cold-junction source, a non-positive divider or
transducer scale (zero reads no current; negative reads discharge as negative,
which never reaches the over-current trip), a staleness timeout at zero (which
switches that monitor off), any limit that is not a finite number (a trip set to
NaN never fires), and any trip set past a rating, so that it fires only after the
damage: over-current above the pack rating, max temp above the cell's ceiling,
undervoltage below the cutoff, or a resistor trip above its element's rating.
These used to be printed as warnings and the rig armed anyway. They are judged
on the limits the logic process actually holds, so a **sidebar edit** counts the
same as the config file. The logic refuses `ARM` while any stand, the main
window says why in red beside the hardware pills, and a config or sidebar change
that introduces one while armed faults as `CONFIG FAULT` and kills the load.
Everything else is an advisory (`RigConfig.advisories()`), shown and left to the
operator: a derate that cannot ramp (it fails safe to no load), a thermistor
count that does not match the cell count, no resistor thermocouples or a bank
with a trip and no thermocouple, and a bad sample period. Fewer thermistors than cells and missing
thermocouples are advisories by choice, so a rig still being instrumented can
arm; an armed rig whose thermocouples deliver nothing still faults as
`NO RESISTOR TEMP DATA`. A config with blockers can still be saved, so a team
mid-rewire can save and restart.

**The resistor bank is held to its power ratings per bank**, not just to its
8 kW total, and a sidebar switch chooses between 100% and 140% of them. See
"The bank's power ratings" in [docs/hardware_topology.md](docs/hardware_topology.md).

DAQ changes only take effect on restart, because the NI task and sensor buffers
are built once at process start. The GUI tells you this after saving rather than
letting you believe a rewiring change is already live. ARM is judged against the
wiring the DAQ is actually running, so correcting a channel list in the dialog
does not unblock it until after the restart.

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
| `mass_car_kg` / `mass_driver_kg` | 233 / 95.3 | 328.3 kg total: the 2026 car as weighed with today's pack, and the heaviest driver (210 lb) |
| `rotational_mass_factor` | 1.00 | Wheels, rotors, axles, drivetrain as virtual mass. 1.00 switches it off: the 1.05 first used was a textbook figure, not this car's. Raise it once the inertia is measured |
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

Effect of the efficiency term versus the earlier model: **+5.26 %** at every
speed. With a rotational mass factor of 1.05 the total rises to **+10.3 %**
under hard acceleration: at 10 m/s and a = +8 m/s² that moves the bank from
6.73 Ω to 6.10 Ω, roughly two 0.25 Ω steps, so it is resolvable by the hardware
rather than lost in quantisation. As shipped the factor is 1.00, so only the
efficiency term is in force, and the bench slightly under-loads the module
during acceleration.

## Cell constants — Reliance RS50

The module has been **Reliance RS50** since it replaced the Molicel
INR-21700-P45B. The figures come from the supplier's listing (liionwholesale.com,
"Reliance RS50 70A 5000mAh Flat Top 21700") and from **Mooch's independent test
of the cell (October 2025)**, which the team has from Discord. There is still no
manufacturer datasheet. Mooch's card asks that it not be shared after April
2026, so only its numbers are recorded here, not the card or its charts. The
P45B's datasheet stays in `datasheets/` for reference: some tests pin the
derivation math to its published figures.

| Quantity | Cell | 12S4P module | Source |
|---|---|---|---|
| Capacity | 5000 mAh typical, 4950 mAh min (0.2 C) | 20.0 Ah (19.8 Ah min) | listing; Mooch measured 4950 mAh |
| Full charge | 4.2 V | 50.4 V | listing ("peak voltage") |
| Nominal | 3.6 V | 43.2 V | listing |
| Max continuous discharge | 70 A "with 80 °C temperature cut-off" | **280 A** | listing; Mooch confirms 70 A holds only until 80 °C |
| True continuous (stays under 80 °C) | 40 A | 160 A | Mooch |
| Discharge temperature limit | 80 °C | 80 °C over-temp trip | listing (its only temperature figure); Mooch rates to the same 80 °C |
| Discharge cutoff | 2.5 V | 30.0 V absolute floor | **not on the listing**; Mooch's capacity runs end at 2.5 V; no manufacturer figure |
| DC internal resistance | 6.5 mΩ | 19.5 mΩ | Mooch (was a 4 mΩ estimate); about 8 mΩ effective over a sustained discharge |
| Energy | 18 Wh | 864 Wh | 5.0 Ah × 3.6 V |
| Charge current | 15 A max, 8 A standard | — | listing; the rig never charges |

Pack derivation (`derive_from_pack: true`) turns these into the trips: 275 A
operating current and a 280 A over-current trip, 80 °C max temp, 36.0 V module
undervoltage (3.0 V/cell), 2.70 V per cell, and a 38.0 V warning line.

**80 °C is the right trip for this cell.** The P45B needed care here: its 45 A
rating carried an 80 °C *test* cut-off while its discharge operating range
stopped at 60 °C, so the rig used 60. The RS50 listing gives only the 80 °C
cut-off with its 70 A rating and no separate operating range, so the rig trips at
80. Lower `cell_max_temp_c` if an RS50 datasheet gives a narrower discharge range.
The shipped `rig_config.json` uses 65 °C: the sensors sit on the cell tops, which
read cooler than the side wall a cell test measures.

**70 A, not 40 A, sets the current rating, because the rig enforces the 80 °C
condition.** Mooch's 70 A holds only until the cell reaches 80 °C; 40 A is what
it carries for a whole discharge. Every cell has a temperature sensor and the
over-temp trip sits under 80 °C, so the rig enforces the condition the 70 A
depends on. The ladder's ~200 A at the bottom step is 50 A per cell, past the
40 A: a long run at the heaviest settings ends on the temperature trip, not the
current one. That is the intended order. If the temperature sensors are ever
off or unplugged, set `cell_max_continuous_a` to 40.

**Why the undervoltage trip sits at 36.0 V and not 30.0 V:** headroom for sag.
The RS50 module's 19.5 mΩ sags 5.5 V at its 280 A rating, about 3.9 V at the
~200 A the ladder can actually draw (about 4.8 V once polarisation builds over a
sustained discharge, at Mooch's ~8 mΩ effective). The 3.0 V/cell figure came
from the P45B, whose 45 mΩ sagged 8.1 V at 180 A; it still leaves the RS50 well
clear of its cutoff.

`rig_config.json` used to say `max_amps: 180`, the P45B's figure. With
`derive_from_pack: true` it was replaced by the RS50's 275 A on every load, behind
a "hand-edited limits were replaced" dialog, so 275 A was already in force. The
file now says 275.

### What bounds the load: battery current and bank power

Two independent limits, each enforced on what is commanded and again as a trip:

| Limit | On the command | As a trip | Can configuration raise it? |
|---|---|---|---|
| **Battery current** — 280 A for the RS50 module (70 A × 4P) | Resistance never below V ÷ 275 A, rounded up a step | Over-current at 275 + 5 = 280 A, in any state | No: a trip above the pack rating blocks ARM |
| **Bank power** — 8 kW ladder total and each bank's own rating (`BANK_RATED_POWER_W`) | Every setting checked against both at worst-case tolerance; one that fails moves to the nearest that passes | Ladder V × I in any state; each bank's share once a setting has settled | Only between 100% and 140% (the sidebar switch) |

**With the RS50 the bank binds first.** The 0.25 Ω bottom step limits a full
50.4 V module to about 202 A, under the cells' 280 A, so the over-current trip is
a backstop for a fault such as a shorted ladder rather than a limit the rig
reaches in use. At full charge the heaviest load is 0.25 Ω and ~10.2 kW with the
default 140% bank setting; held to rated power, it is 0.75 Ω and ~3.4 kW above
43.6 V. `tests/test_shipped_limits.py` runs every setting the shipped config can
command, across the charge range and at both bank settings, against both limits.

## Battery topology — read this before touching the physics

**The bench loads ONE module. The car has nine of them in series.** Almost every
mistake in this codebase has come from conflating the two.

- A module is `series_count` × `parallel_count` cells (default 12S4P, ~50 V).
- The car's battery is `modules_in_series` (default 9) of those in series, ~450 V.
- The DAQ reads taps on **one** module, and the resistor bank is wired across
  **that one module only**. So `voltage` in the telemetry packet is ~50 V, and
  the bank never sees 450 V.
- Series modules all carry the same current, so **the RS50 module's 280 A is
  simultaneously the per-module limit and the whole-battery limit**. Current limits do not scale
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

   **Over-temperature and over-current are checked in every state**, as are
   bus-bar over-temperature, bank overpower and resistor over-temperature, and
   they latch from `DISCONNECTED` as well. Everything
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

   **Readings are checked before any trip trusts them** (`measurement_problems()`,
   once armed). NaN loses every comparison, so a packet of NaN current, voltage
   and cells used to pass every trip; and a packet with one cell, one sensor and
   one thermocouple passed too, because nothing counted them. Now:
   `INVALID READING` for anything not a finite number, a current below −20 A
   (a reversed or saturated transducer hides over-current), a module above 110%
   of full or a cell more than 0.8 V above full (either can never trip
   undervoltage), or a cell temperature below the DS18B20's −55 °C; and
   `INCOMPLETE DATA` when the counts of cells, temperature sensors or
   thermocouples differ from the wiring. Empty arrays keep their own names
   (`NO CELL DATA` and its siblings). The temperature parser also drops any
   value outside −55…125 °C, so the library's −127 "disconnected" value can
   never count as a fresh reading (the sketch already sends `ERR` for it). A
   NaN current sample is skipped by the coulomb count rather than freezing the
   SOC at 100%.

   **No simulated pack without the NI-DAQ.** `hardware_manager` used to
   substitute a healthy pack at rest (15 A, every cell at nominal + 0.5 V) when
   the DAQ failed to start, so a rig started without its DAQ drove the real bank
   while the overcurrent, undervoltage and cell trips watched made-up numbers.
   It now publishes zero readings with `hardware_status['ni_daq']` False. `ARM`
   is refused on them (the state stays `IDLE`, nothing to reset), and a DAQ lost
   after arming faults as `NI-DAQ OFFLINE` on the next packet and kills the load
   rather than waiting out `DAQ DATA STALE`. A failed read no longer ends the DAQ
   process, so temperatures keep reaching the GUI; it does not retry, so restart
   once the DAQ is back. Desk testing on simulated data is what the SIL dongle
   is for.

   **The logic loop never skips its body.** It previously did `continue` when the
   DAQ queue was empty, which jumped past GUI commands, every safety check, the
   resistor heartbeat and telemetry forwarding. A hung DAQ therefore paralysed
   the logic process: the operator's E-STOP sat unread in the queue and the GUI
   kept displaying `RUNNING`. The Arduino's own 2 s watchdog still shed the load,
   but nothing in software noticed or reported it. The loop now carries the last
   packet forward tagged with its age, and `DAQ DATA STALE` faults on it.

   **A failed write to the resistor controller is a lost link.** Every write
   goes through `write_resistor()`, which returns False rather than raising. A
   failed heartbeat, command or KILL, or a port that closes under the loop,
   closes the handle, re-arms discovery and, if ARMED or RUNNING, latches FAULT
   (`RESISTOR LINK LOST`). Heartbeat and command failures used to be swallowed,
   leaving the GUI showing a connected controller and RUNNING; the KILL writes
   were unguarded and could crash the process. Writes time out after 0.25 s
   rather than blocking. The loop sits in `try/finally`, so however it ends the
   load is killed and the port closed, and an unexpected error sends the GUI a
   final FAULT packet. **Limit:** the firmware does not answer `alive`, so a
   controller whose sketch hangs while still enumerated is not detected by the
   host. The chip's own watchdog timer restarts it within 2 s, which opens
   every relay, and the host then reads the start-up banner.

   **The measured current is checked against the ladder setting** (`LOAD
   MISMATCH`, `load_mismatch()`). Nothing compared the two before. Both wiring
   faults found so far were of that kind and were found with a meter: the
   command word sent in reverse, 202 A where 1.6 A was asked for, and the 8 and
   16 Ω relays crossed. A run faults when the current has not fitted the setting
   for three DAQ samples in a row, each taken after the setting had settled:
   more than 15% over, or more than 35% under, with a 2 A floor. The under side
   is the looser because wiring, contacts and hot elements all take current off;
   nothing innocent adds it. It also catches a contactor or relay that did not
   move, a controller that restarted, and a current sensor that stopped reading,
   which would otherwise leave the over-current and bank-power trips blind. It
   cannot see a wrong bank above about 16 Ω, where the difference is under 2 A,
   or a profile whose rows change faster than the 0.5 s settle. **The bench
   supplies do not agree with it**, the taps on one supply and the bank on
   another, so the sidebar's *Check load against ladder* box switches it off
   for those tests; SIL mode skips it, since the plant's current is a slider.

   **The host reads what the controller prints**, each pass, without waiting.
   Its start-up banner after the port has settled means it restarted:
   `CONTROLLER RESET` if armed or running. It used to drop every relay, come
   back two seconds later, take the next row's command and carry on with
   nothing on record. Its shed notice mid-run, when the host has sent no KILL,
   is its own watchdog: `CONTROLLER SHED LOAD`. A rejected line is reported,
   and while running the setting is sent again, once.

   **Outside a run the keep-alive is KILL, not `alive`.** `alive` feeds the
   Arduino's watchdog; KILL does not. When a run ended, one KILL went out and
   the rig went back to feeding the watchdog in `IDLE`, so had that one line
   been lost the contactor stayed closed at the last setting. Now `alive` is
   sent only while `RUNNING`, and every other state repeats KILL twice a
   second.
2. **Operator E-STOP** — GUI button, raises its own latched `estop_event`
   (`request_estop()`) rather than queueing a `STOP`, and the logic loop takes it
   ahead of every queued command. It used to go through
   `send_command_nonblocking()`, which makes room in a full queue by taking an
   old command out; a `multiprocessing.Queue` can refuse that for a moment after
   its own puts, so a STOP pressed into a full queue could be dropped while
   heartbeats carried on and the Arduino watchdog never fired. An Event has no
   capacity to run out of. Any `ARM`, `RUN` or `RESET` pressed before the stop
   is void (see the state machine above). **Esc** raises it from any of the
   rig's windows. Nothing modal opens during a run, because a modal dialog
   takes the button's clicks and Qt holds the shortcut back: Configure and Load
   Profile are refused while running, with a line in the window instead of a
   box. If the logic process goes quiet for 1.5 s the state banner says
   `NO TELEMETRY`; the window used to show its last state for ever.
3. **Arduino serial watchdog** — 2 s of host silence opens the main contactor
   and sheds all load, independent of the host entirely. Behind it, the chip's
   own watchdog timer restarts a sketch whose `loop()` has stopped turning.

Layer 3 is the backstop when layer 1 is stalled (see Open Questions).

### Bus-bar temperatures

One DS18B20 on each bus bar that joins one group of four parallel cells to the
next: eleven on a 12S module, the bar between S1 and S2 first. The cell sensors
sit on the cell tops; these sit on the current path itself, so a poor weld or a
bar running hot shows there first.

**None are fitted as shipped, and nothing changes until they are.**
`busbar_sensor_count` is 0 in `rig_config.json` and `BUSBAR_SENSOR_COUNT` is 0
in the temperature sketch: no line is sent, nothing is expected, nothing can
trip, and the thermal map draws the eleven places empty. To fit them: wire them
to pins 8 and 9 of the temperature Arduino, put their addresses in
`busbarSensors[]` in the sketch, set both counts to 11, flash, restart. Set the
two counts together: the host takes the board's `B,...` line only when it
carries exactly the configured number of readings.

Once fitted they are held to `busbar_max_temp_c` (80 °C as shipped, the RS50's
own cut-off; the cell-top sensors trip at 65 °C because a cell top reads cooler
than the cell, and a bus bar needs no such allowance). `BUSBAR OVERTEMP` applies
in every state. Once armed, a sensor that stops reporting is `BUSBAR SENSOR
FAULT` and a configured count with nothing arriving is `NO BUSBAR DATA`. Trips
name the bar and the groups it joins. The thermal map shows them in a row under
the cells, each between its two series groups; the main window gains a Max Bus
Bar card; the recording gains `BusBar_1_T` onwards.

## Testing

```bash
python -m pytest
```

About 700 unit tests (686 passing in November 2026), no hardware required.
`python -m pip install -r requirements.txt` installs what they and the rig need.
They cover the pure decision logic extracted from the control loop (trip
thresholds at and around their boundaries, FSM transition guards, coulomb
counting and SOC clamping, the thermal derate curve, road-load power), the real
logic loop and DAQ loop driven through their queues against fakes, the window's
widgets off-screen, and the sketches' tables read from their source.

Fourteen of them compile the resistor controller's sketch for the PC and skip
without `g++` or `clang++` on PATH. The pin table itself is checked without a
compiler, by reading it out of the sketch.

`tests/SIL_CHECKLIST.md` is the manual desk-test procedure for what unit tests
structurally cannot reach — process/queue plumbing, GUI responsiveness, timing,
and shutdown hygiene. Requires the SIL dongle; no battery or DAQ needed.

`tests/BENCH_CHECKLIST.md` tests every trip on the real hardware with bench
supplies in place of the module: a 30 V / 100 A supply on the power terminals
and a tap supply into a 12-resistor divider on the cell taps. Three things
support it, and all three work the same with a battery:

- **The trip log**, `logs/trip_log.csv`. Every stop and fault, one row each,
  with the reading and limit that tripped it and the milliseconds from the DAQ
  sample to the KILL. KILL now goes out before the trip is printed or logged.
  The GUI shows the reason in red beside the hardware pills (`TRIPPED: ...`).
- **The load override**, in the sidebar: hold a fixed resistance, or a fixed
  power at the bank (V²/R on the measured module voltage, re-solved every
  0.5 s), before or during a run. The run keeps the profile's timing and laps.
  Not saved; every session starts on the profile.
- **Bench profiles**, a `Resistance (ohm)` column in place of `Speed (mph)`
  (`profiles/Bench - *.csv`). A 0 Ω row refuses the profile, since the ladder
  would read it as its heaviest load.

The override and bench rows go through `steps_for_resistance()`, the same
floors and ratings as a lap row, so neither can command more load than a lap at
full demand.

**DC resistance, the first module-on measurement.** `profiles/Bench - DC
Resistance Pulses.csv` steps the module from rest into 12 s pulses at 2.5, 1.25
and 0.75 Ω (about 20, 40 and 67 A), then takes about a tenth of its charge at
1.5 Ω. Each pulse uses two or more banks, so no bank rating moves it at either
bank setting. Record 8 laps from full, then `python tools/dcir_from_log.py`
reads the newest recording and prints the module's and each cell's resistance at
1 s and 10 s, down the charge, against `cell_dc_milliohm`. The 0.75 Ω pulse
needs `max_amps` above 67 A; at the bench-supply 60 A it repeats the 1.25 Ω one.
Mooch measured 6.5 mΩ per cell, and `cell_dc_milliohm` now carries it; the
1 s reading should land near that, and the 10 s reading higher (about 8 mΩ). The
figure matters beyond the rig: the 2027 pack study's cell heat scales with it,
1.6× from the old 4.0 mΩ estimate to 6.5 mΩ. A module that reads well above
6.5 mΩ per cell has a weak cell group or a poor collector-plate weld.

**Calibrate the taps before trusting a per-cell figure.** Each tap has its own
10:1 divider and none is exactly 10:1. A cell voltage is the difference of two
taps, so the dividers' errors grow up the stack: with 1% parts, taps 11 and 12
near 46 V and 50 V can put cell 12 almost a volt out, and its measured
resistance a fifth out, however large the pulse. `python tools/tap_calibrate.py`
reads every tap, takes a multimeter reading for each, and prints the
`voltage_tap_gains` list for `rig_config.json`. Empty, as shipped, every tap
takes the 11.0 multiplier alone.

**The recording** (`logs/telemetry_*.csv`) stamps each row with the DAQ's own
sample time, writes every sample once, and reaches the disk row by row. After
the columns it always had it carries the lap and profile row, `Asked_Res`
beside `Target_Res`, `Expected_A` (what the step sent should draw), remaining
Ah, the packet's age, the resistor bank temperatures and any bus-bar
temperatures.

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
| Current limit 182 A, temp limit 65 °C (P45B pack) | Both exceeded datasheet ratings (180 A / 60 °C). E-STOP only fired at 187 A = 46.75 A/cell, 3.89 % over rated |

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
20.0 Ah nameplate figure. Two problems: it uses *typical* capacity (5000 mAh)
rather than *minimum* (4950 mAh, → 19.8 Ah), overstating by up to 1 %; and it
ignores rate dependence, though the ~200 A the ladder can draw is a 10 C
discharge where real deliverable capacity is meaningfully lower. Quantifying the
second needs a discharge-rate curve. The RS50 listing has none; Mooch's test has
curves at 5, 20, 40 and 70 A, not yet folded into the SOC estimate.

**2. The regen branch of the power model is currently inert.**
`compute_target_resistance()` maps any `req_power <= 0` to `MAX_RESISTANCE`, so
braking power of −51.7 kW and −52.2 kW command the same 63.75 Ω — the bank is
purely dissipative and cannot recover energy. The braking efficiency correction
is still physically correct and matters if computed power is ever used for
energy prediction, but it does not change bank behaviour today. Coulomb counting
uses measured current, not this figure, so it does not propagate there either.

**3. The November 2026 firmware is in the repo, not on the boards.** Both
sketches changed: the resistor controller gained the hardware watchdog and
refuses `00000000`, and the temperature sketch gained the bus-bar line (off
until `BUSBAR_SENSOR_COUNT` is set). They compile for the Uno, Nano and Mega
but have not been flashed or run. The host works with the older resistor
sketch too: it never sends `00000000`, and KILL as the idle keep-alive needs
only that KILL does not feed the watchdog, which has been so since October.
After flashing, run the bench check in `arduino/README.md`.

(Port discovery used to block the logic loop, so an E-STOP could not be acted on
during a scan. It has run on its own thread since October 2026, and the SIL
dongle is now looked for before the logic process starts, so nothing sweeps the
COM ports at the same time as anything else.)

**4. The `SIL_KEY` dongle firmware is not in this repo.** `main_v2.py` probes for
a device replying `SIL_KEY` to unlock software-in-the-loop mode, and no such
sketch exists on the development machine. The other three sketches now live in
`arduino/` — see that folder's README. If the dongle exists, its firmware is
elsewhere and should join them.

**5. No cross-validation between threshold spinboxes.** Nothing prevents setting
`V Warn` below `V Crit`, or `Derate Start` above `Max Temp`. The derate case now
fails safe rather than crashing, but the UI still accepts nonsense combinations.

**6. Tested on bench supplies, not yet on a module.** Every trip was fired on
the real rig with supplies in place of the module in October 2026
(`tests/BENCH_CHECKLIST.md`, `logs/trip_log.csv`). No run on real cells has
been made. The November 2026 changes (the load check, the controller read-back,
KILL as the idle keep-alive, row timing, the recording, per-tap gains and the
bus-bar sensors) are unit-tested against fakes and have not been run on the
rig. Run the bench checklist again before the first module, with the load
check's two new steps.

**7. Things only the hardware can answer.** Whether the relay MOSFET gates have
pull-downs, so the relays stay released while the controller is in reset.
Whether the module is fused. What tolerance the tap dividers' resistors are,
which decides how much `tools/tap_calibrate.py` is needed. Which board the
resistor controller is: a Nano on its old bootloader restarts over and over
after a hardware-watchdog restart until power-cycled (see `arduino/README.md`).
