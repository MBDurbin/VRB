"""
Unit tests for the rig configuration layer.

This is safety-relevant: SafetyLimits derives the over-current, over-temp and
undervoltage trip points from the cell datasheet, so a wrong derivation puts
wrong thresholds into the logic process. Also covers persistence, because a
config that fails to round-trip silently reverts a future team's settings.
"""
import json
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rig_config import (
    VehicleParams,
    PackConfig,
    SafetyLimits,
    RigConfig,
    field_label,
)


# ================= PACK DERIVATION =================

class TestPackDerivation:
    def test_defaults_match_p45b_datasheet(self):
        # Molicel INR-21700-P45B v1.2, 12S4P. These are the numbers validated
        # against the datasheet -- if one drifts, the rig is mis-rated.
        p = PackConfig()
        assert p.cell_count == 48
        assert math.isclose(p.capacity_ah, 18.0)          # 4.5 Ah x 4P
        assert math.isclose(p.max_current_a, 180.0)       # 45 A x 4P
        assert math.isclose(p.max_voltage, 50.4)          # 4.2 V x 12S
        assert math.isclose(p.min_voltage, 30.0)          # 2.5 V x 12S
        assert math.isclose(p.nominal_voltage, 43.2)      # 3.6 V x 12S
        assert math.isclose(p.resistance_ohm, 0.045)      # 15 mOhm / 4P x 12S

    def test_parallel_count_scales_current_and_capacity_not_voltage(self):
        p4 = PackConfig(parallel_count=4)
        p6 = PackConfig(parallel_count=6)

        assert math.isclose(p6.max_current_a, p4.max_current_a * 1.5)
        assert math.isclose(p6.capacity_ah, p4.capacity_ah * 1.5)
        assert math.isclose(p6.max_voltage, p4.max_voltage)

    def test_series_count_scales_voltage_not_current(self):
        s12 = PackConfig(series_count=12)
        s14 = PackConfig(series_count=14)

        assert math.isclose(s14.max_voltage, s12.max_voltage * 14 / 12)
        assert math.isclose(s14.max_current_a, s12.max_current_a)

    def test_internal_resistance_series_adds_parallel_divides(self):
        p = PackConfig(series_count=10, parallel_count=5, cell_dc_milliohm=20.0)
        # 20 mOhm / 5P = 4 mOhm per block, x10S = 40 mOhm
        assert math.isclose(p.resistance_ohm, 0.040)

    def test_minimum_capacity_option_is_more_conservative(self):
        typical = PackConfig(use_minimum_capacity=False)
        minimum = PackConfig(use_minimum_capacity=True)

        assert minimum.capacity_ah < typical.capacity_ah
        assert math.isclose(minimum.capacity_ah, 17.2)  # 4.3 Ah x 4P

    def test_sag_matches_ohms_law(self):
        p = PackConfig()
        assert math.isclose(p.sag_volts(180.0), 180.0 * 0.045)

    def test_voltage_under_load_reproduces_the_sag_hazard(self):
        # A pack resting at a healthy-looking 3.2 V/cell is pushed under the
        # 2.5 V/cell cutoff while loaded. This is why the trip sits above 30 V.
        p = PackConfig()
        loaded = p.voltage_under_load(ocv_per_cell=3.2, current_a=187.0)
        assert loaded < p.min_voltage
        assert math.isclose(loaded / p.series_count, 2.499, abs_tol=0.001)

    def test_suggested_min_voltage_is_above_absolute_cutoff(self):
        p = PackConfig()
        assert p.suggested_min_voltage() > p.min_voltage
        assert math.isclose(p.suggested_min_voltage(), 36.0)


# ================= LIMIT DERIVATION =================

class TestLimitDerivation:
    def test_derives_from_pack_by_default(self):
        pack = PackConfig()
        limits = SafetyLimits().apply_pack_derivation(pack)

        # Operating limit sits a buffer below the rating so the E-STOP, which
        # fires at max_amps + buffer, lands exactly ON the rating.
        assert math.isclose(limits.max_amps, 175.0)
        assert math.isclose(limits.max_amps + limits.amp_buffer, pack.max_current_a)
        assert math.isclose(limits.max_temp, 60.0)
        assert math.isclose(limits.min_volts, 36.0)

    def test_trip_point_never_exceeds_pack_rating(self):
        for buffer in [0.0, 5.0, 20.0, 100.0]:
            pack = PackConfig()
            limits = SafetyLimits(amp_buffer=buffer).apply_pack_derivation(pack)
            assert limits.max_amps + limits.amp_buffer <= pack.max_current_a + 1e-9

    def test_absurd_buffer_cannot_drive_limit_to_zero(self):
        pack = PackConfig()
        limits = SafetyLimits(amp_buffer=500.0).apply_pack_derivation(pack)
        assert limits.max_amps > 0

    def test_derivation_follows_a_different_cell(self):
        # A future team's cell: 30 A/cell, 55 C ceiling, 20S6P.
        pack = PackConfig(cell_max_continuous_a=30.0, cell_max_temp_c=55.0,
                          parallel_count=6, series_count=20)
        limits = SafetyLimits(amp_buffer=5.0).apply_pack_derivation(pack)

        assert math.isclose(limits.max_amps, 175.0)  # 30 A x 6P = 180, less 5 buffer
        assert math.isclose(limits.max_temp, 55.0)
        assert math.isclose(limits.min_volts, 60.0)  # 3.0 V x 20S

    def test_derivation_is_skipped_when_disabled(self):
        pack = PackConfig()
        limits = SafetyLimits(derive_from_pack=False, max_amps=99.0, max_temp=42.0)
        limits.apply_pack_derivation(pack)

        assert math.isclose(limits.max_amps, 99.0)
        assert math.isclose(limits.max_temp, 42.0)

    def test_derate_start_kept_below_ceiling(self):
        # Derate range must stay positive or the ramp cannot compute.
        pack = PackConfig(cell_max_temp_c=50.0)
        limits = SafetyLimits(derate_start=55.0).apply_pack_derivation(pack)
        assert limits.derate_start < limits.max_temp

    def test_command_dict_shape_matches_logic_process(self):
        keys = set(SafetyLimits().to_command_dict())
        assert keys == {'max_amps', 'amp_buffer', 'max_temp', 'min_volts',
                        'derate_en', 'derate_start'}


# ================= EXCEEDANCE WARNINGS =================

class TestExceedances:
    def test_derived_defaults_are_clean(self):
        pack = PackConfig()
        limits = SafetyLimits().apply_pack_derivation(pack)
        assert limits.exceedances(pack) == []

    def test_buffer_pushing_trip_over_rating_is_flagged(self):
        # The E-STOP fires at max_amps + buffer, so a limit exactly at the
        # rating still trips over it. This is the 182/187 A bug, generalised.
        pack = PackConfig()
        limits = SafetyLimits(derive_from_pack=False, max_amps=180.0, amp_buffer=5.0)
        warnings = limits.exceedances(pack)
        assert any("exceeds" in w and "A" in w for w in warnings)

    def test_zero_buffer_at_rating_is_clean(self):
        pack = PackConfig()
        limits = SafetyLimits(derive_from_pack=False, max_amps=180.0, amp_buffer=0.0)
        assert not any("pack rating" in w for w in limits.exceedances(pack))

    def test_overtemp_limit_is_flagged(self):
        pack = PackConfig()
        limits = SafetyLimits(derive_from_pack=False, max_temp=65.0, amp_buffer=0.0,
                              max_amps=180.0, min_volts=36.0)
        assert any("discharge ceiling" in w for w in limits.exceedances(pack))

    def test_undervoltage_below_absolute_cutoff_is_flagged(self):
        pack = PackConfig()
        limits = SafetyLimits(derive_from_pack=False, min_volts=28.0, amp_buffer=0.0,
                              max_amps=180.0, max_temp=60.0)
        assert any("absolute cutoff" in w for w in limits.exceedances(pack))

    def test_impossible_derate_range_is_flagged(self):
        pack = PackConfig()
        limits = SafetyLimits(derive_from_pack=False, derate_enabled=True,
                              derate_start=60.0, max_temp=60.0, amp_buffer=0.0,
                              max_amps=180.0, min_volts=36.0)
        assert any("derate" in w.lower() for w in limits.exceedances(pack))


# ================= PERSISTENCE =================

class TestPersistence:
    def test_round_trip_preserves_values(self):
        cfg = RigConfig.defaults()
        cfg.vehicle.mass_car_kg = 245.5
        cfg.vehicle.drag_coefficient = 0.42
        cfg.pack.cell_model = "Samsung INR21700-50E"
        cfg.pack.parallel_count = 6

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cfg.json")
            cfg.save(path)
            loaded = RigConfig.load(path)

        assert math.isclose(loaded.vehicle.mass_car_kg, 245.5)
        assert math.isclose(loaded.vehicle.drag_coefficient, 0.42)
        assert loaded.pack.cell_model == "Samsung INR21700-50E"
        assert loaded.pack.parallel_count == 6

    def test_missing_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            loaded = RigConfig.load(os.path.join(d, "nope.json"))
        assert math.isclose(loaded.pack.capacity_ah, 18.0)

    def test_corrupt_file_falls_back_rather_than_crashing(self):
        # A broken config must never stop the rig from starting.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bad.json")
            with open(path, 'w') as fh:
                fh.write("{ this is not json")
            loaded = RigConfig.load(path)
        assert math.isclose(loaded.pack.max_current_a, 180.0)

    def test_unknown_keys_are_ignored(self):
        # Forward compatibility: a config from a newer version still loads.
        raw = RigConfig.defaults().to_dict()
        raw['pack']['some_future_field'] = 123
        raw['vehicle']['another_one'] = "x"
        loaded = RigConfig.from_dict(raw)
        assert math.isclose(loaded.pack.capacity_ah, 18.0)

    def test_missing_keys_fall_back_to_field_defaults(self):
        loaded = RigConfig.from_dict({'pack': {'parallel_count': 8}})
        assert loaded.pack.parallel_count == 8
        assert math.isclose(loaded.pack.cell_max_continuous_a, 45.0)
        assert math.isclose(loaded.pack.max_current_a, 360.0)

    def test_empty_dict_yields_defaults(self):
        loaded = RigConfig.from_dict({})
        assert math.isclose(loaded.vehicle.total_mass_kg, 330.0)

    def test_loading_redderives_limits_for_changed_pack(self):
        # Someone edits the JSON to a bigger pack; limits must follow, not
        # silently keep the old pack's current ceiling.
        raw = RigConfig.defaults().to_dict()
        raw['pack']['parallel_count'] = 8
        loaded = RigConfig.from_dict(raw)
        assert math.isclose(loaded.limits.max_amps + loaded.limits.amp_buffer, 360.0)

    def test_saved_file_is_readable_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cfg.json")
            RigConfig.defaults().save(path)
            with open(path) as fh:
                raw = json.load(fh)
        assert set(raw) == {'vehicle', 'pack', 'limits'}


# ================= GUI FIELD LABELS =================

class TestFieldLabels:
    def test_known_field_has_label_and_unit(self):
        label, unit = field_label('mass_car_kg', {'mass_car_kg': ("Car mass", "kg")})
        assert label == "Car mass"
        assert unit == "kg"

    def test_unknown_field_falls_back_to_prettified_name(self):
        label, unit = field_label('some_new_param', {})
        assert label == "Some new param"
        assert unit == ""
