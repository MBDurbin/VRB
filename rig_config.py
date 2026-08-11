"""
Rig configuration: vehicle model, battery pack, DAQ wiring, and safety limits.

Everything a future team needs to change when moving this rig to a different car
or a different cell lives here, and is editable from the GUI (Configure button)
without touching source. Settings persist to rig_config.json beside this file.

Design intent for inheriting teams:

  * Pack limits are DERIVED from per-cell datasheet values times the series and
    parallel counts. Enter the numbers off your cell's datasheet and the pack
    current, voltage and capacity limits follow automatically -- you should not
    be hand-computing 180 A anywhere.
  * Safety limits may be overridden to be MORE conservative than the derived
    values, but the GUI warns when an override exceeds what the cells are rated
    for. Deriving is the default; overriding is a deliberate act.
  * Vehicle parameters describe the car, not the rig. A new chassis means new
    numbers here and nothing else.

HOW THIS FILE IS STRUCTURED
    Four dataclasses, each a group of related settings, plus a RigConfig that
    holds one of each and handles saving/loading:

        VehicleParams  - the car being simulated (mass, aero, tyres, drivetrain)
        PackConfig     - the cells and how they are wired
        DaqConfig      - which NI-DAQ channels and sensors are physically wired
        SafetyLimits   - the trip thresholds that stop a run
        RigConfig      - all four together, plus JSON persistence

    Each dataclass is followed by a *_FIELD_LABELS dictionary. The GUI builds its
    Configure dialog by walking the dataclass fields and looking up these labels,
    so ADDING A FIELD HERE AUTOMATICALLY ADDS IT TO THE GUI. No GUI code needed.

UNITS
    Every field name carries its unit as a suffix where there is any ambiguity
    (_kg, _ms2, _ah, _volts, _s). Fields without a suffix are dimensionless
    ratios or coefficients. Stick to this when adding fields.
"""

# --- Standard library imports -------------------------------------------------

# json: reads and writes rig_config.json. Chosen over pickle/YAML deliberately --
# a student should be able to open the config in Notepad and understand it, and
# a corrupt file should be fixable by hand rather than requiring this program.
import json

# os: used for path handling (building CONFIG_PATH, checking the file exists,
# and shortening the filename in warning messages).
import os

# dataclasses gives us a lot for free here:
#   dataclass  - decorator that generates __init__, __repr__, __eq__ from the
#                annotated fields, so a settings group is just a list of names,
#                types and defaults.
#   asdict     - recursively converts a dataclass instance to a plain dict, which
#                is what gets written to JSON.
#   field      - needed for mutable defaults (see voltage_channels below); a bare
#                list default would be SHARED between every instance.
#   fields     - introspects the field list at runtime. Used both by the GUI to
#                build its form and by from_dict() to filter unknown keys.
#   replace    - makes a modified copy of a dataclass instance. Used by
#                derivation_conflicts() to probe what derivation WOULD do without
#                actually mutating the original.
from dataclasses import dataclass, asdict, field, fields, replace

# List is only used for the type annotation on voltage_channels. On Python 3.9+
# a plain `list[str]` would work, but the explicit import keeps this readable on
# older interpreters a team might still be running.
from typing import List


# Absolute path to the saved settings file, resolved relative to THIS file rather
# than the current working directory. That matters: the rig is launched from
# shortcuts, IDEs and terminals with different working directories, and a
# relative path would silently create or read a different file each time.
# __file__          -> ".../rig_config.py"
# os.path.abspath   -> makes it absolute even if launched via a relative path
# os.path.dirname   -> strips the filename, leaving the project directory
# os.path.join      -> appends the config filename using the OS separator
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rig_config.json")


# ================= VEHICLE =================

@dataclass
class VehicleParams:
    """Vehicle and environment parameters for the road-load model.

    Retune the simulation by editing a field rather than the physics -- either
    mutate the active config, or build a variant and pass it in:

        fast = VehicleParams(drag_coefficient=0.55, mass_driver_kg=65.0)
        compute_required_power(v, a, params=fast)

    These describe the CAR, not the test bench. The bench loads one battery
    module; these numbers exist so the lap profile can be turned into the power
    that whole car would demand, which is then translated into one module's share.
    All of them feed compute_road_load_forces() in control_logic.py.
    """

    # --- Mass ---

    # Dry mass of the car itself: chassis, battery, motor, bodywork, fluids.
    # Feeds BOTH the inertia term (F = ma) and the rolling-resistance term (via
    # weight pressing the tyres down), so an error here shifts the whole power
    # curve, not just acceleration.
    mass_car_kg: float = 260.0 #kg

    # Driver mass, kept separate from the car so it can be changed per driver
    # without touching the chassis figure. Summed with the above in
    # total_mass_kg; the physics never uses either one alone.
    mass_driver_kg: float = 70.0 #kg

    # Rotational inertia of wheels, brake rotors, axles and drivetrain, modelled
    # as extra "virtual" mass. Applies to acceleration ONLY: spinning components
    # resist changes in speed but do not press the tyres into the track, so this
    # must not feed the normal force behind rolling resistance.
    # Typical 1.04-1.10 for an open-wheel/student formula car.
    #
    # Currently 1.00, which switches the effect OFF -- the 1.05 that shipped
    # first was a textbook figure, not a measured property of this car. Running
    # at 1.00 slightly UNDER-loads the bench during acceleration, which is the
    # conservative direction. Raise it once the real inertia is measured.
    # A value below 1.0 is physically meaningless (spinning parts cannot make a
    # car easier to accelerate) and is caught by a unit test.
    rotational_mass_factor: float = 1.00 # should be =>1

    # --- Environment ---

    # Air density. Sets the scale of both drag and downforce, since both are
    # proportional to it. 1.2255 kg/m^3 is the ISA sea-level, 15 C value.
    # Worth changing for a hot day or an altitude event: thinner air means less
    # drag but also less downforce, so it does not simply reduce load.
    air_density_kgm3: float = 1.2255  # ISA sea level, 15 C

    # Gravitational acceleration, used to turn mass into the weight component of
    # the tyre normal force. A field rather than a constant purely so the whole
    # physics model has a single source for every number it uses.
    gravity_ms2: float = 9.81

    # --- Aerodynamics ---
    # Drag and downforce carry separate reference areas: they are frequently the
    # same number, but a wing package can change one without the other.

    # Cd, the drag coefficient. Dimensionless. Multiplied by drag_area_m2 and
    # dynamic pressure to give the drag force. Cd and area always appear as a
    # product, so only "CdA" is physically meaningful -- they are split here
    # because teams usually have them as separate figures from CFD or testing.
    drag_coefficient: float = 0.6           # Cd

    # Reference area the drag coefficient was measured against. Usually frontal
    # area. Must match whatever area your Cd figure assumes, or the product is
    # wrong even if both numbers look individually sensible.
    drag_area_m2: float = 2.224             # reference area for drag

    # Cl, the lift coefficient. NEGATIVE means downforce, following the usual
    # aero sign convention (lift is positive upward). The physics takes abs() of
    # this, so a positive value here would still produce downforce -- the sign is
    # kept for readability and to match how aero data is normally reported.
    lift_coefficient: float = -1.0          # Cl, negative = downforce

    # Reference area for the lift/downforce coefficient. Separate from the drag
    # area because a wing change can alter one without the other.
    downforce_area_m2: float = 2.224        # reference area for downforce

    # --- Tyres ---

    # Rolling resistance coefficient: the fraction of the normal force that
    # opposes motion. Dimensionless, typically 0.010-0.020 for racing slicks on
    # tarmac. Note this multiplies the DYNAMIC normal force (weight PLUS
    # downforce), so rolling drag grows with speed rather than staying constant.
    rolling_resistance_coeff: float = 0.015

    # --- Drivetrain ---
    # Mechanical efficiency between motor shaft and contact patch (chain, gears,
    # bearings). ~0.95 for a well-maintained chain drive.
    #
    # Applied asymmetrically in compute_required_power(): power is DIVIDED by
    # this when driving (the motor must produce more than reaches the road) and
    # MULTIPLIED when braking (friction eats part of what would come back).
    # Clamped to [0.01, 1.0] at the point of use so a mistyped 0 cannot divide by
    # zero inside the safety-critical logic process.
    drivetrain_efficiency: float = 0.95 #assumed but should be measured

    @property
    def total_mass_kg(self) -> float:
        """Car plus driver. The only mass figure the physics actually uses.

        A property rather than a stored field so it can never drift out of sync
        with the two values it is derived from.
        """
        return self.mass_car_kg + self.mass_driver_kg


# Human-readable labels and units for GUI generation. Keys must match field
# names; any field missing here falls back to a prettified field name.
#
# Each value is a (label, unit) tuple. The GUI renders the label in normal text
# and the unit in smaller muted text beside it. An empty unit string means the
# quantity is dimensionless and no unit is shown.
VEHICLE_FIELD_LABELS = {
    'mass_car_kg':             ("Car mass", "kg"),
    'mass_driver_kg':          ("Driver mass", "kg"),
    'rotational_mass_factor':  ("Rotational mass factor", "x"),
    'air_density_kgm3':        ("Air density", "kg/m^3"),
    'gravity_ms2':             ("Gravity", "m/s^2"),
    'drag_coefficient':        ("Drag coefficient (Cd)", ""),
    'drag_area_m2':            ("Drag reference area", "m^2"),
    'lift_coefficient':        ("Lift coefficient (Cl)", "neg = downforce"),
    'downforce_area_m2':       ("Downforce reference area", "m^2"),
    'rolling_resistance_coeff':("Rolling resistance coeff", ""),
    'drivetrain_efficiency':   ("Drivetrain efficiency", "0-1"),
}


# ================= BATTERY PACK =================

@dataclass
class PackConfig:
    """Per-cell datasheet values plus pack topology. Pack limits derive from these.

    Defaults describe the Molicel INR-21700-P45B in 12S4P, taken from Product
    Data Sheet v1.2. Replace with your own cell's numbers.

    THE RULE FOR THIS CLASS: every field is a number you can read directly off a
    cell datasheet or count off the physical pack. Nothing here is calculated by
    hand. Everything calculated appears below as a @property, so changing one
    datasheet figure updates every derived value at once and none of them can
    disagree with each other.
    """

    # Free-text cell part number. Purely informational -- it appears in startup
    # logs and the GUI's status strip so an operator can confirm at a glance
    # which cell the software thinks is installed. Nothing computes with it.
    cell_model: str = "Molicel INR-21700-P45B"

    # Cells in SERIES within one module. Multiplies voltage: 12S of a 4.2 V cell
    # gives a 50.4 V module. Also determines how many DAQ voltage taps are
    # needed, which DaqConfig.validate() cross-checks.
    series_count: int = 12

    # Cells in PARALLEL within each series group. Multiplies current capability
    # and capacity but NOT voltage: 4P of a 45 A cell gives a 180 A module.
    parallel_count: int = 4

    # Modules wired in series to form the car's complete battery.
    #
    # The BENCH loads exactly one module: the DAQ reads its series_count taps and
    # the resistor bank is wired across it alone (~50 V). This number describes
    # the CAR, and exists so the lap physics -- which computes whole-vehicle power
    # off a ~450 V battery -- can be translated into the right duty for the single
    # module on the bench. See compute_target_resistance().
    #
    # Series modules all carry the same current, so every current limit below is
    # a per-module AND whole-battery figure at once, and does not scale with this.
    modules_in_series: int = 9

    # Straight off the cell datasheet

    # Typical capacity of one cell. "Typical" means the average a good cell
    # delivers; individual cells vary. Used for coulomb counting unless
    # use_minimum_capacity is set below.
    cell_capacity_ah: float = 4.5           # typical; see usable_capacity_ah note

    # Guaranteed-minimum capacity of one cell -- the worst-case figure the
    # manufacturer commits to. Lower than typical (4.3 vs 4.5 here, a 4.65%
    # difference across the pack). Selecting this is the conservative choice
    # because overstating remaining charge is the dangerous direction.
    cell_capacity_min_ah: float = 4.3       # minimum / worst case

    # Maximum sustained discharge current for ONE cell. This is the single most
    # safety-critical number in this file: it sets the over-current trip for the
    # whole rig via max_current_a below. Check the datasheet carefully -- many
    # cells quote a higher "pulse" rating that must NOT be used here.
    cell_max_continuous_a: float = 45.0

    # Fully-charged resting voltage of one cell. Sets the top of the pack's
    # voltage range. Not a trip threshold; it is a reference for the GUI's plot
    # scaling and the derived battery figures.
    cell_max_voltage: float = 4.2

    # Nominal (average-over-discharge) cell voltage. Used only for energy figures
    # in watt-hours, since Wh = Ah * average volts. Not a trip threshold.
    cell_nominal_voltage: float = 3.6

    # Absolute discharge cutoff for one cell -- the voltage below which the cell
    # is damaged. This is a HARD FLOOR, not a trip point: the actual undervoltage
    # trip is set ABOVE it by suggested_min_voltage() to leave room for the
    # voltage sag that appears under load. Tripping at this value would only ever
    # fire after the damage was done.
    cell_min_voltage: float = 2.5           # absolute discharge cutoff

    # Highest cell temperature permitted while discharging. Sets the over-temp
    # trip. Note this is the OPERATING range from the datasheet, which is often
    # lower than the cut-off temperature quoted alongside a current rating -- the
    # P45B lists 60 C operating but an 80 C cut-off for its 45 A test, and 60 is
    # the correct one to use here.
    cell_max_temp_c: float = 60.0           # discharge operating ceiling

    # DC internal resistance of one cell, in milliohms, measured at 50% state of
    # charge. Drives the sag calculations: series adds and parallel divides, so
    # 15 mOhm in 12S4P gives a 45 mOhm module. This is why the undervoltage trip
    # sits well above the absolute cutoff -- at 180 A this module sags over 8 V.
    cell_dc_milliohm: float = 15.0          # DC impedance at 50% SOC

    # Use minimum rather than typical capacity for SOC. Conservative: a worst-case
    # pack really does hold less, and overstating SOC is the dangerous direction.
    #
    # Ships False (typical) to match how the rig has been run to date. Setting it
    # True makes the state-of-charge readout pessimistic, which is the safer way
    # to be wrong. Note that NEITHER setting accounts for capacity falling at
    # high discharge rates, which at this rig's 10 C draw is a further optimism.
    use_minimum_capacity: bool = False

    # --- Derived pack values ---
    # Everything below is computed from the fields above. None of it is stored,
    # so none of it can go stale when a datasheet figure is edited.

    @property
    def cell_count(self) -> int:
        """Total cells in ONE module. 12S x 4P = 48."""
        return self.series_count * self.parallel_count

    @property
    def capacity_ah(self) -> float:
        """Nameplate pack capacity used for coulomb counting.

        Parallel cells share the load, so capacity scales with parallel_count.
        Series count does NOT appear: wiring cells in series raises voltage, not
        amp-hours.
        """
        # Pick typical or guaranteed-minimum per-cell capacity per the flag above.
        per_cell = self.cell_capacity_min_ah if self.use_minimum_capacity else self.cell_capacity_ah
        return per_cell * self.parallel_count

    @property
    def max_current_a(self) -> float:
        """Maximum sustained current for the module -- and for the whole battery.

        Parallel cells each carry a share, so the module's limit is the cell
        rating times parallel_count. Series modules all carry the SAME current,
        so this figure is simultaneously the whole-battery limit and does not
        scale with modules_in_series.
        """
        return self.cell_max_continuous_a * self.parallel_count

    @property
    def max_voltage(self) -> float:
        """Module voltage at full charge. Series adds voltage; parallel does not."""
        return self.cell_max_voltage * self.series_count

    @property
    def nominal_voltage(self) -> float:
        """Module voltage averaged over a discharge. Used for energy figures."""
        return self.cell_nominal_voltage * self.series_count

    @property
    def min_voltage(self) -> float:
        """Absolute floor. The undervoltage trip should sit ABOVE this to leave
        room for IR sag under load -- see suggested_min_voltage()."""
        return self.cell_min_voltage * self.series_count

    @property
    def resistance_ohm(self) -> float:
        """Module DC internal resistance: series adds, parallel divides."""
        # Convert milliohms to ohms, then apply the topology: each parallel group
        # is cell_R / parallel_count, and series_count of those groups add up.
        return (self.cell_dc_milliohm / 1000.0) * self.series_count / self.parallel_count

    # --- Whole-battery values (the car's full pack) ---
    # Reference figures for the vehicle the bench is standing in for. The rig
    # itself never sees these -- the bank is across one module only.
    #
    # They exist so the GUI and startup logs can state what car this module
    # belongs to, and so nobody has to multiply by 9 in their head. Do NOT feed
    # these into a current limit: series modules share one current, so the
    # current figures above already apply to the whole battery unchanged.

    @property
    def battery_cell_count(self) -> int:
        """Every cell in the car: 48 per module x 9 modules = 432."""
        return self.cell_count * self.modules_in_series

    @property
    def battery_max_voltage(self) -> float:
        """Car battery voltage at full charge. ~454 V for the default pack."""
        return self.max_voltage * self.modules_in_series

    @property
    def battery_nominal_voltage(self) -> float:
        """Car battery voltage averaged over a discharge. ~389 V by default."""
        return self.nominal_voltage * self.modules_in_series

    @property
    def battery_min_voltage(self) -> float:
        """Car battery voltage at the cells' absolute cutoff. ~270 V by default."""
        return self.min_voltage * self.modules_in_series

    @property
    def battery_resistance_ohm(self) -> float:
        """Whole-battery internal resistance. Series modules add resistance."""
        return self.resistance_ohm * self.modules_in_series

    @property
    def battery_energy_wh(self) -> float:
        """Total stored energy in the car's battery.

        Defined before energy_wh below purely for grouping -- Python resolves the
        reference when the property is CALLED, not when the class is defined, so
        the ordering is fine.
        """
        return self.energy_wh * self.modules_in_series

    @property
    def energy_wh(self) -> float:
        """Stored energy in ONE module: amp-hours times average volts."""
        return self.capacity_ah * self.nominal_voltage

    def sag_volts(self, current_a: float) -> float:
        """Voltage drop across pack internal resistance at a given current."""
        # Ohm's law. At the 180 A limit with 45 mOhm this is 8.1 V, which is why
        # the undervoltage trip cannot sit at the cells' absolute cutoff.
        return current_a * self.resistance_ohm

    def voltage_under_load(self, ocv_per_cell: float, current_a: float) -> float:
        """What the module reads while loaded, given a resting per-cell voltage.

        ocv_per_cell is the OPEN-CIRCUIT (resting) voltage of one cell. The
        measured voltage while current flows is always lower by the sag. This is
        the calculation that shows why 3.0 V/cell is the right trip point: a pack
        resting at a healthy-looking 3.2 V/cell reads 2.499 V/cell at 187 A,
        already through the 2.5 V cutoff.
        """
        return (ocv_per_cell * self.series_count) - self.sag_volts(current_a)

    def suggested_min_voltage(self, per_cell_trip: float = 3.0) -> float:
        """Undervoltage trip point, defaulting to 3.0 V/cell.

        Deliberately above the absolute cutoff: at max current this pack sags
        enough that a cell resting at a healthy-looking voltage can be pushed
        under its cutoff while loaded. Tripping at the absolute floor would only
        ever fire after the damage was done.
        """
        return per_cell_trip * self.series_count


# GUI labels for the Battery Pack tab. Same (label, unit) format as the vehicle
# table above; see the note there for how the GUI consumes these.
PACK_FIELD_LABELS = {
    'cell_model':            ("Cell model", ""),
    'series_count':          ("Series count (S)", "cells per module"),
    'parallel_count':        ("Parallel count (P)", "cells"),
    'modules_in_series':     ("Modules in series", "per battery"),
    'cell_capacity_ah':      ("Cell capacity (typical)", "Ah"),
    'cell_capacity_min_ah':  ("Cell capacity (minimum)", "Ah"),
    'cell_max_continuous_a': ("Cell max continuous current", "A"),
    'cell_max_voltage':      ("Cell max voltage", "V"),
    'cell_nominal_voltage':  ("Cell nominal voltage", "V"),
    'cell_min_voltage':      ("Cell cutoff voltage", "V"),
    'cell_max_temp_c':       ("Cell max discharge temp", "C"),
    'cell_dc_milliohm':      ("Cell DC impedance", "mOhm"),
    'use_minimum_capacity':  ("Use minimum capacity for SOC", ""),
}


# ================= DATA ACQUISITION =================

# Default NI-DAQ channel names for the 12 cumulative voltage taps, in ORDER.
# Order is meaningful: entry i must be the tap reading the sum of cells 1..i+1,
# because per-cell voltages come from differencing consecutive entries. Getting
# two entries the wrong way round produces a negative cell voltage rather than an
# error, so double-check against the physical wiring.
#
# The "cDAQ1ModN/aiM" form is National Instruments' addressing: chassis 1,
# module N, analog input M. Each module here carries 4 inputs, so 12 taps plus
# one current channel spans four modules.
DEFAULT_VOLTAGE_CHANNELS = [
    "cDAQ1Mod8/ai1", "cDAQ1Mod8/ai2", "cDAQ1Mod8/ai3",
    "cDAQ1Mod7/ai0", "cDAQ1Mod7/ai1", "cDAQ1Mod7/ai2", "cDAQ1Mod7/ai3",
    "cDAQ1Mod6/ai0", "cDAQ1Mod6/ai1", "cDAQ1Mod6/ai2", "cDAQ1Mod6/ai3",
    "cDAQ1Mod5/ai0",
]


@dataclass
class DaqConfig:
    #NOTE: NEED TO VERIFY WHEN BACK IN UTAH
    """NI-DAQ channel mapping, scaling, and temperature bus layout.

    Channel names and sensor counts describe physical wiring, so they cannot be
    derived from the pack's series/parallel counts -- but they must AGREE with
    them. validate() reports any disagreement; the DAQ process prints those
    warnings at startup rather than failing silently or crashing on an index.

    One voltage channel per series group, wired cumulatively: channel i reads
    the sum of cells 1..i+1, so per-cell voltages come from differencing.

    CHANGES HERE ONLY TAKE EFFECT ON RESTART. The DAQ process builds its nidaqmx
    task and sensor buffers once at startup, so the GUI shows a "restart
    required" dialog after saving rather than letting you believe a rewiring
    change is already live.
    """

    # --- Channel mapping ---

    # The single analog input carrying the current transducer's output. Must NOT
    # also appear in voltage_channels; validate() checks for that collision
    # because miswiring it would silently turn a current reading into a cell
    # voltage.
    current_channel: str = "cDAQ1Mod8/ai0"

    # Ordered list of cumulative voltage taps, one per series group.
    #
    # field(default_factory=...) rather than a plain list default: a mutable
    # default on a dataclass is created ONCE and shared by every instance, so
    # editing one config's channel list would silently edit them all. The lambda
    # wraps list() to hand out a fresh copy each time.
    voltage_channels: List[str] = field(
        default_factory=lambda: list(DEFAULT_VOLTAGE_CHANNELS))

    # --- Scaling ---
    # Resistor divider on each tap: a 10:1 divider means multiply by 11.
    #
    # The taps reach ~50 V but the DAQ inputs only accept +/-10 V, so each tap
    # goes through a divider. Multiply by (R1+R2)/R2 to recover the real voltage:
    # a 10:1 divider drops to 1/11th, hence 11.0. Get this wrong and every
    # voltage reading -- and therefore every voltage trip -- is off by the ratio.
    voltage_multiplier: float = 11.0

    # Current transducer output, amps per volt at the DAQ input.
    # The sensor emits a small voltage proportional to current; this converts it
    # back. 100.0 means 1 V at the input represents 100 A of real current.
    current_amps_per_volt: float = 100.0

    # --- Analog input range ---
    # The voltage window each DAQ channel is configured to measure. Set this to
    # the smallest range that covers your signals: a wider range than necessary
    # wastes ADC resolution, while a narrower one clips. Negative minimum allows
    # for a bidirectional current sensor reading below zero during regen.
    ai_min_volts: float = -10.0
    ai_max_volts: float = 10.0
    #NOTE THIS WILL NEED EDITING WHEN WE ADD THE RESISTOR TEMPS AND INBETWEEN TEMPS
    # --- Temperature sensor layout ---
    # DS18B20s on OneWire buses. The Arduino emits one CSV line per bus:
    # "<bus number>,<t1>,...,<tN>", so a line carries sensors_per_bus + 1 fields.

    # Number of separate OneWire buses on the temperature Arduino. Sensors are
    # split across several buses rather than one long chain because each bus is
    # read sequentially -- more buses means a faster full sweep.
    temp_bus_count: int = 6

    # DS18B20 sensors on each bus. Multiplied by temp_bus_count to give the total
    # sensor count, which validate() compares against the pack's cell count.
    #
    # IMPORTANT: the sensors' ROM addresses are hardcoded in the Arduino sketch,
    # not here. Changing this number alone will not change what the firmware
    # reads -- the sketch must be edited and re-flashed to match.
    sensors_per_bus: int = 8

    # Serial baud rate for the temperature Arduino. Must match the Serial.begin()
    # value in its sketch or the handshake returns garbage and the device is
    # never identified. High because 48 readings per sweep is a lot of text.
    temp_baud_rate: int = 115200

    # --- Loop timing ---
    # Target seconds per DAQ cycle. 0.1 s is 10 Hz. The loop sleeps for whatever
    # is left of this interval after reading, so lowering it samples faster at
    # the cost of CPU. Must stay comfortably below SafetyLimits.daq_stale_timeout_s
    # or normal operation would look like a stalled DAQ.
    sample_period_s: float = 0.1

    @property
    def channel_count(self) -> int:
        """How many voltage taps are configured. Should equal pack.series_count."""
        return len(self.voltage_channels)

    @property
    def sensor_count(self) -> int:
        """Total thermistors across all buses. Should cover every cell."""
        return self.temp_bus_count * self.sensors_per_bus

    @property
    def temp_fields_per_line(self) -> int:
        """Comma-separated fields in one serial line from the temperature Arduino.

        The +1 is the bus number, which the sketch prints before the readings:
        "1,25.0,25.5,..." is 1 identifier plus sensors_per_bus temperatures.
        The parser rejects any line that does not have exactly this many fields,
        which is what stops a truncated serial read being applied as if the
        missing sensors simply were not there.
        """
        return self.sensors_per_bus + 1

    def validate(self, pack: PackConfig):
        """Report mismatches between the wiring and the configured pack."""
        # Collected rather than raised: a mismatch should be shouted about at
        # startup and shown in red in the GUI, but must not stop the rig running.
        # A team mid-rewire needs to be able to see the warning AND boot.
        problems = []

        # One voltage tap per series group. Fewer means some cells are never
        # measured; more means the extras are ignored. Either way the per-cell
        # undervoltage protection is not covering what you think it is.
        if self.channel_count != pack.series_count:
            problems.append(
                f"{self.channel_count} voltage channels configured but the pack is "
                f"{pack.series_count}S. Cell voltages will be read for "
                f"{self.channel_count} groups only -- add or remove channels in "
                f"rig_config.json to match."
            )
        # One thermistor per cell is the design intent, so the thermal map covers
        # the whole module. A shortfall means hot cells with no sensor on them.
        if self.sensor_count != pack.cell_count: #CHANGE THIS WHEN WE ADD THE INBETWEEN SENSORS FOR THE MODULES
            problems.append(
                f"{self.temp_bus_count} buses x {self.sensors_per_bus} sensors = "
                f"{self.sensor_count} thermistors, but the pack has "
                f"{pack.cell_count} cells ({pack.series_count}S{pack.parallel_count}P). "
                f"The thermal map will not cover every cell."
            )
        # A channel cannot be both the current input and a voltage tap. If it is,
        # one of the two readings is meaningless and it is impossible to tell
        # which from the data alone.
        if self.current_channel in self.voltage_channels:
            problems.append(
                f"Current channel {self.current_channel} is also listed as a voltage "
                f"channel; one of them is wrong."
            )
        # Repeated taps mean two series groups share a reading, so the difference
        # between them computes as zero volts -- a cell that always looks dead
        # flat, or one that never appears to discharge.
        duplicates = {c for c in self.voltage_channels
                      if self.voltage_channels.count(c) > 1}
        if duplicates:
            problems.append(f"Duplicate voltage channels: {', '.join(sorted(duplicates))}.")
        # A zero or negative period would make the DAQ loop spin without pausing.
        if self.sample_period_s <= 0:
            problems.append("Sample period must be greater than zero.")

        return problems


# GUI labels for the DAQ / Sensors tab. voltage_channels is a list, which the
# dialog renders as a comma-separated text box -- hence the unit hint.
DAQ_FIELD_LABELS = {
    'current_channel':       ("Current channel", ""),
    'voltage_channels':      ("Voltage channels", "comma separated"),
    'voltage_multiplier':    ("Divider multiplier", "x"),
    'current_amps_per_volt': ("Current transducer", "A/V"),
    'ai_min_volts':          ("Analog input min", "V"),
    'ai_max_volts':          ("Analog input max", "V"),
    'temp_bus_count':        ("OneWire bus count", "buses"),
    'sensors_per_bus':       ("Sensors per bus", "sensors"),
    'temp_baud_rate':        ("Temp sensor baud", "baud"),
    'sample_period_s':       ("DAQ sample period", "s"),
}


# ================= SAFETY LIMITS =================

@dataclass
class SafetyLimits:
    """Active trip thresholds. Defaults are derived from PackConfig.

    Set derive_from_pack True (the default) to keep these locked to the cell
    datasheet. Set it False only if you deliberately want values that differ.

    These are the numbers that stop a run. They are pushed to the logic process
    as a plain dict (see to_command_dict) and evaluated against every telemetry
    packet by evaluate_safety() in control_logic.py.
    """

    # When True, the six DERIVED_FIELDS below are recomputed from the cell
    # datasheet every time the config is loaded -- so swapping cells or changing
    # S/P automatically moves the limits, and a new cell can never inherit the
    # old one's ceiling.
    #
    # Editing a threshold in the GUI sidebar sets this to False automatically, so
    # a deliberate operator override survives. Hand-editing rig_config.json
    # without also setting this False means the edit IS discarded on load -- but
    # loudly, via derivation_conflicts() below, not silently.
    derive_from_pack: bool = True

    # Continuous operating current limit, in amps. NOT the trip point: the
    # over-current fault fires at max_amps + amp_buffer. Also used as the ceiling
    # the resistance calculation clamps against, so it shapes normal running as
    # well as faulting.
    max_amps: float = 180.0

    # Headroom between the operating limit and the actual over-current trip.
    # Exists so brief overshoot while the bank switches steps does not fault the
    # run. Note this is taken OUT of the cell rating during derivation, not added
    # on top -- see apply_pack_derivation.
    amp_buffer: float = 5.0

    # Over-temperature trip, in Celsius, checked against the hottest cell. Applies
    # in EVERY state, armed or not: cells getting hot with the rig idle means
    # something is wrong regardless of what the state machine believes.
    max_temp: float = 60.0

    # Module-total undervoltage trip, in volts. Checked only once ARMED -- before
    # that, a rig powered up without a battery reads 0 V and would latch a fault
    # that RESET could not clear.
    min_volts: float = 36.0

    # Display-only warning level for the voltage plot. Draws a dashed line on the
    # GUI chart and does NOTHING else -- it is deliberately absent from
    # to_command_dict() because the logic process has no warning tier, only hard
    # trips. Do not mistake this for a protective threshold.
    warn_volts: float = 38.0

    # Per-cell undervoltage trip. A module-total trip cannot see one weak cell:
    # eleven cells at 3.40 V plus one at 1.00 V totals 38.4 V, comfortably above
    # a 36.0 V module trip, while that one cell is destroyed.
    min_cell_volts: float = 2.70

    # Any cell reading below this is treated as a broken sense connection rather
    # than a real measurement -- a genuinely 0 V cell and an unplugged sense lead
    # look identical, and both must stop the run.
    #
    # Reported as CELL SENSE FAULT rather than CELL UNDERVOLTAGE. Both fault; the
    # separate name tells the operator to check wiring rather than the pack.
    cell_sense_floor: float = 0.50

    # Temperature readings older than this are refused. Without it, losing the
    # temperature link freezes the last reading and the overtemp trip can never
    # fire while the cells keep heating.
    #
    # Seconds. Must exceed the Arduino's full sensor sweep or normal operation
    # would look like a dead sensor.
    temp_stale_timeout_s: float = 3.0

    # Whole-packet staleness. The logic loop keeps running on the last packet
    # when the DAQ stops feeding it, so every other check would otherwise be
    # evaluating frozen values that still look plausible. The DAQ samples at
    # 10 Hz, so 1 s is roughly ten missed cycles.
    daq_stale_timeout_s: float = 1.0

    # Whether to progressively reduce the current limit as cells approach the
    # temperature ceiling, rather than running at full load until the hard trip.
    derate_enabled: bool = False

    # Temperature at which derating begins ramping, in Celsius. Between this and
    # max_temp the allowed current falls linearly to zero. Must stay BELOW
    # max_temp or the ramp has no range to act over -- apply_pack_derivation
    # enforces a 5 C gap and exceedances() warns if it is violated by hand.
    derate_start: float = 55.0

    def apply_pack_derivation(self, pack: PackConfig):
        """Recompute limits from the cell datasheet. No-op if derivation is off."""
        # Honour the opt-out first, so an operator override is never touched.
        if not self.derive_from_pack:
            return self

        # The E-STOP fires at max_amps + amp_buffer, so the buffer has to come
        # OUT of the rating, not sit on top of it. Deriving max_amps as the raw
        # pack rating would put the actual trip above what the cells are rated
        # for -- the original 182 A / 187 A bug. Floor at 1 A so an absurd
        # buffer cannot produce a zero or negative operating limit.
        self.max_amps = max(1.0, pack.max_current_a - self.amp_buffer)

        # Straight from the datasheet's discharge operating ceiling.
        self.max_temp = pack.cell_max_temp_c

        # 3.0 V/cell, above the absolute cutoff to leave room for IR sag.
        self.min_volts = pack.suggested_min_voltage()

        # Warning line sits 2.0 V/cell-equivalent above the trip, scaled to the
        # series count so it stays proportionate on a pack of any size.
        self.warn_volts = self.min_volts + (2.0 / 12.0) * pack.series_count

        # A margin above the absolute cutoff so an outlier cell trips before it
        # is damaged, not after.
        self.min_cell_volts = pack.cell_min_voltage + 0.20

        # Keep derate start below the ceiling so the derate range stays positive.
        # min() rather than an assignment so a user's more-conservative start
        # temperature is preserved; only an impossible one is pulled down.
        self.derate_start = min(self.derate_start, self.max_temp - 5.0)
        return self

    # Fields that apply_pack_derivation() writes. Kept next to it so the two
    # cannot drift apart.
    #
    # A unit test fails if derivation starts writing a field absent from this
    # tuple -- without that, a newly-derived field would begin silently reverting
    # hand edits again, which is precisely the bug derivation_conflicts() exists
    # to prevent.
    DERIVED_FIELDS = ('max_amps', 'max_temp', 'min_volts', 'warn_volts',
                      'min_cell_volts', 'derate_start')

    def derivation_conflicts(self, pack: PackConfig):
        """Values that pack derivation would overwrite, as (field, loaded, derived).

        Derivation is authoritative on purpose -- change the pack and the limits
        must follow, or a new cell inherits the old one's ceiling. But that means
        a hand-edited rig_config.json is silently reverted while
        derive_from_pack is left True, and the reversion can go in the unsafe
        direction: an engineer derating to 120 A gets 175 A back. Callers use
        this to say so out loud rather than let it pass unnoticed.
        """
        # Nothing to discard if derivation is switched off.
        if not self.derive_from_pack:
            return []

        # replace() makes a copy with the same field values, so derivation can be
        # run speculatively on the copy. Mutating self here would apply the very
        # overwrite we are trying to report on before the caller sees it.
        derived = replace(self)
        derived.apply_pack_derivation(pack)

        conflicts = []
        for name in self.DERIVED_FIELDS:
            loaded, after = getattr(self, name), getattr(derived, name)
            # Float tolerance rather than != : values that round-trip through
            # JSON can differ in the last bit without being a real edit.
            if abs(loaded - after) > 1e-9:
                conflicts.append((name, loaded, after))
        return conflicts

    def exceedances(self, pack: PackConfig):
        """Return human-readable warnings where limits exceed cell ratings.

        The E-STOP fires at max_amps + amp_buffer, so the buffer is checked as
        part of the current limit rather than ignored.
        """
        warnings = []

        # Check the value the trip ACTUALLY fires at, not the operating limit.
        # Checking max_amps alone was how a 182 A limit with a 5 A buffer passed
        # review while really tripping at 187 A, above the cells' 180 A rating.
        trip_current = self.max_amps + self.amp_buffer
        if trip_current > pack.max_current_a:
            warnings.append(
                f"Over-current trip at {trip_current:.1f} A exceeds the "
                f"{pack.max_current_a:.1f} A pack rating "
                f"({trip_current / pack.parallel_count:.2f} A/cell vs "
                f"{pack.cell_max_continuous_a:.1f} A rated)."
            )
        # Running hotter than the datasheet's discharge ceiling.
        if self.max_temp > pack.cell_max_temp_c:
            warnings.append(
                f"Max temp {self.max_temp:.1f} C exceeds the cell's "
                f"{pack.cell_max_temp_c:.1f} C discharge ceiling."
            )
        # A trip below the absolute cutoff can only fire after cells are damaged.
        if self.min_volts < pack.min_voltage:
            warnings.append(
                f"Undervoltage trip {self.min_volts:.1f} V is below the "
                f"{pack.min_voltage:.1f} V absolute cutoff -- cells would be "
                f"damaged before the trip fires."
            )
        # Same reasoning at the individual cell level.
        if self.min_cell_volts < pack.cell_min_voltage:
            warnings.append(
                f"Per-cell trip {self.min_cell_volts:.2f} V is below the cell's "
                f"{pack.cell_min_voltage:.2f} V cutoff -- a cell would be damaged "
                f"before the trip fires."
            )
        # A non-positive timeout disables the staleness check entirely, which
        # would let a dead temperature sensor go unnoticed indefinitely.
        if self.temp_stale_timeout_s <= 0:
            warnings.append(
                "Temperature staleness timeout must be positive, or a lost "
                "temperature link will never be detected."
            )
        # Derating needs a temperature band to ramp across.
        if self.derate_enabled and self.derate_start >= self.max_temp:
            warnings.append(
                f"Derate start {self.derate_start:.1f} C is not below max temp "
                f"{self.max_temp:.1f} C; derating cannot ramp."
            )
        return warnings

    def to_command_dict(self):
        """The payload shape control_logic expects from a SET_LIMITS command.

        A plain dict rather than the dataclass because it crosses a process
        boundary through a multiprocessing queue, and because the logic loop
        merges partial updates into it.

        evaluate_safety() indexes these keys DIRECTLY, so a missing one is a
        KeyError inside the safety-critical loop rather than a quietly skipped
        check. A unit test pins this exact key set for that reason.

        warn_volts is deliberately absent: it is a display-only plot line and the
        logic process has no warning tier to apply it to.
        """
        return {
            'max_amps': self.max_amps,
            'amp_buffer': self.amp_buffer,
            'max_temp': self.max_temp,
            'min_volts': self.min_volts,
            'min_cell_volts': self.min_cell_volts,
            'cell_sense_floor': self.cell_sense_floor,
            # Note the key names drop the _s suffix the fields carry -- the logic
            # process reads these names, so renaming either side breaks the pair.
            'temp_stale_timeout': self.temp_stale_timeout_s,
            'daq_stale_timeout': self.daq_stale_timeout_s,
            'derate_en': self.derate_enabled,
            'derate_start': self.derate_start,
        }


# ================= TOP-LEVEL CONFIG =================

@dataclass
class RigConfig:
    """The complete rig setup: one of each settings group, plus persistence.

    Every process (GUI, control logic, DAQ) calls RigConfig.load() independently
    at startup rather than sharing one instance, because they run in separate
    processes. Live changes reach the logic process as explicit SET_CONFIG /
    SET_LIMITS commands; the DAQ process only picks changes up on restart.
    """

    vehicle: VehicleParams   # the car being simulated
    pack: PackConfig         # the cells and their wiring
    limits: SafetyLimits     # the thresholds that stop a run
    daq: DaqConfig           # which channels and sensors are physically present

    @staticmethod
    def defaults():
        """A complete config from every dataclass's own defaults.

        Used on first run, when the config file is missing, and when it is too
        corrupt to parse.
        """
        pack = PackConfig()
        # Derive the limits from that pack immediately, so the defaults are
        # self-consistent rather than relying on the hardcoded SafetyLimits
        # numbers happening to match the default cell.
        limits = SafetyLimits().apply_pack_derivation(pack)
        return RigConfig(vehicle=VehicleParams(), pack=pack, limits=limits,
                         daq=DaqConfig())

    def to_dict(self):
        """Plain nested dict, ready for json.dump or a multiprocessing queue.

        asdict() recurses through each dataclass. Note @property values are NOT
        included -- only real fields -- which is correct: derived values would be
        stale the moment someone edited the JSON by hand.
        """
        return {
            'vehicle': asdict(self.vehicle),
            'pack': asdict(self.pack),
            'limits': asdict(self.limits),
            'daq': asdict(self.daq),
        }

    def validate(self):
        """All cross-cutting consistency problems, as human-readable strings."""
        # Two independent checks concatenated: limits versus what the cells can
        # take, and wiring versus what the pack needs. Both return lists, so an
        # empty result means the whole configuration is coherent.
        return self.limits.exceedances(self.pack) + self.daq.validate(self.pack)

    @staticmethod
    def from_dict(raw):
        """Build from a dict, ignoring unknown keys and filling missing ones.

        Tolerant by design: a config written by an older or newer version of the
        rig should still load rather than crashing the app on startup.
        """
        def build(cls, data):
            # Field names this dataclass actually accepts.
            valid = {f.name for f in fields(cls)}
            # Keep only recognised keys, so a config from a NEWER version with
            # extra fields still loads instead of raising TypeError. Absent keys
            # simply fall back to the dataclass defaults.
            # `data or {}` covers a missing section, where .get() returned None.
            return cls(**{k: v for k, v in (data or {}).items() if k in valid})

        pack = build(PackConfig, raw.get('pack'))
        limits = build(SafetyLimits, raw.get('limits'))

        # Capture what derivation is about to discard, before it discards it.
        conflicts = limits.derivation_conflicts(pack)
        limits.apply_pack_derivation(pack)

        cfg = RigConfig(
            vehicle=build(VehicleParams, raw.get('vehicle')),
            pack=pack,
            limits=limits,
            daq=build(DaqConfig, raw.get('daq')),
        )
        # Plain attribute, not a dataclass field, so it never reaches to_dict().
        # If it were a field it would be written back into rig_config.json and
        # then read as configuration on the next load.
        cfg.discarded_limits = conflicts
        return cfg

    def save(self, path=CONFIG_PATH):
        """Write the config to JSON. Returns the path written, for logging."""
        # indent=2 because this file is meant to be read and hand-edited.
        # encoding is explicit so the file is identical on any machine's locale.
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(self.to_dict(), fh, indent=2)
        return path

    @staticmethod
    def load(path=CONFIG_PATH):
        """Load config, falling back to defaults if the file is absent or broken.

        Never raises: a corrupt config must not stop the rig from starting.
        """
        # First run, or a fresh checkout with no saved settings yet.
        if not os.path.exists(path):
            return RigConfig.defaults()
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                cfg = RigConfig.from_dict(json.load(fh))
        except Exception as exc:
            # Deliberately broad: malformed JSON, permission errors and encoding
            # problems must all degrade to working defaults rather than leaving
            # a team unable to start the rig because of a stray comma.
            print(f"[CONFIG] Could not read {path} ({exc}); using defaults.")
            return RigConfig.defaults()

        # Say out loud when a hand-edited limit was replaced by the derived one.
        # getattr with a default because from_dict is the only thing that sets
        # this attribute, and a config built another way will not have it.
        for name, loaded, derived in getattr(cfg, 'discarded_limits', []):
            # :g formats without trailing zeros, so 120.0 prints as "120".
            print(f"[CONFIG WARNING] {name}={loaded:g} in {os.path.basename(path)} was "
                  f"replaced by the pack-derived {derived:g}. Set "
                  f"\"derive_from_pack\": false to keep hand-edited limits.")
        return cfg


def field_label(field_name, labels):
    """Display label and unit for a dataclass field.

    Used by the GUI when generating the Configure dialog. Falls back to a
    prettified field name so a newly added field still renders sensibly without
    anyone remembering to update the label tables above -- it just gets
    "Some new field" instead of a hand-written label and unit.
    """
    if field_name in labels:
        return labels[field_name]
    # "cell_max_temp_c" -> "Cell max temp c", and no unit.
    return (field_name.replace('_', ' ').capitalize(), "")
