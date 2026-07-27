"""
Unit tests for the pure safety/physics logic extracted from control_logic.py.

These exercise the decision logic that keeps the resistor bank/battery safe
(over-temp/over-current trip, FSM transition guards, thermal derate, coulomb
counting) without needing a GUI, Arduino, or DAQ attached. Run with:

    pytest tests/test_control_logic.py -v
"""
import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from control_logic import (
    check_safety_trip,
    coulomb_step,
    compute_lap_physics,
    compute_required_power,
    compute_road_load_forces,
    compute_target_resistance,
    resistance_to_steps,
    is_valid_transition,
    VehicleParams,
    MAX_RESISTANCE,
    RESISTOR_RESOLUTION,
)


# ================= SAFETY TRIP =================

class TestSafetyTrip:
    """Thresholds here mirror the Molicel INR-21700-P45B v1.2 datasheet for 12S4P:
    180 A continuous (45 A/cell x 4P), 60 C discharge ceiling, 36.0 V undervoltage
    trip (3.0 V/cell, above the 30.0 V absolute cutoff).
    """

    LIMITS = dict(max_safe_temp=60.0, max_safe_current=180.0,
                  current_buffer=5.0, min_safe_voltage=36.0)

    def test_nominal_no_trip(self):
        is_fault, reason = check_safety_trip(max_temp=40.0, amps=100.0, voltage=48.0,
                                              **self.LIMITS)
        assert is_fault is False
        assert reason is None

    def test_just_under_temp_threshold_no_trip(self):
        is_fault, _ = check_safety_trip(max_temp=59.99, amps=0.0, voltage=48.0,
                                         **self.LIMITS)
        assert is_fault is False

    def test_at_temp_threshold_trips(self):
        is_fault, reason = check_safety_trip(max_temp=60.0, amps=0.0, voltage=48.0,
                                              **self.LIMITS)
        assert is_fault is True
        assert reason == "OVERTEMP"

    def test_over_temp_threshold_trips(self):
        is_fault, reason = check_safety_trip(max_temp=70.0, amps=0.0, voltage=48.0,
                                              **self.LIMITS)
        assert is_fault is True
        assert reason == "OVERTEMP"

    def test_current_within_buffer_no_trip(self):
        # max=180, buffer=5 -> trips at 185
        is_fault, _ = check_safety_trip(max_temp=0.0, amps=184.99, voltage=48.0,
                                         **self.LIMITS)
        assert is_fault is False

    def test_current_at_buffered_limit_trips(self):
        is_fault, reason = check_safety_trip(max_temp=0.0, amps=185.0, voltage=48.0,
                                              **self.LIMITS)
        assert is_fault is True
        assert reason == "OVERCURRENT"

    def test_datasheet_continuous_rating_is_not_exceeded_silently(self):
        # 4P x 45 A/cell = 180 A. Anything at or past limit+buffer must fault.
        is_fault, reason = check_safety_trip(max_temp=25.0, amps=187.0, voltage=48.0,
                                              **self.LIMITS)
        assert is_fault is True
        assert reason == "OVERCURRENT"

    # --- Undervoltage (2.5 V/cell = 30.0 V absolute cutoff for 12S) ---

    def test_nominal_voltage_no_trip(self):
        is_fault, _ = check_safety_trip(max_temp=25.0, amps=50.0, voltage=43.2,
                                         **self.LIMITS)
        assert is_fault is False

    def test_just_above_undervoltage_no_trip(self):
        is_fault, _ = check_safety_trip(max_temp=25.0, amps=50.0, voltage=36.01,
                                         **self.LIMITS)
        assert is_fault is False

    def test_at_undervoltage_threshold_trips(self):
        is_fault, reason = check_safety_trip(max_temp=25.0, amps=50.0, voltage=36.0,
                                              **self.LIMITS)
        assert is_fault is True
        assert reason == "UNDERVOLTAGE"

    def test_sagged_below_cell_cutoff_trips(self):
        # 45 mohm pack IR at 187 A sags 8.4 V; a 3.2 V/cell pack lands at 2.499 V/cell.
        is_fault, reason = check_safety_trip(max_temp=25.0, amps=100.0, voltage=29.99,
                                              **self.LIMITS)
        assert is_fault is True
        assert reason == "UNDERVOLTAGE"

    def test_zero_voltage_reading_faults_rather_than_running(self):
        # Lost/failed voltage sensing must fail safe, not be treated as healthy.
        is_fault, reason = check_safety_trip(max_temp=25.0, amps=0.0, voltage=0.0,
                                              **self.LIMITS)
        assert is_fault is True
        assert reason == "UNDERVOLTAGE"

    # --- Priority ordering ---

    def test_temp_outranks_current(self):
        is_fault, reason = check_safety_trip(max_temp=100.0, amps=300.0, voltage=48.0,
                                              **self.LIMITS)
        assert is_fault is True
        assert reason == "OVERTEMP"

    def test_current_outranks_undervoltage(self):
        is_fault, reason = check_safety_trip(max_temp=25.0, amps=300.0, voltage=20.0,
                                              **self.LIMITS)
        assert is_fault is True
        assert reason == "OVERCURRENT"


# ================= FSM TRANSITION GUARDS =================

class TestFSMTransitions:
    def test_arm_only_from_idle(self):
        assert is_valid_transition("IDLE", "ARM") is True
        for bad_state in ["DISCONNECTED", "ARMED", "RUNNING", "FAULT"]:
            assert is_valid_transition(bad_state, "ARM") is False

    def test_run_only_from_armed(self):
        assert is_valid_transition("ARMED", "RUN") is True
        for bad_state in ["DISCONNECTED", "IDLE", "RUNNING", "FAULT"]:
            assert is_valid_transition(bad_state, "RUN") is False

    def test_reset_only_from_fault(self):
        assert is_valid_transition("FAULT", "RESET") is True
        for bad_state in ["DISCONNECTED", "IDLE", "ARMED", "RUNNING"]:
            assert is_valid_transition(bad_state, "RESET") is False

    def test_stop_always_allowed(self):
        for state in ["DISCONNECTED", "IDLE", "ARMED", "RUNNING", "FAULT"]:
            assert is_valid_transition(state, "STOP") is True

    def test_unknown_command_rejected(self):
        assert is_valid_transition("IDLE", "NOT_A_REAL_COMMAND") is False


# ================= COULOMB COUNTING =================

class TestCoulombCounting:
    def test_zero_current_no_drain(self):
        remaining, soc = coulomb_step(amps=0.0, dt=10.0, remaining_ah=18.0, total_capacity_ah=18.0)
        assert remaining == 18.0
        assert soc == 100.0

    def test_drains_correct_amount(self):
        # 18A for 3600s (1hr) = 18 Ah consumed
        remaining, soc = coulomb_step(amps=18.0, dt=3600.0, remaining_ah=18.0, total_capacity_ah=18.0)
        assert math.isclose(remaining, 0.0, abs_tol=1e-9)
        assert soc == 0.0

    def test_soc_clamped_at_zero_when_over_consumed(self):
        remaining, soc = coulomb_step(amps=100.0, dt=3600.0, remaining_ah=1.0, total_capacity_ah=18.0)
        assert remaining < 0.0  # remaining_ah itself is allowed to go negative (tracks overdraw)
        assert soc == 0.0  # but SOC display is clamped

    def test_soc_clamped_at_hundred(self):
        # Regen/negative current edge case shouldn't report >100%
        remaining, soc = coulomb_step(amps=-5.0, dt=3600.0, remaining_ah=18.0, total_capacity_ah=18.0)
        assert soc == 100.0


# ================= LAP PHYSICS =================

class TestLapPhysics:
    def test_first_row_has_zero_acceleration(self):
        row = {"Speed (mph)": 30.0, "Time (s)": 0.0}
        velocity_ms, accel, t = compute_lap_physics(row, prev_velocity_ms=0.0, prev_time_s=0.0,
                                                      is_first_row=True, row_idx=0)
        assert accel == 0.0
        assert math.isclose(velocity_ms, 30.0 * 0.44704, rel_tol=1e-9)

    def test_accelerating_row_has_positive_acceleration(self):
        row = {"Speed (mph)": 40.0, "Time (s)": 1.0}
        velocity_ms, accel, t = compute_lap_physics(row, prev_velocity_ms=30.0 * 0.44704,
                                                      prev_time_s=0.0, is_first_row=False, row_idx=1)
        assert accel > 0.0

    def test_non_positive_dt_falls_back_to_one_second(self):
        # Duplicate/out-of-order timestamps shouldn't divide by zero or go negative.
        row = {"Speed (mph)": 40.0, "Time (s)": 5.0}
        velocity_ms, accel, t = compute_lap_physics(row, prev_velocity_ms=30.0 * 0.44704,
                                                      prev_time_s=5.0, is_first_row=False, row_idx=1)
        expected_dv = (40.0 * 0.44704) - (30.0 * 0.44704)
        assert math.isclose(accel, expected_dv / 1.0, rel_tol=1e-9)

    def test_missing_columns_default_safely(self):
        row = {}
        velocity_ms, accel, t = compute_lap_physics(row, prev_velocity_ms=0.0, prev_time_s=0.0,
                                                      is_first_row=True, row_idx=3)
        assert velocity_ms == 0.0
        assert t == 3.0  # falls back to row_idx


# ================= RESISTANCE TARGETING =================

class TestResistanceTargeting:
    def test_zero_power_requests_max_resistance(self):
        req_r = compute_target_resistance(voltage=48.0, req_power=0.0, max_safe_current=180.0,
                                           current_max_temp=25.0, derate_enabled=False,
                                           derate_start_temp=55.0, max_safe_temp=60.0)
        assert req_r == MAX_RESISTANCE

    def test_never_exceeds_max_resistance(self):
        req_r = compute_target_resistance(voltage=48.0, req_power=0.001, max_safe_current=180.0,
                                           current_max_temp=25.0, derate_enabled=False,
                                           derate_start_temp=55.0, max_safe_temp=60.0)
        assert req_r <= MAX_RESISTANCE

    def test_never_drops_below_current_limit_clamp(self):
        # Huge power demand should be clamped by max current, not driven to a tiny resistance.
        req_r = compute_target_resistance(voltage=48.0, req_power=1_000_000.0, max_safe_current=180.0,
                                           current_max_temp=25.0, derate_enabled=False,
                                           derate_start_temp=55.0, max_safe_temp=60.0)
        expected_floor = 48.0 / 180.0
        assert math.isclose(req_r, expected_floor, rel_tol=1e-6)

    def test_derate_disabled_ignores_high_temp(self):
        no_derate = compute_target_resistance(voltage=48.0, req_power=1000.0, max_safe_current=180.0,
                                                current_max_temp=59.0, derate_enabled=False,
                                                derate_start_temp=55.0, max_safe_temp=60.0)
        with_derate = compute_target_resistance(voltage=48.0, req_power=1000.0, max_safe_current=180.0,
                                                  current_max_temp=59.0, derate_enabled=True,
                                                  derate_start_temp=55.0, max_safe_temp=60.0)
        assert with_derate >= no_derate

    def test_derate_below_start_temp_is_noop(self):
        below = compute_target_resistance(voltage=48.0, req_power=1000.0, max_safe_current=180.0,
                                            current_max_temp=50.0, derate_enabled=True,
                                            derate_start_temp=55.0, max_safe_temp=60.0)
        disabled = compute_target_resistance(voltage=48.0, req_power=1000.0, max_safe_current=180.0,
                                              current_max_temp=50.0, derate_enabled=False,
                                              derate_start_temp=55.0, max_safe_temp=60.0)
        assert math.isclose(below, disabled, rel_tol=1e-9)

    def test_derate_at_max_temp_forces_min_current(self):
        # Huge req_power pushes the un-derated resistance down near the current-limit
        # clamp (48/180 ~= 0.27 ohm); full derate should force it up to the 1.0A floor (48 ohm).
        req_r = compute_target_resistance(voltage=48.0, req_power=1_000_000.0, max_safe_current=180.0,
                                           current_max_temp=60.0, derate_enabled=True,
                                           derate_start_temp=55.0, max_safe_temp=60.0)
        assert math.isclose(req_r, 48.0, rel_tol=1e-6)

    def test_misconfigured_derate_thresholds_do_not_crash(self):
        # derate_start_temp >= max_safe_temp used to raise ZeroDivisionError and
        # crash the safety-critical process. It must now fail safe (full derate) instead.
        req_r = compute_target_resistance(voltage=48.0, req_power=1_000_000.0, max_safe_current=180.0,
                                           current_max_temp=65.0, derate_enabled=True,
                                           derate_start_temp=60.0, max_safe_temp=60.0)
        assert math.isclose(req_r, 48.0, rel_tol=1e-6)

        req_r2 = compute_target_resistance(voltage=48.0, req_power=1_000_000.0, max_safe_current=180.0,
                                            current_max_temp=70.0, derate_enabled=True,
                                            derate_start_temp=65.0, max_safe_temp=60.0)
        assert math.isclose(req_r2, 48.0, rel_tol=1e-6)


# ================= REQUIRED POWER =================

class TestRequiredPower:
    def test_zero_velocity_zero_power(self):
        assert compute_required_power(velocity_ms=0.0, acceleration=0.0) == 0.0

    def test_higher_speed_needs_more_power_at_zero_accel(self):
        low = compute_required_power(velocity_ms=10.0, acceleration=0.0)
        high = compute_required_power(velocity_ms=30.0, acceleration=0.0)
        assert high > low

    def test_braking_can_reduce_required_power(self):
        cruise = compute_required_power(velocity_ms=20.0, acceleration=0.0)
        braking = compute_required_power(velocity_ms=20.0, acceleration=-5.0)
        assert braking < cruise


# ================= ROTATIONAL MASS =================

class TestRotationalMass:
    def test_factor_increases_acceleration_force(self):
        plain = VehicleParams(rotational_mass_factor=1.0)
        spun = VehicleParams(rotational_mass_factor=1.05)

        f_plain = compute_road_load_forces(20.0, 3.0, plain)['accel']
        f_spun = compute_road_load_forces(20.0, 3.0, spun)['accel']

        assert f_spun > f_plain
        assert math.isclose(f_spun / f_plain, 1.05, rel_tol=1e-9)

    def test_factor_scales_exactly_with_total_mass(self):
        p = VehicleParams(mass_car_kg=260.0, mass_driver_kg=70.0,
                          rotational_mass_factor=1.05)
        f = compute_road_load_forces(20.0, 2.0, p)['accel']
        assert math.isclose(f, 330.0 * 1.05 * 2.0, rel_tol=1e-9)

    def test_no_effect_at_steady_speed(self):
        # Spinning components resist speed *changes* only.
        plain = VehicleParams(rotational_mass_factor=1.0)
        spun = VehicleParams(rotational_mass_factor=1.10)

        assert math.isclose(compute_required_power(25.0, 0.0, plain),
                            compute_required_power(25.0, 0.0, spun), rel_tol=1e-12)

    def test_does_not_inflate_rolling_resistance(self):
        # The factor must not reach the normal force -- rotating mass does not
        # press the tyres into the track.
        plain = VehicleParams(rotational_mass_factor=1.0)
        spun = VehicleParams(rotational_mass_factor=1.10)

        assert math.isclose(compute_road_load_forces(20.0, 5.0, plain)['rolling'],
                            compute_road_load_forces(20.0, 5.0, spun)['rolling'],
                            rel_tol=1e-12)

    def test_increases_power_demand_while_accelerating(self):
        plain = VehicleParams(rotational_mass_factor=1.0)
        spun = VehicleParams(rotational_mass_factor=1.05)
        assert compute_required_power(20.0, 4.0, spun) > compute_required_power(20.0, 4.0, plain)


# ================= DRIVETRAIN EFFICIENCY =================

class TestDrivetrainEfficiency:
    def test_driving_power_is_scaled_up(self):
        lossless = VehicleParams(drivetrain_efficiency=1.0)
        lossy = VehicleParams(drivetrain_efficiency=0.95)

        p_lossless = compute_required_power(25.0, 1.0, lossless)
        p_lossy = compute_required_power(25.0, 1.0, lossy)

        assert p_lossless > 0
        assert p_lossy > p_lossless
        assert math.isclose(p_lossy, p_lossless / 0.95, rel_tol=1e-9)

    def test_braking_power_is_scaled_down_in_magnitude(self):
        lossless = VehicleParams(drivetrain_efficiency=1.0)
        lossy = VehicleParams(drivetrain_efficiency=0.95)

        p_lossless = compute_required_power(20.0, -8.0, lossless)
        p_lossy = compute_required_power(20.0, -8.0, lossy)

        assert p_lossless < 0  # actually braking
        assert p_lossy > p_lossless          # closer to zero
        assert abs(p_lossy) < abs(p_lossless)  # less energy recovered
        assert math.isclose(p_lossy, p_lossless * 0.95, rel_tol=1e-9)

    def test_unity_efficiency_is_a_noop(self):
        unity = VehicleParams(drivetrain_efficiency=1.0)
        forces = compute_road_load_forces(22.0, 1.5, unity)
        assert math.isclose(compute_required_power(22.0, 1.5, unity),
                            forces['total'] * 22.0, rel_tol=1e-12)

    def test_zero_efficiency_does_not_divide_by_zero(self):
        # A mistyped efficiency must not crash the safety-critical process.
        broken = VehicleParams(drivetrain_efficiency=0.0)
        result = compute_required_power(20.0, 2.0, broken)
        assert math.isfinite(result)

    def test_negative_efficiency_does_not_flip_sign(self):
        broken = VehicleParams(drivetrain_efficiency=-0.5)
        driving = compute_required_power(20.0, 2.0, broken)
        assert driving > 0  # still a discharge, not a phantom regen


# ================= PARAMETERISATION =================

class TestVehicleParams:
    def test_defaults_match_documented_vehicle(self):
        p = VehicleParams()
        assert p.total_mass_kg == 330.0
        assert p.rotational_mass_factor == 1.05
        assert p.drivetrain_efficiency == 0.95

    def test_drag_coefficient_is_tunable(self):
        slippery = VehicleParams(drag_coefficient=0.3)
        draggy = VehicleParams(drag_coefficient=0.9)
        assert (compute_road_load_forces(30.0, 0.0, draggy)['drag'] >
                compute_road_load_forces(30.0, 0.0, slippery)['drag'])

    def test_drag_area_is_tunable_independently_of_downforce_area(self):
        base = VehicleParams()
        wide = VehicleParams(drag_area_m2=base.drag_area_m2 * 2)

        f_base = compute_road_load_forces(30.0, 0.0, base)
        f_wide = compute_road_load_forces(30.0, 0.0, wide)

        assert math.isclose(f_wide['drag'], f_base['drag'] * 2, rel_tol=1e-9)
        assert math.isclose(f_wide['downforce'], f_base['downforce'], rel_tol=1e-12)

    def test_downforce_raises_rolling_resistance_with_speed(self):
        winged = VehicleParams(lift_coefficient=-2.0)
        flat = VehicleParams(lift_coefficient=0.0)
        assert (compute_road_load_forces(30.0, 0.0, winged)['rolling'] >
                compute_road_load_forces(30.0, 0.0, flat)['rolling'])

    def test_rolling_coefficient_is_tunable(self):
        sticky = VehicleParams(rolling_resistance_coeff=0.030)
        slick = VehicleParams(rolling_resistance_coeff=0.010)
        assert (compute_road_load_forces(15.0, 0.0, sticky)['rolling'] >
                compute_road_load_forces(15.0, 0.0, slick)['rolling'])

    def test_air_density_is_tunable(self):
        sea = VehicleParams(air_density_kgm3=1.2255)
        altitude = VehicleParams(air_density_kgm3=1.0)
        assert (compute_road_load_forces(30.0, 0.0, sea)['drag'] >
                compute_road_load_forces(30.0, 0.0, altitude)['drag'])

    def test_mass_is_tunable(self):
        light = VehicleParams(mass_car_kg=200.0)
        heavy = VehicleParams(mass_car_kg=320.0)
        assert heavy.total_mass_kg > light.total_mass_kg
        assert (compute_road_load_forces(20.0, 2.0, heavy)['accel'] >
                compute_road_load_forces(20.0, 2.0, light)['accel'])

    def test_omitting_params_uses_module_default(self):
        explicit = compute_required_power(20.0, 1.0, VehicleParams())
        implicit = compute_required_power(20.0, 1.0)
        assert math.isclose(explicit, implicit, rel_tol=1e-12)

    def test_force_terms_sum_to_total(self):
        f = compute_road_load_forces(24.0, 1.7, VehicleParams())
        assert math.isclose(f['total'], f['drag'] + f['rolling'] + f['accel'], rel_tol=1e-12)


# ================= STEP CONVERSION =================

class TestResistanceToSteps:
    def test_below_resolution_floors_to_one_step(self):
        assert resistance_to_steps(0.0) == 1

    def test_matches_resolution_grid(self):
        assert resistance_to_steps(RESISTOR_RESOLUTION) == 1
        assert resistance_to_steps(RESISTOR_RESOLUTION * 4) == 4

    def test_max_resistance_step_count(self):
        assert resistance_to_steps(MAX_RESISTANCE) == round(MAX_RESISTANCE / RESISTOR_RESOLUTION)
