# Datasheets

Source documents for the numbers the rig's safety limits are built from. Kept
here so an inheriting team can verify every derived limit without hunting for
the right revision — and revision matters, because these values were checked
against **v1.2** specifically.

## INR-21700-P45B_v1.2.pdf

Molicel INR-21700-P45B, Product Data Sheet version 1.2. The cell used in the
module under test (12S4P, 48 cells per module).

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

**The power ratings are free-air figures.** `CFM Calculator.py` in the repo root
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
| 0.278 Ω (at the 180 A limit) | 180 A | ~9.0 kW |
| 0.25 Ω (bank minimum) | 200 A | ~10 kW |

That is the peak duty in the whole system, and it lands on one step.

**Two things to verify against the physical build**, which cannot be settled
from the datasheet alone because it covers a series rather than the specific
parts installed:

1. **Dissipation.** ~9 kW through the 0.25 Ω step exceeds the 2500 W free-air
   ceiling of even the largest part in this series by roughly 3.6×. That is not
   necessarily wrong — forced air raises it, the peak is transient rather than
   continuous, and the step may be several elements sharing the load — but the
   margin should be confirmed rather than assumed.
2. **RCWV.** At 0.25 Ω, √(P × R) gives 25.0 V even for a 2500 W part, against
   the ~50 V the module puts across the bank at minimum resistance. If that step
   is a single element, it is above its rated continuous working voltage.

Both hinge on how many physical resistors make up each ladder step and how they
are wired, which is not recorded anywhere in this repo. **Worth writing down.**

### Not yet used by the software

Nothing in the control logic models resistor temperature or power. Every trip
protects the *cells* — over-temp, over-current, undervoltage. The bank itself
has no thermal protection in software; it relies on the airflow analysis being
right and on the cell-side current limit indirectly bounding dissipation.

`rig_config.py` carries a note that resistor temperature sensors are planned.
When they are added, **155 °C is the number from this datasheet** — the
operating ceiling — and it will need its own limit field and trip, separate from
`max_temp`, which is a cell figure and sits at 60 °C.

## Adding another datasheet

If the cell changes, add the new PDF here with its revision in the filename, add
a section above mapping its values to `PackConfig` fields, and update the
defaults in `rig_config.py`. The pack limits will follow automatically — that is
the whole point of deriving them rather than typing them in.
