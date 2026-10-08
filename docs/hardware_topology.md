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
in series, 4 in parallel — of Molicel INR-21700-P45B, obtained through a Tesla
scholarship.

**The bench loads one module.** See the Battery topology section of
`PROJECT_CONTEXT.md` for why that distinction governs the physics, and
`datasheets/` for the cell datasheet the limits derive from.

| Per module | Value |
|---|---|
| Continuous discharge current | 180 A |
| Charge voltage | 50.4 V |
| Nominal voltage | 43.2 V |
| Discharge cutoff | 30.0 V |
| Internal resistance | 0.045 Ω |

---

## Resistor bank

Eight banks give the ladder its binary resistance. **Maximum energy dissipation
is 8 kW.**

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
the current:

| Bank current | Per resistor | Per resistor power | vs 2000 W rating | Bank voltage vs 44.7 V RCWV |
|---|---|---|---|---|
| 160 A | 40.0 A | 1600 W | −20% | 40.0 V (−10.6%) |
| **180 A** (trip) | 45.0 A | **2025 W** | **+1.2%** | **45.0 V (+0.6%)** |
| 200 A (0.25 Ω floor) | 50.0 A | 2500 W | +25% | 50.0 V (+11.8%) |

At the over-current trip the design sits roughly **1% over** the elements' free-air
rating and their rated continuous working voltage (RCWV = √(P × R) = 44.72 V for
a 2 kW 1 Ω part). Forced air raises the usable power well above the free-air
figure, so this is tight rather than wrong — but it is tight, and it is why the
fan is not optional.

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
(equivalently, the number of 0.25 Ω steps). A received `00000000` is converted to
`00000001` — 0.25 Ω — so the bank is never commanded to zero resistance.

Firmware: `arduino/resistor_bank_controller/`.

---

## Temperature acquisition

DS18B20 sensors on OneWire, each wired with ground, signal and 5 V. **A larger
than usual pull-up resistor is used**, because several buses feeding one Arduino
add up to significant capacitance.

Sensors are split across **6 buses of 8**, pairing two series groups per bus
(groups 1–2 on bus 1, 3–4 on bus 2, and so on). The split exists to improve
signal integrity and keep bus capacitance down. The Arduino sends the updated
values through a state machine so it transmits only when something has changed.

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

---

## Current and voltage acquisition

**Current**: a Hanalem 400 A sensor feeding the NI-DAQ. Its ±4 V for ±400 A
output is where `DaqConfig.current_amps_per_volt = 100.0` comes from.

**Voltage**: the board housing the Arduino also steps the module tap voltages
down and feeds them to the NI-DAQ, which passes them to the application. The
divider ratio is `DaqConfig.voltage_multiplier = 11.0` (a 10:1 divider).

---

## Gaps between the hardware and the software

Recorded here because none of them are visible from the code.

**1. No fan interlock.** The fan is plugged into a wall socket and has no
feedback path. Nothing in software confirms it is running, so a run with the fan
unplugged has no protection at all — the cell-side trips would not notice a
resistor bank cooking. This is currently a procedural control only: *never run
without the fan*.

**2. Resistor thermal protection — banks 1–4 only.** One thermocouple per bank
on banks 1–4, read by the NI-DAQ. The channels are `resistor_tc_channels` in
`rig_config.json`, default `cDAQ1Mod3/ai0`–`ai3`, bank 1 first, Type K. They are
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
each) it works out to 275 A, about 14 kW at 50 V, so the cell limit can no
longer protect the bank. Two ratings apply, both code constants in
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
is instrumented. Planned.

**5. Bank composition is recorded here and nowhere else.** How many physical
resistors form each ladder step, and how they are wired, exists only in this
document. Keep it current if the bank changes, or the margin calculations above
become fiction.
