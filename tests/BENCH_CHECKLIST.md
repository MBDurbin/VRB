# Bench Checklist — trips on supplies, no battery

The hardware counterpart of [SIL_CHECKLIST.md](SIL_CHECKLIST.md). It runs the
real rig (NI-DAQ, both Arduinos, contactor, relays, bank, fan) with bench
supplies standing in for the module, and checks that every trip fires at the
value it should and that the load really comes off. Unit tests cover the
decision logic and SIL covers the plumbing; this is the only place the relays,
the contactor, the sensors and the watchdog are tested together.

Every "Expected" line below was computed from the shipped `rig_config.json`
with the trip code itself (`evaluate_safety`, `steps_for_resistance`). If the
config has changed, recompute before trusting a number here.

---

## What you need

| Item | Why |
|---|---|
| 30 V / 100 A supply ("current supply") | Stands in for the module's power terminals. Drives current through the bank |
| 80 V supply ("tap supply") into a 12-resistor divider | Stands in for the module's 12 cell taps. Run it at **44.0 V**, not 80 (see below) |
| A second low-current output, or the current supply on its minimum current limit | Drives one tap on its own for the per-cell test |
| A DS18B20 harness on the temperature Arduino (the bench harness is fine) | All 48 sensors must report or the rig faults `TEMP SENSOR FAULT` the moment it arms |
| Heat gun | Resistor thermocouples only (section E) |
| **DC clamp meter** | Checks the Hanalem reading against the real current. Without it nothing says the current trip fires at the current you think |
| **Multimeter** | Checks each tap against the GUI cell readings |
| **Thermocouple reader** | Checks the bank thermocouples against something independent while the heat gun is on them |
| 2-3 turns of 100 A cable through the Hanalem's window | Makes the sensor read 2-3x what flows. The only way to reach the 280 A trip on a 100 A supply (section C) |
| 0.1 A fuse in the extra tap lead | A slip with a 100 A supply on a sense lead burns the board, not just the wire |
| Reverse diode across the current supply's output, and a fuse or breaker in series | Opening the contactor at 100 A into wirewound resistors kicks back into the supply. Check its manual for built-in reverse protection |
| A way to cut the current supply within reach (its output switch) | You are testing the software's shutoffs, so it cannot be your only one |
| Fan plugged in and running for every test that puts current through the bank | As for any run. Section E is the one exception, with no current flowing |
| Fire extinguisher and safety glasses | Contactor and relay arcing at 100 A DC |

**Why 44 V on the tap supply, not 80:** once armed, a module reading above
55.4 V (110% of 50.4 V) or a cell above 5.0 V is refused as `INVALID READING`.
80 V across 12 equal resistors is 6.7 V a cell. 44.0 V gives 3.67 V a cell,
mid-charge.

**The two supplies do not agree, and that is fine.** The software sees the tap
supply's 44 V as the module voltage while the bank actually runs on 30 V. So it
computes power about 1.5x higher than the bank really dissipates. That is the
safe direction: the bank-power trips fire early, never late.

## Wiring and pre-power checks

| # | Check | Expected |
|---|---|---|
| W0 | Chassis connected: `python "tools/discover DAQ.py"`. Compare each module's model and channel names with `daq` in `rig_config.json`: `current_channel`, the 12 `voltage_channels` in cell order, and `resistor_tc_channels` in bank order | Every configured channel exists and is the one wired to that signal. Terminal numbers are not channel numbers: a 9215's terminals pair up as AI+/AI−, and the 9205's D-SUB skips around (docs/hardware_topology.md) |
| W0a | Tap supply on, nothing else. Measure the top tap with the multimeter, then `python tools/tap_map_check.py <that voltage>` | `voltage_channels in rig_config.json matches the wiring.` Otherwise it says which tap is on which input, or which is missing, and prints the list to paste in |
| W1 | Divider resistors ≤ 1 kΩ each, matched (1%) | The tap board's own 10:1 dividers do not load the string |
| W2 | With nothing powered, meter between the tap harness's top lead and the bank's power positive | **Open circuit.** If they are joined, the tap supply back-feeds the current supply |
| W3 | Tap supply negative, current supply negative and the board ground share **one** point, at the bank's negative terminal | Taps referenced the way the module's are |
| W4 | Current-limit the tap supply to ~50 mA, and any supply on a tap to its minimum | — |
| W5 | Current supply set to 30 V, current limit 100 A, output **off** | — |
| W6 | `python -m pytest` | All pass |
| W7 | `python tools/bench_relay_check.py`, `walk`, current supply off | Each bank switches the resistance the table says |

---

## A. Bring-up, no load

Start `python main_v2.py`, current supply off, tap supply on at 44.0 V.

| # | Step | Expected |
|---|---|---|
| A1 | Wait for the pills | NI DAQ, TEMP SENSOR, RESISTOR CTRL all online; state `IDLE`; no `WILL NOT ARM` line |
| A2 | Read each cell in the sidebar against the multimeter on the divider | Every cell 3.67 V ± 0.05; module 44.0 V |
| A3 | Current card with no current flowing | 0 A ± 1 A. Note the offset |
| A4 | Bank thermocouples (Resistor Map) against the thermocouple reader | Within a few °C of room temperature and of each other |
| A5 | Thermal Map | All 48 cells read, none blank |
| A6 | Press **ARM** | `ARMED`, and it stays there for 10 s. Then **E-STOP**, **RESET** |

## B. Load under control, no trips

Fan on, current supply output on.

| # | Step | Expected |
|---|---|---|
| B1 | **Load Profile** → `Bench - Resistance Steps.csv`, ARM, RUN | Bank steps 63.75 → 8 → 4 → 2 → 1 → 0.5 → 0.25 Ω, 15 s each. Clamp meter reads ~3.8, 7.5, 15, 30, 60, then 100 A (supply in current limit). GUI current matches the clamp within a few % |
| B2 | Same run | **No trip.** Ends in `IDLE` with `All 1 laps completed`. Nothing new in `logs/trip_log.csv` |
| B3 | Sidebar **Load override** → Fixed resistance, 2 Ω, Enter. Then ARM, RUN (any profile) | Status line amber `Holding 2.00 Ω, ladder at 2.00 Ω`; clamp ~15 A |
| B4 | Mid-run, change it to 1 Ω | Within half a second: ladder 1.00 Ω, clamp ~30 A |
| B5 | Fixed power, 1.0 kW | Ladder 44² / 1000 = 1.94 → 2.00 Ω, the nearest step; 15 A. The Power card shows 44 V × 15 A = 0.66 kW, while the bank really dissipates 30 V × 15 A = 0.45 kW. On a battery the two agree |
| B6 | Back to **Lap profile** | The profile takes the load back at once |

## C. Current and bank-power trips

These need the sensor to read more than flows. The software holds the real
current under **Max current** by choosing the resistance, so the trip can only
be reached by a fault it cannot see: here, extra turns through the sensor. A
trip fires on the GUI's **reading**, so watch the Current card, not the
supply's display.

| # | Setup | Step | Expected |
|---|---|---|---|
| C1 | 3 turns. Tap supply **38.0 V**. Current supply limit 80 A. Override: Fixed resistance 0.25 Ω | ARM, RUN. Raise the supply's current limit 2 A at a time | Card reads ~3x the clamp. Trips once the card reaches 280 A (limit ~94 A): `OVERCURRENT ALARM!`, `Current 28x.x A (trip 280.0 A)`. Contactor opens, clamp to 0. 38 V keeps V×I under the 11.2 kW bank trip until then |
| C2 | 1 turn, tap supply 44.0 V, sidebar **Max current** 60 A (trip 65 A), supply limit 100 A | RUN with override 0.25 Ω | Ladder held at **0.75 Ω**, not 0.25: the current floor. ~40 A, no trip. This is the software keeping current under the limit by itself |
| C3 | C2 with 2 turns | RUN | `OVERCURRENT`, `Current 80.0 A (trip 65.0 A)` |
| C4 | 3 turns, tap supply **40.0 V**, tick **Bank at rated power only**, override 0.25 Ω, supply limit 60 A | RUN, raise the limit 1 A at a time | `BANK OVERPOWER` once V×I passes 8 kW (limit ~67 A): `Ladder at 80xx W (limit 8000 W)` |
| C5 | 2 turns, 40.0 V, rated power ticked, override **0.5 Ω** (bank 2 alone), supply limit 100 A | RUN | After 0.5 s settle: `BANK OVERPOWER`, `Bank 2 at 4800 W (limit 4000 W)`. Per-bank check, total still under 8 kW |
| C6 | Over-current is checked in every state | In `IDLE`, with Max current 60 A, run the current supply in current limit into a shorting cable through the sensor, raising from 50 A | `OVERCURRENT` at a 65 A reading, from `IDLE` |

For each, `logs/trip_log.csv` gets a row with the reading, the ladder setting
and `Sample_To_Kill_ms` (see "Reading the trip log" below).

## D. Voltage trips

`ARMED` is enough: these are checked once armed, with no current needed.

| # | Step | Expected |
|---|---|---|
| D1 | Lower the tap supply from 44.0 V in 0.1 V steps | `UNDERVOLTAGE` at 36.0 V: `Module at 36.0x V (trip 36.00 V)`. Cells are still 3.0 V, so the module trips, not a cell |
| D2 | RESET, ARM at 44 V. Raise the tap supply to 56 V | `INVALID READING`: `module voltage 56.0 V is above 55.4 V` |
| D3 | 44 V. Set the extra output to 29.3 V (tap 8's own voltage) with its current limit at minimum and a 0.1 A fuse in the lead. Move tap 8's lead from the divider to it. ARM, then lower it in 0.05 V steps | `CELL UNDERVOLTAGE` at ~28.36 V: `Cell 8 at 2.69 V (trip 2.70 V)`. Cell 9 rises to ~4.64 V as you go; that is expected, and stays under 5.0 V |
| D4 | Lead back on the divider. ARM, then unplug tap 5's lead | `INVALID READING`, `cell(s) 6 above 5.00 V`. An open lead reads 0 V, so the cell above it reads the whole stack. It faults, but as an implausible reading, not `CELL SENSE FAULT` |
| D5 | ARM, unplug the top lead (tap 12) | `UNDERVOLTAGE`, `Module at 0.00 V` |

## E. Temperature trips

**Never put the heat gun on a battery module.** On the bench harness it is fine,
but a DS18B20 is rated to 125 °C and its plastic package melts well below a heat
gun's air temperature: low setting, from a distance, with the thermocouple
reader beside the sensor.

| # | Step | Expected |
|---|---|---|
| E1 | Bench harness, shipped 80 °C trip. Warm one DS18B20 with the heat gun as above | `OVERTEMP` as the reader passes 80 °C, from `IDLE` too: `Hottest cell at 80.x C (trip 80.0 C)` |
| E1a | Gentler alternative: sidebar **Max temp** 5 °C above the hottest sensor, and warm one between finger and thumb | Same trip at the lowered value |
| E2 | ARM, then unplug one DS18B20's data lead | `TEMP SENSOR FAULT` within ~3 s, naming the bus and sensor |
| E3 | ARM, then unplug the temperature Arduino's USB | `TEMP LINK LOST` |
| E4 | Fan off, current supply off, `IDLE`. Heat gun on bank 1's thermocouple, thermocouple reader beside it. Keep the airflow off the cDAQ module (its cold junction), relays, PCB and wiring | `RESISTOR OVERTEMP`, `Bank 1 at 225 C (trip 225 C)`, from `IDLE`. Reader within a few °C of the trip. Repeat for banks 2-4 (bank 4 trips at 150 °C) |
| E4a | Gentler alternative: lower `resistor_max_temp_c` in `rig_config.json` to 60 for each bank, restart | Same trips at 60 °C |
| E5 | ARM, then unplug one thermocouple at the NI module | `RESISTOR TC FAULT` within ~3 s, naming the bank |

## F. Lost links and the watchdog

Each with the fan on and `Bench - Hold 1 ohm.csv` running (30 A). Time how long
the clamp takes to reach 0. A phone video of the clamp is enough.

| # | Step | Expected |
|---|---|---|
| F1 | Unplug the NI-DAQ's USB | `NI-DAQ OFFLINE` on the next packet; contactor opens |
| F2 | Unplug the resistor Arduino's USB | `RESISTOR LINK LOST`. Contactor opens at once if the Arduino is USB-powered, or within 2 s on its watchdog if not. Note which |
| F3 | Task Manager: end every `python.exe` | Contactor opens within 2 s: the Arduino's watchdog, with no host at all. No trip-log row, because nothing is left to write it |
| F4 | Plug everything back, restart, RUN again | Comes back up normally |

## G. Operator stops

| # | Step | Expected |
|---|---|---|
| G1 | **E-STOP** mid-run (Hold 1 Ω) | `FAULT` at once; clamp to 0; trip log `E-STOP`, `RUNNING`, `yes` |
| G2 | With the rig in `FAULT`, press **RESET** | `IDLE`; the red `TRIPPED:` line clears |

## H. Endurance

| # | Step | Expected |
|---|---|---|
| H1 | Lap profile, 10 laps, fan on | No trip, no `NOT IN FORCE` line, GUI never freezes, watchdog never fires mid-run |
| H2 | `Bench - Hold 1 ohm.csv` (5 min) with override Fixed resistance 0.25 Ω: 100 A, 2.5 kW in bank 1 | Bank 1 thermocouple climbs and levels off well under 225 °C. If it does not level, stop: the fan is not doing its job |

---

## Reading the trip log

`logs/trip_log.csv`, one row per stop or fault, appended across sessions:

| Column | Meaning |
|---|---|
| `State` | What the rig was doing when it tripped |
| `Reason`, `Detail` | The trip, and the reading and limit it crossed |
| `Amps` … `Resistor_Temps_C` | The packet that tripped it |
| `Ladder_Ohm` | The setting in circuit, if running |
| `Sample_To_Kill_ms` | DAQ sample to KILL leaving the host: the queue, the loop and the write. The DAQ samples every 100 ms, so expect tens of ms; look into anything over a few hundred. It does not include the sensor's response or the contactor opening; time those with the clamp |
| `Kill_Sent` | `yes`, or `NO` when the link was already gone (the watchdog then sheds the load) |

Close it in Excel before the next trip: Excel locks the file, and the row is
skipped (with a console warning) rather than allowed to stall the rig.

## Afterwards

Sidebar changes are saved to `rig_config.json` when the window closes, and a
threshold edit there switches off pack derivation. Before a battery goes back
on:

1. `git diff rig_config.json`. If the only changes are this test's, `git checkout -- rig_config.json`.
2. Load override back to **Lap profile**. It is not saved; a restart also resets it.
3. Turns off the Hanalem, tap leads back on the module harness, the module's own taps checked against the multimeter again (A2).
