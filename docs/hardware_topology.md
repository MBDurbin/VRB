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

`CFM Calculator.py` in the repo root is the analysis behind this arrangement —
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

**2. No resistor thermal protection.** Every trip in `control_logic.py` protects
the *cells*. The bank has no temperature sensors yet and no software limit.
Planned, per the notes in `rig_config.py`. When they arrive, **155 °C** is the
TE series operating ceiling and needs its own limit field — `max_temp` is a
60 °C cell figure and must not be reused.

**3. The current limit exceeds the bank's rating.** The VRB is rated 8 kW, which
at 50 V is **160 A**. The over-current trip is 180 A ≈ 9 kW, about **12.5%
over**. That 180 A derives purely from the cells; the software has no idea the
resistor bank exists. Forced air may well cover it — the 2 kW element figures are
free-air ratings — but no software limit reflects the bank's own capability.

**4. Side-of-cell temperature sensors are not fitted.** Only the top of each cell
is instrumented. Planned.

**5. Bank composition is recorded here and nowhere else.** How many physical
resistors form each ladder step, and how they are wired, exists only in this
document. Keep it current if the bank changes, or the margin calculations above
become fiction.
