# Datasheets

Source documents for the numbers the rig's safety limits are built from. Kept
here so an inheriting team can verify every derived limit without hunting for
the right revision.

## Reliance RS50 — the installed cell (no datasheet here yet)

Reliance RS50, 21700, 5000 mAh, 70 A, flat top. The cell in the module under
test (12S4P, 48 cells per module) since it replaced the Molicel P45B below.

**There is no RS50 datasheet in this folder.** The figures come from the
supplier's product listing: liionwholesale.com, "Reliance RS50 70A 5000mAh Flat
Top 21700". If a manufacturer datasheet turns up, add it here and check the two
flagged rows against it.

| Listing value | `PackConfig` field | Derived module figure (12S4P) |
|---|---|---|
| Nominal capacity 5000 mAh | `cell_capacity_ah = 5.0` | 20.0 Ah (× 4P) |
| Minimum capacity 4950 mAh (0.2 C) | `cell_capacity_min_ah = 4.95` | 19.8 Ah (× 4P) |
| Max continuous discharge 70 A, "with 80 °C temperature cut-off" | `cell_max_continuous_a = 70` | **280 A** (× 4P) |
| Peak voltage 4.2 V | `cell_max_voltage = 4.2` | 50.4 V module, ~454 V battery |
| Nominal voltage 3.6 V | `cell_nominal_voltage = 3.6` | 43.2 V module |
| 80 °C cut-off (the only temperature given) | `cell_max_temp_c = 80.0` | 80 °C over-temp trip |
| **Not listed** — discharge cutoff | `cell_min_voltage = 2.5` | 30.0 V absolute floor |
| **Not listed** — DC impedance | `cell_dc_milliohm = 4.0` | 12 mΩ module (÷4P × 12S) |
| Max charge 15 A, standard 8 A | — | unused: the rig never charges |

**The two unlisted rows are working values, not sourced ones.** 2.5 V is the
usual cutoff for this class of cell; 4 mΩ is an estimate. The cutoff feeds the
undervoltage trips (30 V floor, 36 V module trip, 2.70 V per cell), so it is the
one that matters most to confirm. The impedance only feeds sag estimates and the
SIL plant; measuring a module's sag under a known load would settle it.

**80 °C is the right trip here, unlike on the P45B.** The P45B quoted an 80 °C
cut-off for its current *test* but a 60 °C discharge operating range, and the
rig used 60. The RS50 listing gives no separate operating range, so the trip
sits at the 80 °C attached to the 70 A rating. Lower it if a datasheet gives a
narrower range.

**The buffer comes out of the rating.** The over-current trip fires at
`max_amps + amp_buffer`, so derivation sets `max_amps = 280 − 5 = 275 A`, putting
the trip exactly on 280 A. With these cells the ladder's 0.25 Ω bottom step caps a
full module at about 202 A anyway, so the bank's power ratings bind first; see
"What bounds the load" in `PROJECT_CONTEXT.md`.

## INR-21700-P45B_v1.2.pdf — the previous cell

Molicel INR-21700-P45B, Product Data Sheet version 1.2. The cell the modules were
first built with, replaced by the RS50 above. Kept because its section below
records how each datasheet figure was read, and some tests pin the derivation
math to its published values.

Redistributed here as a manufacturer datasheet. Copyright remains E-One Moli
Energy Corp.; this copy is included for reference by people working on this rig.

### Where each value ends up

Everything below is entered in `rig_config.py` (`PackConfig`) or the GUI's
Battery Pack tab. Nothing is hand-computed — the pack figures in the right-hand
column are *derived* from the per-cell numbers.

| Datasheet value | `PackConfig` field | Derived pack figure (12S4P) |
|---|---|---|
| Typical capacity 4500 mAh | `cell_capacity_ah = 4.5` | 18.0 Ah (× 4P) |
| Minimum capacity 4300 mAh | `cell_capacity_min_ah = 4.3` | 17.2 Ah (× 4P) |
| Discharge current, continuous 45 A | `cell_max_continuous_a = 45.0` | **180 A** (× 4P) |
| Charge voltage 4.2 V | `cell_max_voltage = 4.2` | 50.4 V module, ~454 V battery |
| Nominal voltage 3.6 V | `cell_nominal_voltage = 3.6` | 43.2 V module |
| Discharge cutoff 2.5 V | `cell_min_voltage = 2.5` | 30.0 V absolute floor |
| Discharge temperature −40 to 60 °C | `cell_max_temp_c = 60.0` | 60 °C over-temp trip |
| DC impedance 15 mΩ @ 50% SOC | `cell_dc_milliohm = 15.0` | 45 mΩ module (÷4P × 12S) |
| Typical energy 16.2 Wh | — | 777.6 Wh module |

### Three readings worth getting right

**The 45 A rating carries an "80 °C cut-off" note, and that is not the operating
limit.** The temperature row separately gives a discharge range of −40 to 60 °C.
80 °C is the condition under which the 45 A *test* terminates; 60 °C is what the
cell is rated to operate at. The rig uses 60. An early version used 65, sitting
between the two figures, which is the mistake this note exists to prevent.

**The buffer comes out of the rating, not on top of it.** The over-current trip
fires at `max_amps + amp_buffer`, so deriving `max_amps` as the raw 180 A would
put the real trip at 185 A — above the cells. Derivation therefore sets
`max_amps = 180 − buffer = 175 A`, landing the trip exactly on 180 A. The
original code shipped 182 A with a 5 A buffer, tripping at 187 A.

**The undervoltage trip must sit above the 2.5 V cutoff, not at it.** With
45 mΩ of module resistance, 180 A of draw sags the module 8.1 V. A pack resting
at a healthy-looking 3.2 V/cell reads 2.499 V/cell while loaded — already
through the cutoff. The trip is set at 3.0 V/cell (36.0 V) to leave room for
that sag, and the per-cell trip at 2.70 V.

### What the datasheet cannot tell you from the text alone

Capacity falls at high discharge rates, and this rig draws **10 C** (180 A on
18 Ah). The Discharge Rate Characteristics chart on page 1 shows by how much,
but it is a plotted curve with no tabulated values — it has to be read off by
eye. Until someone does that, coulomb counting integrates against the flat
nameplate capacity and therefore reads **optimistically high under load**, which
is the dangerous direction. `use_minimum_capacity` in the config switches to the
4300 mAh figure, which helps but does not address rate dependence.

## TE-Series_High-Power-Wirewound_9-1773453-2_RevE.pdf

TE Connectivity TE Series high-power wirewound resistors, document 9-1773453-2
Rev. E, 05/2021. The braking/load resistors that make up the bank. Its listed
applications include "load test simulation" and "dynamic braking", which is
exactly what this rig does.

Copyright remains TE Connectivity.

### Key ratings

| Parameter | Value |
|---|---|
| Power rating | 50 W – 2500 W **at 70 °C in free air** |
| Resistance range | 0.1 Ω – 2.7 kΩ depending on power rating |
| Tolerance | ±5% (J) or ±10% (K) |
| Operating temperature | **−55 to +155 °C** |
| Short-term overload | 3 × rated power for 5 seconds |
| Temperature coefficient | ±400 PPM/°C below 20 Ω, ±300 PPM/°C at or above |
| Rated continuous working voltage | **RCWV = √(P × R)** |
| Construction | Ceramic core, Ni-Cr or Cu-Ni wire, UL94V flameproof coating |
| Part numbering | `TE` – power – mounting – resistance – tolerance (e.g. `TE 50 B 1K0 J`) |

Note the resistance range narrows as power rating rises: the 2500 W part starts
at 1.0 Ω, so the bank's 0.25 Ω step cannot be a single 2500 W unit of this
series. Whatever makes up that step is either a lower-power part or several
elements combined.

### Why this matters to the rig, and what to check

**The power ratings are free-air figures.** `analysis/CFM Calculator.py`
exists precisely because the bank is force-cooled — it models the resistors as a
staggered tube bank in cross-flow and sizes the airflow. Its 60 mm cylinder
diameter matches the 1000 W–2500 W parts in the dimensions table on page 5. The
derating curve and temperature-rise chart on page 3 are the datasheet side of
that same question, but both are **plotted, not tabulated**, so they have to be
read by eye.

**The smallest ladder step carries the largest thermal load.** The bank is a
series ladder (0.25, 0.5, 1, 2, 4, 8, 16, 32 Ω) across a ~50 V module, so
minimum resistance means maximum current, and that current flows through the
element that is on its own:

| Bank state | Current | Dissipated in the 0.25 Ω step |
|---|---|---|
| 0.25 Ω at a 44.7 V module | 179 A | 8.0 kW (its rating) |
| 0.25 Ω at a full 50.4 V RS50 module | 202 A | ~10.2 kW (127%) |

That is the peak duty in the whole system, and it lands on one step. The step
is bank 1: four 1 Ω TE2000 elements in parallel, so each carries a quarter of
the power and sees the whole module voltage. At a full module that is 2.54 kW and
50.4 V per element, against 2 kW and an RCWV of √(2000 × 1) = 44.7 V. RCWV is
just the voltage at rated power, so it is the same overload, not a second one.
The bank's composition is now recorded in `docs/hardware_topology.md`.

### How the software uses it

The bank now has protection of its own, separate from the cell trips:

- **Power.** Every setting is checked against the 8 kW ladder total and each
  bank's own rating, and so is the measured power; the software allows 140% of
  them by default, 100% with *Bank at rated power only* ticked. See "The bank's
  power ratings" in `docs/hardware_topology.md`.
- **Temperature.** Thermocouples on banks 1–4 trip at 225 °C for the TE banks.
  **155 °C in this datasheet is an ambient limit**, not the element's: the
  element is conventionally limited to 275 °C, where the derating curve on page 3
  reaches zero load, and the 225 °C trip leaves 50 K under that. Bank 4's 150 °C
  trip is a placeholder for a part with no datasheet here.

## Adding another datasheet

If the cell changes, add the new PDF here with its revision in the filename (or,
as with the RS50, record where the figures came from if there is no PDF), add a
section above mapping its values to `PackConfig` fields, and update the defaults
in `rig_config.py` and the values in `rig_config.json`. The pack limits will follow automatically — that is
the whole point of deriving them rather than typing them in.
