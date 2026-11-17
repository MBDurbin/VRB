# VRB hardware topology

What the rig physically is, for anyone trying to work out what the software is
talking to. Extracted from the project work log; the log itself is not in this
repo.

The **VRB** (Variable Resistor Bank) is a binary-weighted resistor ladder that
loads **one battery module** and dissipates its energy as heat, following a
recorded lap profile. It is designed to test one module at a time, not the whole
car's battery.

---

## Battery

The car's battery is **9 modules in series**. Each module is **12S4P** — 12 cells
in series, 4 in parallel — of **Reliance RS50** (21700, 5000 mAh, 70 A). The
modules were first built with Molicel INR-21700-P45B, obtained through a Tesla
scholarship; the RS50 replaced it.

**The bench loads one module.** See the Battery topology section of
`PROJECT_CONTEXT.md` for why that distinction governs the physics, and
`datasheets/README.md` for where each cell figure comes from.

| Per module | Value |
|---|---|
| Continuous discharge current | 280 A (70 A × 4P) until the cells reach 80 °C; 160 A (40 A × 4P) for a whole discharge |
| Capacity | 20.0 Ah (19.8 Ah minimum) |
| Charge voltage | 50.4 V |
| Nominal voltage | 43.2 V |
| Discharge cutoff | 30.0 V (2.5 V/cell; not on the RS50 listing — confirm) |
| Internal resistance | 0.0195 Ω (6.5 mΩ/cell, Mooch's measurement, October 2025) |
| Discharge temperature limit | 80 °C |

---

## Resistor bank

Eight banks give the ladder its binary resistance. **Rated dissipation is 8 kW**
(bank 1's four 2 kW elements), and every bank has its own rating below. The
software holds the ladder and each bank to 140% of these by default, or 100%
with *Bank at rated power only* ticked — see "The bank's power ratings" below.

| Bank | Resistance | Built from | Part | Capacity |
|---|---|---|---|---|
| 1 | 0.25 Ω | 4 × 1 Ω in parallel | TE2000B1R0J (1 Ω, 2 kW) | 8000 W |
| 2 | 0.5 Ω | 2 × 1 Ω in parallel | TE2000B1R0J | 4000 W |
| 3 | 1 Ω | 1 × 1 Ω | TE2000B1R0J | 2000 W |
| 4 | 2 Ω | 2 × 4 Ω in parallel | Uxcell 500 W 4 Ω, ID 1031013 | 1000 W |
| 5 | 4 Ω | 1 × 4 Ω | Uxcell 500 W 4 Ω, ID 1031013 | 500 W |
| 6 | 8 Ω | 1 × 8 Ω | Ohmite HS300 8R F (Mouser 284-HS300-8.0F) | 300 W |
| 7 | 16 Ω | 1 × 16 Ω | Ohmite HS200 16R F (Mouser 284-HS200-16F) | 200 W |
| 8 | 32 Ω | 2 × 16 Ω in series | Ohmite HS200 16R F | 400 W |

Total ladder resistance 63.75 Ω, smallest step 0.25 Ω — matching
`MAX_RESISTANCE` and `RESISTOR_RESOLUTION` in `control_logic.py`.

The TE parts are covered by
`datasheets/TE-Series_High-Power-Wirewound_9-1773453-2_RevE.pdf`. Decoding
`TE2000B1R0J`: TE series, 2000 W, B = with bracket, 1R0 = 1.0 Ω, J = ±5%.

### Bank 1 carries the peak duty

Minimum resistance means maximum current, and at minimum resistance only bank 1
is in circuit — so it absorbs the entire load. Its four parallel elements share
the current, and each sees the whole module voltage:

| Module voltage | Bank current | Per resistor | Per resistor power | vs 2000 W rating | vs 44.7 V RCWV |
|---|---|---|---|---|---|
| 40.0 V | 160 A | 40.0 A | 1600 W | −20% | −10.6% |
| 44.7 V | 179 A | 44.7 A | 2000 W | 0% | 0% |
| **50.4 V** (full RS50 module) | **202 A** | 50.4 A | **2540 W** | **+27%** | **+12.7%** |

**The ladder, not the cells, caps the current.** With the RS50's 280 A rating, the
0.25 Ω bottom step limits a full module to about 202 A, so the over-current trip
is a backstop for a fault (a shorted ladder, say) rather than a limit the rig
reaches in use. What bounds the load is the bank: at a full module bank 1 runs at
127% of its elements' rating, which is why the default bank setting is 140% (see
"The bank's power ratings" below), why it leans on the thermocouple trips, and
why the fan is not optional. RCWV = √(P × R) is the voltage at rated power, so
it is exceeded by the same overload rather than being a separate limit.

---

## Cooling — read this before running

The bank is arranged as a **tube bank, like a heat exchanger**. The four 1 Ω
elements of bank 1 sit side by side on their own level, spaced about one
resistor diameter apart. Banks 2 and 3 sit beneath, offset to align with the
gaps above so the array behaves as a staggered tube bank in cross-flow. The
remaining resistors mount to four sideways aluminium flat bars, positioned to
add turbulence while keeping the air temperature rise low.

Airflow comes from a **24 in wall-mounted shutter exhaust fan, 3500 CFM**,
aluminium blades, 1500 RPM, shutters removed, mounted below the structure and
pointing up.

> **The fan has one setting: ON. It must be plugged into the wall.
> Never run the VRB without the fan blowing.**

`analysis/CFM Calculator.py` is the analysis behind this arrangement —
it models the resistors as a staggered tube bank and sizes the airflow. Its
60 mm cylinder diameter matches the 1000–2500 W parts in the TE dimensions
table.

**Nothing in software checks the fan.** See Gaps below.

---

## Control system

An Arduino acts as slave to the VRB computer application, which is master. It
drives a control board of **9 normally-open relays**, switched at 12 V through
MOSFETs. The MOSFETs sit on a PCB with the control Arduino, keeping the wiring
tidy.

The command is a binary word representing **4 × the target resistance in ohms**
(equivalently, the number of 0.25 Ω steps), **most significant bank first**: 32 Ω
is `10000000`, 0.25 Ω is `00000001`. A `1` opens that bank's bypass relay and
puts the bank in circuit. `00000000` would bypass every bank at once, a dead
short, so the firmware refuses it and moves nothing. (It used to convert it to
`00000001`, bank 1 alone: the heaviest load the ladder has. A board still on a
sketch from before November 2026 does that.) The host never sends it.

| Character | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|---|
| Bank | 8 | 7 | 6 | 5 | 4 | 3 | 2 | 1 |
| Resistance | 32 Ω | 16 Ω | 8 Ω | 4 Ω | 2 Ω | 1 Ω | 0.5 Ω | 0.25 Ω |
| Arduino pin | 12 | **10** | **11** | 9 | 8 | 7 | 6 | 5 |

The main contactor is pin 4. In software this table is `COMMAND_BANKS` and
`BANK_RELAY_PIN` in `control_logic.py`; `tests/test_firmware_bank_map.py`
compiles the firmware and checks every one of the 255 settings against it.

The host used to send the word reversed, least significant bank first, so 32 Ω
arrived as `00000001` and switched in bank 1 alone: about 202 A / 10 kW at
50.4 V instead of 1.6 A / 79 W.

**Which pin bypasses which bank is the relay board's wiring, and only a meter
proves it**: run `python tools/bench_relay_check.py`, `walk`, with the battery
disconnected, before the first run and after any rewiring. The October 2026
walk found pin 10 on the 16 Ω bank and pin 11 on the 8 Ω bank, the other way
round from the binary order, so the firmware and `BANK_RELAY_PIN` swap them.

Outside a run the host sends `KILL` twice a second, and `alive` only while
running. It also reads what the controller prints: a restart, the controller's
own watchdog firing mid-run, and a rejected line are all acted on. And it
checks the current it measures against the resistance it commanded, which is
what would have caught both the reversed word and the crossed relays without a
meter. See Safety layers in `PROJECT_CONTEXT.md`.

Firmware: `arduino/resistor_bank_controller/`.

---

## Temperature acquisition

DS18B20 sensors on OneWire, each wired with ground, signal and 5 V. **A larger
than usual pull-up resistor is used**, because several buses feeding one Arduino
add up to significant capacitance.

Sensors are split across **6 buses of 8**, pairing two series groups per bus
(groups 1–2 on bus 1, 3–4 on bus 2, and so on). The split exists to improve
signal integrity and keep bus capacitance down. The Arduino reads the buses in
a state machine and sends every sensor on every sweep, about one sweep a second,
so the host can tell a sensor that has stopped from one that has not changed.

Firmware: `arduino/temperature_sensor_array/`.

### Sensor ROM addresses

One sensor per cell: rows **A–D** are the 4 parallel cells, columns **S1–S12**
the 12 series groups.

**These are transcribed from the firmware, which is authoritative.** The work log
carries a copy with two stale entries — B1 and B12 — that do not match what is
flashed. If the two ever disagree again, the sketch wins; it is what actually
reads the bus, and a wrong address there shows up immediately as `ERR`.

| Row | S1 | S2 | S3 | S4 | S5 | S6 | S7 | S8 | S9 | S10 | S11 | S12 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **A** | 28DB84FB0F00004C | 287A9BFB0F000034 | 289799FB0F0000D4 | 285E96FB0F0000B2 | 287E2FFB0F0000B8 | 28004EFB0F0000EF | 289176FB0F0000D3 | 28A9F6FB0F000054 | 28F805FC0F000023 | 284B89FB0F000011 | 28D002FB0F0000E3 | 288A8CFB0F00006B |
| **B** | 283BFDFB0F0000FB | 28EF2EFB0F0000F3 | 28ACF3FA0F0000E2 | 2869CFFB0F0000BC | 289E04FC0F00009F | 286BAFFB0F0000C3 | 2835DBFB0F00008C | 28EBD3FB0F000065 | 28B8C6FB0F0000BC | 282806FC0F0000A9 | 28EAEDFA0F0000FB | 28F8D8FB0F000017 |
| **C** | 28DA4AFB0F0000FB | 284F46FB0F000080 | 28BFCAFB0F000018 | 2816CFFA0F00008F | 28E450FB0F0000C4 | 28C202FC0F000050 | 2869D9FB0F00005C | 284052FB0F0000C7 | 284D9DFA0F00004F | 28AA3EFB0F000011 | 2897C9FB0F000041 | 286501FB0F000041 |
| **D** | 28E0BEFB0F000060 | 28FB97FB0F0000C8 | 28A6CEFB0F0000CA | 287EE1FB0F000038 | 289FAAFB0F0000BF | 283941FB0F0000FB | 2824BCFA0F00002F | 28C796FB0F000095 | 2829C1FA0F0000E4 | 287042FB0F000056 | 28EB06FB0F0000E9 | 280425FB0F000052 |

Use `arduino/ds18b20_address_scanner/` to read the address off a replacement
sensor, then update the sketch and this table together.

### Bus-bar sensors

A place for one more DS18B20 on each **bus bar between two series groups**:
eleven on a 12S module, numbered from the bar between S1 and S2 to the bar
between S11 and S12. The cell sensors above sit on the cell tops; a bus bar is
the current path itself and is welded to the cell terminals, so a poor weld or a
bar running hot shows there first.

**None are fitted yet.** `busbar_sensor_count` in `rig_config.json` and
`BUSBAR_SENSOR_COUNT` in the sketch are both 0, and with both at 0 nothing in
the rig expects them. To fit them:

1. Wire them to pin 8 of the temperature Arduino, and to pin 9 as well if one
   bus is too long. The cell sensors are split eight to a bus to keep each bus's
   capacitance down; eleven on one is more than that.
2. Read each address with the scanner and put it in `busbarSensors[]` in the
   sketch, in bus-bar order, with the bus it is on (0 for pin 8, 1 for pin 9).
3. Set `BUSBAR_SENSOR_COUNT` to 11 and flash.
4. Set `"busbar_sensor_count": 11` in `rig_config.json` and restart the rig.

Do 3 and 4 together. The board sends them as `B,<t1>,...,<t11>` after the six
cell buses, and the host takes that line only when it carries exactly the
configured number of readings.

| Bus bar | Between | Bus (pin) | ROM address |
|---|---|---|---|
| 1 | S1 and S2 | | not fitted |
| 2 | S2 and S3 | | not fitted |
| 3 | S3 and S4 | | not fitted |
| 4 | S4 and S5 | | not fitted |
| 5 | S5 and S6 | | not fitted |
| 6 | S6 and S7 | | not fitted |
| 7 | S7 and S8 | | not fitted |
| 8 | S8 and S9 | | not fitted |
| 9 | S9 and S10 | | not fitted |
| 10 | S10 and S11 | | not fitted |
| 11 | S11 and S12 | | not fitted |

Fill this in with the sketch, as for the cell table.

---

## Current and voltage acquisition

**Current**: a Hanalem 400 A sensor feeding the NI-DAQ. Its ±4 V for ±400 A
output is where `DaqConfig.current_amps_per_volt = 100.0` comes from.

**Voltage**: the board housing the Arduino also steps the module tap voltages
down and feeds them to the NI-DAQ, which passes them to the application. The
divider ratio is `DaqConfig.voltage_multiplier = 11.0` (a 10:1 divider).

Each tap has its own divider, and none is exactly 10:1. A cell voltage is the
difference of two taps, so those errors grow up the stack: with 1% resistors,
taps 11 and 12 near 46 V and 50 V can put cell 12 almost a volt out.
`DaqConfig.voltage_tap_gains` holds one correction per tap, and
`python tools/tap_calibrate.py` measures them against a multimeter and prints
the list. **The tolerance of the divider resistors is not recorded anywhere**,
which is what decides how much this matters; until the taps are calibrated,
treat per-cell voltages near the top of the stack as good to a few tenths of a
volt.

**The chassis** (cDAQ-9178, read with `tools/discover DAQ.py` in October 2026):

| Slot | Module | Carries |
|---|---|---|
| 6 | NI 9212 + TB-9212 mini-TC | Bank thermocouples on jacks TC0, TC2, TC4, TC6 (`ai0`, `ai2`, `ai4`, `ai6`). The odd jacks are empty and read ~2284 °C, the module's open-thermocouple value |
| 7 | NI 9215 (differential only) | Current sensor on `ai0`: signal on screw terminal 0 (AI0+), sensor 0 V on terminal 1 (AI0−) and terminal 9 (COM) |
| 8 | NI 9205 (DSUB) + NI 9923 | The 12 cell taps, single-ended (RSE) against COM |

**The 9205's D-SUB does not run in channel order**, and the NI 9923's terminal
numbers are the D-SUB pin numbers (NI 9205 Getting Started Guide, Figure 1):

| Terminals | Signal |
|---|---|
| 1–8 | AI0–AI7 |
| 9 | DO0, a digital output: never a tap |
| 10, 29 | COM: the tap ground, and the reference every RSE reading is measured against |
| 11–18 | AI16–AI23 |
| 19 | AISENSE |
| 20–27 | AI8–AI15 |
| 28 | PFI0 |
| 30–37 | AI24–AI31 |

The config expects ground on terminal 10 (COM), taps 1–7 on terminals 2–8,
tap 8 on 20, tap 9 on 21, and taps 10–12 on 11–13. After any rewiring, power
the taps and run `python tools/tap_map_check.py <top-of-stack volts>`: it reads
every input and reports which tap each one carries, and whether
`voltage_channels` matches.

---

## Gaps between the hardware and the software

Recorded here because none of them are visible from the code.

**1. No fan interlock.** The fan is plugged into a wall socket and has no
feedback path. Nothing in software confirms it is running, so a run with the fan
unplugged has no protection at all — the cell-side trips would not notice a
resistor bank cooking. This is currently a procedural control only: *never run
without the fan*.

**2. Resistor thermal protection — banks 1–4 only.** One thermocouple per bank
on banks 1–4, read by an NI 9212 in slot 6 through its TB-9212 mini-TC block,
on jacks TC0 (bank 1), TC2 (bank 2), TC4 (bank 3) and TC6 (bank 4). The channels
are `resistor_tc_channels` in `rig_config.json`: `cDAQ1Mod6/ai0`, `ai2`, `ai4`,
`ai6`, bank 1 first, Type K. They are
read on their own thread so a slow thermocouple module cannot slow the voltage
and current loop. Per-bank trips are `resistor_max_temp_c` in the `limits`
section. Each bank is held to its own trip, which applies in every state and
logs `RESISTOR OVERTEMP` with the bank named. Once armed, a thermocouple with no
valid reading for 3 s (open, off-scale, or a failed read) faults as
`RESISTOR TC FAULT`. No thermocouple data at all faults as
`NO RESISTOR TEMP DATA`. The **Resistor Map** button shows the banks laid out as
built.

Default trips:
- **Banks 1–3: 225 °C.** TE's **155 °C** is an *ambient* limit on its derating
  curve, not an element temperature; at full load the element runs ~370 °C above
  ambient (datasheet p.3). The element limit is conventionally **275 °C**, where
  that curve reaches zero load. 225 °C leaves 50 K for the hot side of an element
  and a thermocouple that sits off the hottest point.
- **Bank 4: 150 °C, a placeholder.** There is no datasheet for the Uxcell part
  in this repo, so it is deliberately low. Replace it once the rating is known.

Banks 5–8 are not instrumented. Not recorded anywhere: which middle-row tube is
bank 3, and where banks 4–8 sit on the flat bars. The map draws both in bank
order (`MIDDLE_ROW_BANKS` and `FLAT_BAR_BANKS` in `gui_layout.py`).

**3. The bank's power ratings — now enforced in software, per bank.** The
current limit derives from the cells alone. With the Reliance RS50 cells (70 A
each) it works out to a 275 A operating limit and a 280 A trip, about 14 kW at
50 V, so the cell limit can no longer protect the bank. Two ratings apply, both
code constants in
`control_logic.py` rather than `rig_config` fields:

- **The ladder total**, `VRB_MAX_POWER_W = 8000`.
- **Each bank's own rating**, `BANK_RATED_POWER_W`, from the bank table above.
  The ladder is in series and a relay bypasses each bank, so a command of N
  steps puts in circuit the banks whose bits are set in N, all carrying the same
  current. A bank on its own carries all of it: 0.5 Ω is bank 2 alone, 5.1 kW at
  50.4 V against its 4 kW. The 8 kW total used to allow that, and 1 Ω (bank 3,
  2.5 kW against 2 kW) and 2 Ω (bank 4, 1.3 kW against 1 kW) likewise.

Both are enforced twice:

- **Command selection.** Every setting sent is checked against both, using the
  measured module voltage with the elements at the low end of their ±5%. The
  total is a floor (V² / (0.95 × 8 kW), rounded up to the next step), but the
  per-bank ratings are not, because a lower resistance can split the power
  across more banks. A setting that fails moves to the nearest one that passes,
  the higher resistance on a tie.
- **Trip.** Measured V × I above the total faults the rig as `BANK OVERPOWER`
  in any state. While running, each bank's share of V × I (its fraction of the
  ladder resistance) is checked against its own rating too, once a setting has
  been in circuit for 0.5 s (`BANK_SETTLE_S`) so the current reading belongs to
  it. The console names the bank.

**Rated or 140%.** The sidebar's *Bank at rated power only* switch
(`SafetyLimits.bank_rated_power_only`) holds both at 100%. Unticked, the default,
allows 140% of both (`BANK_OVERLOAD_FACTOR`), so 11.2 kW for the ladder. 140% is
chosen so that no setting is ever refused at a full 50.4 V module: banks 1–5
each reach their rating alone at the same voltage (rating × resistance is 2000
for all five), and at 50.4 V that takes 133.7% with the elements at the low end
of their tolerance, 127% at nominal. Bank 1's elements then run at about 2.5 kW
each. The TE ratings are continuous figures at 70 °C ambient in free air, with a
short-term overload of 3× for 5 s; a lap holds full power for about 30 s at most,
under 3500 CFM of forced air, and banks 1–3 trip at 225 °C against their
elements' 275 °C. The switch chooses between the two fixed figures; nothing in
the config can go past 140%.

**Where 140% leans on margin rather than measurement:** bank 4 (two Uxcell
500 W parts, 1.27 kW alone at 50.4 V) has a thermocouple but only a placeholder
trip and no datasheet; bank 5 (Uxcell 500 W, 635 W alone) and bank 6 (Ohmite
HS300, 318 W alone) have no thermocouple at all.

At rated power the per-bank ratings cost load at the top of the charge: 0.5 Ω
and 0.25 Ω only come back below **43.6 V** (3.63 V/cell), and above that the
heaviest setting in rating is 0.75 Ω, banks 1 and 2 sharing about **3.4 kW** at
50.4 V. At 140% nothing is refused below 51.6 V, so the whole charge range is
unrestricted.

**4. Side-of-cell temperature sensors are not fitted.** Only the top of each cell
is instrumented. Planned. The bus bars between the series groups have a place in
the software and the sketch, and no sensors yet: see Bus-bar sensors above.

**4a. The relay drive's behaviour in reset, and the module's fusing, are not
recorded.** While the controller is restarting its pins float. Whether the relay
MOSFETs have gate pull-downs, so every relay stays released then, is not written
down anywhere, and neither is whether the module is fused. Both matter more now
that the controller can restart itself on its hardware watchdog.

**5. Bank composition is recorded here and nowhere else.** How many physical
resistors form each ladder step, and how they are wired, exists only in this
document. Keep it current if the bank changes, or the margin calculations above
become fiction.
