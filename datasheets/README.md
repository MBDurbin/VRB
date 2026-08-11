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

## Adding another datasheet

If the cell changes, add the new PDF here with its revision in the filename, add
a section above mapping its values to `PackConfig` fields, and update the
defaults in `rig_config.py`. The pack limits will follow automatically — that is
the whole point of deriving them rather than typing them in.
