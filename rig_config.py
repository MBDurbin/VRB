"""
Rig configuration: vehicle model, battery pack, and safety limits.

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
"""
import json
import os
from dataclasses import dataclass, asdict, field, fields
from typing import List

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rig_config.json")


# ================= VEHICLE =================

@dataclass
class VehicleParams:
    """Vehicle and environment parameters for the road-load model.

    Retune the simulation by editing a field rather than the physics -- either
    mutate the active config, or build a variant and pass it in:

        fast = VehicleParams(drag_coefficient=0.55, mass_driver_kg=65.0)
        compute_required_power(v, a, params=fast)
    """

    # --- Mass ---
    mass_car_kg: float = 260.0
    mass_driver_kg: float = 70.0

    # Rotational inertia of wheels, brake rotors, axles and drivetrain, modelled
    # as extra "virtual" mass. Applies to acceleration ONLY: spinning components
    # resist changes in speed but do not press the tyres into the track, so this
    # must not feed the normal force behind rolling resistance.
    # Typical 1.04-1.10 for an open-wheel/student formula car.
    rotational_mass_factor: float = 1.05

    # --- Environment ---
    air_density_kgm3: float = 1.2255  # ISA sea level, 15 C
    gravity_ms2: float = 9.81

    # --- Aerodynamics ---
    # Drag and downforce carry separate reference areas: they are frequently the
    # same number, but a wing package can change one without the other.
    drag_coefficient: float = 0.6           # Cd
    drag_area_m2: float = 2.224             # reference area for drag
    lift_coefficient: float = -1.0          # Cl, negative = downforce
    downforce_area_m2: float = 2.224        # reference area for downforce

    # --- Tyres ---
    rolling_resistance_coeff: float = 0.015

    # --- Drivetrain ---
    # Mechanical efficiency between motor shaft and contact patch (chain, gears,
    # bearings). ~0.95 for a well-maintained chain drive.
    drivetrain_efficiency: float = 0.95

    @property
    def total_mass_kg(self) -> float:
        return self.mass_car_kg + self.mass_driver_kg


# Human-readable labels and units for GUI generation. Keys must match field
# names; any field missing here falls back to a prettified field name.
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
    """

    cell_model: str = "Molicel INR-21700-P45B"
    series_count: int = 12
    parallel_count: int = 4

    # Straight off the cell datasheet
    cell_capacity_ah: float = 4.5           # typical; see usable_capacity_ah note
    cell_capacity_min_ah: float = 4.3       # minimum / worst case
    cell_max_continuous_a: float = 45.0
    cell_max_voltage: float = 4.2
    cell_nominal_voltage: float = 3.6
    cell_min_voltage: float = 2.5           # absolute discharge cutoff
    cell_max_temp_c: float = 60.0           # discharge operating ceiling
    cell_dc_milliohm: float = 15.0          # DC impedance at 50% SOC

    # Use minimum rather than typical capacity for SOC. Conservative: a worst-case
    # pack really does hold less, and overstating SOC is the dangerous direction.
    use_minimum_capacity: bool = False

    # --- Derived pack values ---

    @property
    def cell_count(self) -> int:
        return self.series_count * self.parallel_count

    @property
    def capacity_ah(self) -> float:
        """Nameplate pack capacity used for coulomb counting."""
        per_cell = self.cell_capacity_min_ah if self.use_minimum_capacity else self.cell_capacity_ah
        return per_cell * self.parallel_count

    @property
    def max_current_a(self) -> float:
        return self.cell_max_continuous_a * self.parallel_count

    @property
    def max_voltage(self) -> float:
        return self.cell_max_voltage * self.series_count

    @property
    def nominal_voltage(self) -> float:
        return self.cell_nominal_voltage * self.series_count

    @property
    def min_voltage(self) -> float:
        """Absolute floor. The undervoltage trip should sit ABOVE this to leave
        room for IR sag under load -- see suggested_min_voltage()."""
        return self.cell_min_voltage * self.series_count

    @property
    def resistance_ohm(self) -> float:
        """Pack DC internal resistance: series adds, parallel divides."""
        return (self.cell_dc_milliohm / 1000.0) * self.series_count / self.parallel_count

    @property
    def energy_wh(self) -> float:
        return self.capacity_ah * self.nominal_voltage

    def sag_volts(self, current_a: float) -> float:
        """Voltage drop across pack internal resistance at a given current."""
        return current_a * self.resistance_ohm

    def voltage_under_load(self, ocv_per_cell: float, current_a: float) -> float:
        return (ocv_per_cell * self.series_count) - self.sag_volts(current_a)

    def suggested_min_voltage(self, per_cell_trip: float = 3.0) -> float:
        """Undervoltage trip point, defaulting to 3.0 V/cell.

        Deliberately above the absolute cutoff: at max current this pack sags
        enough that a cell resting at a healthy-looking voltage can be pushed
        under its cutoff while loaded. Tripping at the absolute floor would only
        ever fire after the damage was done.
        """
        return per_cell_trip * self.series_count


PACK_FIELD_LABELS = {
    'cell_model':            ("Cell model", ""),
    'series_count':          ("Series count (S)", "cells"),
    'parallel_count':        ("Parallel count (P)", "cells"),
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

DEFAULT_VOLTAGE_CHANNELS = [
    "cDAQ1Mod8/ai1", "cDAQ1Mod8/ai2", "cDAQ1Mod8/ai3",
    "cDAQ1Mod7/ai0", "cDAQ1Mod7/ai1", "cDAQ1Mod7/ai2", "cDAQ1Mod7/ai3",
    "cDAQ1Mod6/ai0", "cDAQ1Mod6/ai1", "cDAQ1Mod6/ai2", "cDAQ1Mod6/ai3",
    "cDAQ1Mod5/ai0",
]


@dataclass
class DaqConfig:
    """NI-DAQ channel mapping, scaling, and temperature bus layout.

    Channel names and sensor counts describe physical wiring, so they cannot be
    derived from the pack's series/parallel counts -- but they must AGREE with
    them. validate() reports any disagreement; the DAQ process prints those
    warnings at startup rather than failing silently or crashing on an index.

    One voltage channel per series group, wired cumulatively: channel i reads
    the sum of cells 1..i+1, so per-cell voltages come from differencing.
    """

    # --- Channel mapping ---
    current_channel: str = "cDAQ1Mod8/ai0"
    voltage_channels: List[str] = field(
        default_factory=lambda: list(DEFAULT_VOLTAGE_CHANNELS))

    # --- Scaling ---
    # Resistor divider on each tap: a 10:1 divider means multiply by 11.
    voltage_multiplier: float = 11.0
    # Current transducer output, amps per volt at the DAQ input.
    current_amps_per_volt: float = 100.0

    # --- Analog input range ---
    ai_min_volts: float = -10.0
    ai_max_volts: float = 10.0

    # --- Temperature sensor layout ---
    # DS18B20s on OneWire buses. The Arduino emits one CSV line per bus:
    # "<bus number>,<t1>,...,<tN>", so a line carries sensors_per_bus + 1 fields.
    temp_bus_count: int = 6
    sensors_per_bus: int = 8
    temp_baud_rate: int = 115200

    # --- Loop timing ---
    sample_period_s: float = 0.1

    @property
    def channel_count(self) -> int:
        return len(self.voltage_channels)

    @property
    def sensor_count(self) -> int:
        return self.temp_bus_count * self.sensors_per_bus

    @property
    def temp_fields_per_line(self) -> int:
        return self.sensors_per_bus + 1

    def validate(self, pack: PackConfig):
        """Report mismatches between the wiring and the configured pack."""
        problems = []

        if self.channel_count != pack.series_count:
            problems.append(
                f"{self.channel_count} voltage channels configured but the pack is "
                f"{pack.series_count}S. Cell voltages will be read for "
                f"{self.channel_count} groups only -- add or remove channels in "
                f"rig_config.json to match."
            )
        if self.sensor_count != pack.cell_count:
            problems.append(
                f"{self.temp_bus_count} buses x {self.sensors_per_bus} sensors = "
                f"{self.sensor_count} thermistors, but the pack has "
                f"{pack.cell_count} cells ({pack.series_count}S{pack.parallel_count}P). "
                f"The thermal map will not cover every cell."
            )
        if self.current_channel in self.voltage_channels:
            problems.append(
                f"Current channel {self.current_channel} is also listed as a voltage "
                f"channel; one of them is wrong."
            )
        duplicates = {c for c in self.voltage_channels
                      if self.voltage_channels.count(c) > 1}
        if duplicates:
            problems.append(f"Duplicate voltage channels: {', '.join(sorted(duplicates))}.")
        if self.sample_period_s <= 0:
            problems.append("Sample period must be greater than zero.")

        return problems


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
    """

    derive_from_pack: bool = True

    max_amps: float = 180.0
    amp_buffer: float = 5.0
    max_temp: float = 60.0
    min_volts: float = 36.0
    warn_volts: float = 38.0

    derate_enabled: bool = False
    derate_start: float = 55.0

    def apply_pack_derivation(self, pack: PackConfig):
        """Recompute limits from the cell datasheet. No-op if derivation is off."""
        if not self.derive_from_pack:
            return self

        # The E-STOP fires at max_amps + amp_buffer, so the buffer has to come
        # OUT of the rating, not sit on top of it. Deriving max_amps as the raw
        # pack rating would put the actual trip above what the cells are rated
        # for -- the original 182 A / 187 A bug. Floor at 1 A so an absurd
        # buffer cannot produce a zero or negative operating limit.
        self.max_amps = max(1.0, pack.max_current_a - self.amp_buffer)
        self.max_temp = pack.cell_max_temp_c
        self.min_volts = pack.suggested_min_voltage()
        self.warn_volts = self.min_volts + (2.0 / 12.0) * pack.series_count
        # Keep derate start below the ceiling so the derate range stays positive.
        self.derate_start = min(self.derate_start, self.max_temp - 5.0)
        return self

    def exceedances(self, pack: PackConfig):
        """Return human-readable warnings where limits exceed cell ratings.

        The E-STOP fires at max_amps + amp_buffer, so the buffer is checked as
        part of the current limit rather than ignored.
        """
        warnings = []
        trip_current = self.max_amps + self.amp_buffer
        if trip_current > pack.max_current_a:
            warnings.append(
                f"Over-current trip at {trip_current:.1f} A exceeds the "
                f"{pack.max_current_a:.1f} A pack rating "
                f"({trip_current / pack.parallel_count:.2f} A/cell vs "
                f"{pack.cell_max_continuous_a:.1f} A rated)."
            )
        if self.max_temp > pack.cell_max_temp_c:
            warnings.append(
                f"Max temp {self.max_temp:.1f} C exceeds the cell's "
                f"{pack.cell_max_temp_c:.1f} C discharge ceiling."
            )
        if self.min_volts < pack.min_voltage:
            warnings.append(
                f"Undervoltage trip {self.min_volts:.1f} V is below the "
                f"{pack.min_voltage:.1f} V absolute cutoff -- cells would be "
                f"damaged before the trip fires."
            )
        if self.derate_enabled and self.derate_start >= self.max_temp:
            warnings.append(
                f"Derate start {self.derate_start:.1f} C is not below max temp "
                f"{self.max_temp:.1f} C; derating cannot ramp."
            )
        return warnings

    def to_command_dict(self):
        """The payload shape control_logic expects from a SET_LIMITS command."""
        return {
            'max_amps': self.max_amps,
            'amp_buffer': self.amp_buffer,
            'max_temp': self.max_temp,
            'min_volts': self.min_volts,
            'derate_en': self.derate_enabled,
            'derate_start': self.derate_start,
        }


# ================= TOP-LEVEL CONFIG =================

@dataclass
class RigConfig:
    vehicle: VehicleParams
    pack: PackConfig
    limits: SafetyLimits
    daq: DaqConfig

    @staticmethod
    def defaults():
        pack = PackConfig()
        limits = SafetyLimits().apply_pack_derivation(pack)
        return RigConfig(vehicle=VehicleParams(), pack=pack, limits=limits,
                         daq=DaqConfig())

    def to_dict(self):
        return {
            'vehicle': asdict(self.vehicle),
            'pack': asdict(self.pack),
            'limits': asdict(self.limits),
            'daq': asdict(self.daq),
        }

    def validate(self):
        """All cross-cutting consistency problems, as human-readable strings."""
        return self.limits.exceedances(self.pack) + self.daq.validate(self.pack)

    @staticmethod
    def from_dict(raw):
        """Build from a dict, ignoring unknown keys and filling missing ones.

        Tolerant by design: a config written by an older or newer version of the
        rig should still load rather than crashing the app on startup.
        """
        def build(cls, data):
            valid = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in (data or {}).items() if k in valid})

        pack = build(PackConfig, raw.get('pack'))
        limits = build(SafetyLimits, raw.get('limits'))
        limits.apply_pack_derivation(pack)
        return RigConfig(
            vehicle=build(VehicleParams, raw.get('vehicle')),
            pack=pack,
            limits=limits,
            daq=build(DaqConfig, raw.get('daq')),
        )

    def save(self, path=CONFIG_PATH):
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(self.to_dict(), fh, indent=2)
        return path

    @staticmethod
    def load(path=CONFIG_PATH):
        """Load config, falling back to defaults if the file is absent or broken.

        Never raises: a corrupt config must not stop the rig from starting.
        """
        if not os.path.exists(path):
            return RigConfig.defaults()
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                return RigConfig.from_dict(json.load(fh))
        except Exception as exc:
            print(f"[CONFIG] Could not read {path} ({exc}); using defaults.")
            return RigConfig.defaults()


def field_label(field_name, labels):
    """Display label and unit for a dataclass field."""
    if field_name in labels:
        return labels[field_name]
    return (field_name.replace('_', ' ').capitalize(), "")
