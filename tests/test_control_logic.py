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
    check_thermal_and_current,
    check_cell_safety,
    check_sensor_health,
    check_daq_health,
    evaluate_safety,
    lap_row_interval,
    load_lap_profile,
    NEUTRAL_PACKET,
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


# ================= THERMAL & CURRENT TRIPS =================

class TestThermalAndCurrentTrips:
    """The two trips that apply in every FSM state, armed or not.

    Thresholds mirror the Molicel INR-21700-P45B v1.2 datasheet for 12S4P:
    180 A continuous (45 A/cell x 4P) and a 60 C discharge ceiling.
    """

    LIMITS = dict(max_safe_temp=60.0, max_safe_current=180.0, current_buffer=5.0)

    def test_nominal_no_trip(self):
        assert check_thermal_and_current(40.0, 100.0, **self.LIMITS) == (False, None)

    def test_just_under_temp_threshold_no_trip(self):
        assert check_thermal_and_current(59.99, 0.0, **self.LIMITS)[0] is False

    def test_at_temp_threshold_trips(self):
        assert check_thermal_and_current(60.0, 0.0, **self.LIMITS) == (True, "OVERTEMP")

    def test_over_temp_threshold_trips(self):
        assert check_thermal_and_current(70.0, 0.0, **self.LIMITS) == (True, "OVERTEMP")

    def test_current_within_buffer_no_trip(self):
        # max=180, buffer=5 -> trips at 185
        assert check_thermal_and_current(0.0, 184.99, **self.LIMITS)[0] is False

    def test_current_at_buffered_limit_trips(self):
        assert check_thermal_and_current(0.0, 185.0, **self.LIMITS) == (True, "OVERCURRENT")

    def test_datasheet_continuous_rating_is_not_exceeded_silently(self):
        # 4P x 45 A/cell = 180 A. Anything at or past limit+buffer must fault.
        assert check_thermal_and_current(25.0, 187.0, **self.LIMITS) == (True, "OVERCURRENT")

    def test_temp_outranks_current(self):
        assert check_thermal_and_current(100.0, 300.0, **self.LIMITS) == (True, "OVERTEMP")


# ================= MODULE UNDERVOLTAGE =================

class TestModuleUndervoltage:
    """Undervoltage applies only once armed -- see evaluate_safety's docstring.

    36.0 V trip is 3.0 V/cell for 12S, above the 30.0 V absolute cutoff so there
    is room for IR sag under load.
    """

    def _packet(self, **kw):
        d = {'max_temp': 25.0, 'amps': 50.0, 'voltage': 43.2,
             'cell_voltages': [3.60] * 12, 'temp_age_s': 0.1, 'daq_age_s': 0.05,
             'hardware_status': {'temp_arduino': True}}
        d.update(kw)
        return d

    def test_nominal_voltage_no_trip(self):
        assert evaluate_safety(self._packet(), _limits())[0] is False

    def test_just_above_threshold_no_trip(self):
        assert evaluate_safety(self._packet(voltage=36.01), _limits())[0] is False

    def test_at_threshold_trips(self):
        assert evaluate_safety(self._packet(voltage=36.0), _limits()) == (True, "UNDERVOLTAGE")

    def test_sagged_below_cell_cutoff_trips(self):
        # 45 mohm module IR at 187 A sags 8.4 V; a 3.2 V/cell pack lands at
        # 2.499 V/cell, through the absolute cutoff.
        assert evaluate_safety(self._packet(voltage=29.99, amps=100.0),
                               _limits()) == (True, "UNDERVOLTAGE")

    def test_zero_voltage_reading_faults_rather_than_running(self):
        # Lost or failed voltage sensing must fail safe once armed.
        assert evaluate_safety(self._packet(voltage=0.0, amps=0.0),
                               _limits(), armed=True)[0] is True

    def test_current_outranks_undervoltage(self):
        assert evaluate_safety(self._packet(voltage=20.0, amps=300.0),
                               _limits()) == (True, "OVERCURRENT")


# ================= PER-CELL UNDERVOLTAGE =================

def _limits(**overrides):
    """Default limits dict matching SafetyLimits.to_command_dict()."""
    base = {
        'max_temp': 60.0, 'max_amps': 175.0, 'amp_buffer': 5.0,
        'min_volts': 36.0, 'min_cell_volts': 2.70, 'cell_sense_floor': 0.50,
        'temp_stale_timeout': 3.0, 'daq_stale_timeout': 1.0,
        'derate_en': False, 'derate_start': 55.0,
    }
    base.update(overrides)
    return base


class TestCellSafety:
    def test_healthy_cells_pass(self):
        fault, reason = check_cell_safety([3.9] * 12, 2.70)
        assert fault is False
        assert reason is None

    def test_one_weak_cell_trips_though_module_total_looks_fine(self):
        # The gap this exists to close: 11 x 3.40 + 1.00 = 38.40 V, above a
        # 36.0 V module trip, while one cell sits at 1.00 V and is destroyed.
        cells = [3.40] * 11 + [1.00]
        assert sum(cells) > 36.0
        fault, reason = check_cell_safety(cells, 2.70)
        assert fault is True
        assert reason == "CELL UNDERVOLTAGE"

    def test_trips_at_the_threshold(self):
        fault, reason = check_cell_safety([3.9] * 11 + [2.70], 2.70)
        assert fault is True
        assert reason == "CELL UNDERVOLTAGE"

    def test_just_above_threshold_passes(self):
        fault, _ = check_cell_safety([3.9] * 11 + [2.71], 2.70)
        assert fault is False

    def test_implausibly_low_reading_is_a_sense_fault_not_undervoltage(self):
        # A flat cell and an unplugged sense lead look identical; both must stop
        # the run, but naming them apart tells the operator where to look.
        fault, reason = check_cell_safety([3.9] * 11 + [0.0], 2.70)
        assert fault is True
        assert reason == "CELL SENSE FAULT"

    def test_empty_cell_list_fails_closed(self):
        # No per-cell data at all -- broken harness, empty channel list, parse
        # failure. Returning "safe" here would run a high-power profile blind to
        # the very thing this check exists for.
        fault, reason = check_cell_safety([], 2.70)
        assert fault is True
        assert reason == "NO CELL DATA"


# ================= LAP PROFILE PLAYBACK =================

class TestLapRowInterval:
    """Playback must follow the profile's own timestamps, not a fixed second."""

    def _profile(self, dt, n=5):
        return [{'Time (s)': i * dt, 'Speed (mph)': 30.0} for i in range(n)]

    def test_one_hz_profile(self):
        assert math.isclose(lap_row_interval(self._profile(1.0), 0), 1.0)

    def test_ten_hz_profile(self):
        # The case the old fixed 1.0 s playback got wrong: ten times too slow,
        # and each power demand held ten times too long.
        assert math.isclose(lap_row_interval(self._profile(0.1), 0), 0.1)

    def test_irregular_sampling_is_followed_per_row(self):
        rows = [{'Time (s)': 0.0}, {'Time (s)': 0.5}, {'Time (s)': 2.5}]
        assert math.isclose(lap_row_interval(rows, 0), 0.5)
        assert math.isclose(lap_row_interval(rows, 1), 2.0)

    def test_last_row_falls_back_to_default(self):
        assert math.isclose(lap_row_interval(self._profile(0.25), 4), 1.0)

    def test_missing_or_bad_timestamps_fall_back(self):
        assert math.isclose(lap_row_interval([{'Speed (mph)': 10}, {'Speed (mph)': 12}], 0), 1.0)
        assert math.isclose(lap_row_interval([{'Time (s)': 'x'}, {'Time (s)': 'y'}], 0), 1.0)

    def test_non_monotonic_timestamps_fall_back(self):
        # A backwards or duplicated stamp must not produce a zero/negative dwell
        # that would spin the playback loop.
        assert math.isclose(lap_row_interval([{'Time (s)': 5.0}, {'Time (s)': 5.0}], 0), 1.0)
        assert math.isclose(lap_row_interval([{'Time (s)': 5.0}, {'Time (s)': 1.0}], 0), 1.0)

    def test_nan_timestamps_fall_back(self):
        rows = [{'Time (s)': 0.0}, {'Time (s)': float('nan')}]
        assert math.isclose(lap_row_interval(rows, 0), 1.0)

    def test_empty_profile_does_not_raise(self):
        assert math.isclose(lap_row_interval([], 0), 1.0)


class TestLoadLapProfile:
    def test_drops_trailing_blank_rows(self, tmp_path):
        # Exported telemetry commonly has trailing blanks -- the shipped profile
        # has seven. Left in, they yield NaN power, and NaN loses every
        # comparison in compute_target_resistance() so the bank quietly sits at
        # minimum load for the tail of every lap.
        csv = tmp_path / "lap.csv"
        csv.write_text("Time (s),Speed (mph)\n0,0\n1,19\n2,24\n,\n,\n")
        rows, dropped = load_lap_profile(str(csv))
        assert len(rows) == 3
        assert dropped == 2

    def test_keeps_a_clean_profile_intact(self, tmp_path):
        csv = tmp_path / "lap.csv"
        csv.write_text("Time (s),Speed (mph)\n0,0\n1,19\n2,24\n")
        rows, dropped = load_lap_profile(str(csv))
        assert len(rows) == 3
        assert dropped == 0

    def test_shipped_profile_has_no_unusable_rows_after_load(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "FSAE - ETS - Speed and Time 1 Lap.csv")
        rows, dropped = load_lap_profile(path)
        assert dropped > 0, "shipped profile is known to carry trailing blanks"
        for row in rows:
            assert math.isfinite(float(row['Speed (mph)']))
            assert math.isfinite(float(row['Time (s)']))


# ================= DAQ HEALTH =================

class TestDaqHealth:
    def test_fresh_packet_passes(self):
        assert check_daq_health(0.05, 1.0) == (False, None)

    def test_stale_packet_trips(self):
        assert check_daq_health(5.0, 1.0) == (True, "DAQ DATA STALE")

    def test_zero_timeout_disables_the_check(self):
        assert check_daq_health(999.0, 0.0) == (False, None)

    def test_neutral_packet_reads_as_no_data_not_healthy(self):
        # Used before the first DAQ packet arrives. It must not look like a
        # healthy pack, or the FSM could be driven on a packet that never was.
        p = NEUTRAL_PACKET()
        assert p['voltage'] == 0.0
        assert p['cell_voltages'] == []
        assert evaluate_safety(p, _limits(), armed=True)[0] is True


# ================= SENSOR HEALTH =================

class TestSensorHealth:
    def test_fresh_and_connected_passes(self):
        fault, reason = check_sensor_health(0.2, True, 3.0)
        assert fault is False
        assert reason is None

    def test_lost_link_trips(self):
        fault, reason = check_sensor_health(0.0, False, 3.0)
        assert fault is True
        assert reason == "TEMP LINK LOST"

    def test_stale_data_trips(self):
        # Connected but silent: the DAQ keeps republishing its last array.
        fault, reason = check_sensor_health(5.0, True, 3.0)
        assert fault is True
        assert reason == "TEMP DATA STALE"

    def test_just_within_timeout_passes(self):
        fault, _ = check_sensor_health(2.99, True, 3.0)
        assert fault is False

    def test_zero_timeout_disables_the_staleness_check(self):
        fault, _ = check_sensor_health(999.0, True, 0.0)
        assert fault is False


# ================= COMPOSED SAFETY EVALUATION =================

class TestEvaluateSafety:
    def _packet(self, **overrides):
        data = {
            'max_temp': 30.0, 'amps': 50.0, 'voltage': 45.0,
            'cell_voltages': [3.75] * 12, 'temp_age_s': 0.1, 'daq_age_s': 0.05,
            'hardware_status': {'temp_arduino': True},
        }
        data.update(overrides)
        return data

    def test_healthy_packet_passes(self):
        fault, reason = evaluate_safety(self._packet(), _limits())
        assert fault is False
        assert reason is None

    def test_frozen_temperature_no_longer_reads_as_healthy(self):
        # Sensor died at 52 C. Reading is plausible and below the 60 C limit, so
        # only the staleness check can catch it.
        data = self._packet(max_temp=52.0, temp_age_s=30.0)
        fault, reason = evaluate_safety(data, _limits())
        assert fault is True
        assert reason == "TEMP DATA STALE"

    def test_weak_cell_caught_despite_healthy_module_total(self):
        data = self._packet(voltage=38.4, cell_voltages=[3.40] * 11 + [1.00])
        fault, reason = evaluate_safety(data, _limits())
        assert fault is True
        assert reason == "CELL UNDERVOLTAGE"

    def test_measured_danger_outranks_sensor_fault(self):
        # Over-current with a dead temperature link: the operator needs to hear
        # about the current, not the sensor.
        data = self._packet(amps=500.0, temp_age_s=99.0,
                            hardware_status={'temp_arduino': False})
        fault, reason = evaluate_safety(data, _limits())
        assert fault is True
        assert reason == "OVERCURRENT"

    def test_unarmed_skips_data_integrity_checks(self):
        # In IDLE a missing temperature link is not-connected-yet, not a fault.
        data = self._packet(hardware_status={'temp_arduino': False})
        assert evaluate_safety(data, _limits(), armed=False)[0] is False
        assert evaluate_safety(data, _limits(), armed=True)[0] is True

    def test_unpowered_rig_does_not_lock_out_of_idle(self):
        # Boot the software before plugging the battery in and the DAQ reports
        # 0.0 V. Tripping on that in IDLE latched a FAULT that RESET could not
        # clear -- back to IDLE, instantly re-trips -- locking out bring-up.
        cold = self._packet(voltage=0.0, cell_voltages=[], max_temp=0.0, amps=0.0,
                            temp_age_s=float('inf'),
                            hardware_status={'temp_arduino': False})
        assert evaluate_safety(cold, _limits(), armed=False)[0] is False
        # ...but arming on that same reading must not be silently permitted.
        assert evaluate_safety(cold, _limits(), armed=True)[0] is True

    def test_overtemp_and_overcurrent_apply_even_unarmed(self):
        # Current flowing or cells hot while not armed means something is wrong
        # regardless of what the FSM believes.
        assert evaluate_safety(self._packet(max_temp=99.0), _limits(), armed=False) \
            == (True, "OVERTEMP")
        assert evaluate_safety(self._packet(amps=500.0), _limits(), armed=False) \
            == (True, "OVERCURRENT")

    def test_stale_daq_packet_trips(self):
        # The loop carries the last packet forward when the queue is empty, so
        # every reading below still looks plausible. Only the age gives it away.
        data = self._packet(daq_age_s=5.0)
        assert evaluate_safety(data, _limits()) == (True, "DAQ DATA STALE")

    def test_missing_cell_data_trips_once_armed(self):
        data = self._packet(cell_voltages=[])
        assert evaluate_safety(data, _limits(), armed=True) == (True, "NO CELL DATA")

    def test_missing_keys_fall_back_to_safe_defaults(self):
        # A malformed packet must not raise inside the safety loop.
        fault, reason = evaluate_safety({}, _limits())
        assert fault is True          # 0.0 V reads as undervoltage
        assert reason == "UNDERVOLTAGE"


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


# ================= LAP BOUNDARY =================

class TestLapTransition:
    """Row 0 of lap 2+ is a continuation, not a standing start.

    Two faults used to collide here. Treating any row_idx == 0 as the first row
    forced dv to zero, discarding the car's carried velocity. And because the
    profile's clock restarts each lap, the raw timestamp difference is large and
    negative, which the non-positive guard silently replaced with 1.0 s.
    """

    # 20 -> 45 mph across the start/finish line, sampled at 10 Hz.
    ENTRY_V = 20.0 * 0.44704
    ROW = {"Speed (mph)": 45.0, "Time (s)": 0.0}
    LAP_END_T = 60.0

    def test_carried_velocity_is_not_discarded(self):
        _, accel, _ = compute_lap_physics(
            self.ROW, prev_velocity_ms=self.ENTRY_V, prev_time_s=self.LAP_END_T,
            is_first_row=False, row_idx=0, lap_wrap_dt=0.1)
        assert accel > 0.0

    def test_acceleration_uses_the_supplied_wrap_interval(self):
        _, accel, _ = compute_lap_physics(
            self.ROW, prev_velocity_ms=self.ENTRY_V, prev_time_s=self.LAP_END_T,
            is_first_row=False, row_idx=0, lap_wrap_dt=0.1)
        expected_dv = (45.0 * 0.44704) - self.ENTRY_V
        assert math.isclose(accel, expected_dv / 0.1, rel_tol=1e-9)

    def test_wrap_interval_scales_with_the_profile_rate(self):
        # A 1 Hz profile must not be handed a 10 Hz interval, and vice versa --
        # the whole reason this is derived rather than a literal.
        args = dict(prev_velocity_ms=self.ENTRY_V, prev_time_s=self.LAP_END_T,
                    is_first_row=False, row_idx=0)
        _, fast, _ = compute_lap_physics(self.ROW, lap_wrap_dt=0.1, **args)
        _, slow, _ = compute_lap_physics(self.ROW, lap_wrap_dt=1.0, **args)
        assert math.isclose(fast / slow, 10.0, rel_tol=1e-9)

    def test_without_the_wrap_interval_the_clock_reset_corrupts_dt(self):
        # Documents what the fix prevents: the raw difference is -60 s, the
        # guard substitutes 1.0 s, and the result is a real velocity change
        # divided by a fabricated interval.
        _, accel, _ = compute_lap_physics(
            self.ROW, prev_velocity_ms=self.ENTRY_V, prev_time_s=self.LAP_END_T,
            is_first_row=False, row_idx=0, lap_wrap_dt=None)
        expected_dv = (45.0 * 0.44704) - self.ENTRY_V
        assert math.isclose(accel, expected_dv / 1.0, rel_tol=1e-9)
        # ...which is ten times gentler than the truth at a 10 Hz log rate.
        _, correct, _ = compute_lap_physics(
            self.ROW, prev_velocity_ms=self.ENTRY_V, prev_time_s=self.LAP_END_T,
            is_first_row=False, row_idx=0, lap_wrap_dt=0.1)
        assert correct > accel * 9

    def test_standing_start_still_has_no_acceleration(self):
        # Lap 1 row 0 keeps the old behaviour.
        _, accel, _ = compute_lap_physics(
            {"Speed (mph)": 0.0, "Time (s)": 0.0}, prev_velocity_ms=0.0, prev_time_s=0.0,
            is_first_row=True, row_idx=0)
        assert accel == 0.0

    def test_standing_start_ignores_a_wrap_interval(self):
        # Belt and braces: the flags cannot both apply, but if they did the
        # standing start must win rather than differencing against nothing.
        _, accel, _ = compute_lap_physics(
            self.ROW, prev_velocity_ms=0.0, prev_time_s=0.0,
            is_first_row=True, row_idx=0, lap_wrap_dt=0.1)
        assert accel == 0.0

    def test_braking_across_the_line_gives_negative_acceleration(self):
        slower = {"Speed (mph)": 10.0, "Time (s)": 0.0}
        _, accel, _ = compute_lap_physics(
            slower, prev_velocity_ms=self.ENTRY_V, prev_time_s=self.LAP_END_T,
            is_first_row=False, row_idx=0, lap_wrap_dt=0.1)
        assert accel < 0.0

    def test_row_one_of_a_new_lap_differences_normally(self):
        # prev_time_s was rewritten to the new lap's row 0 stamp, so the wrap is
        # a single frame and the profile's own timestamps resume immediately.
        _, accel, _ = compute_lap_physics(
            {"Speed (mph)": 50.0, "Time (s)": 0.1},
            prev_velocity_ms=45.0 * 0.44704, prev_time_s=0.0,
            is_first_row=False, row_idx=1)
        expected_dv = (50.0 - 45.0) * 0.44704
        assert math.isclose(accel, expected_dv / 0.1, rel_tol=1e-9)

    def test_standing_start_flag_is_lap_aware(self):
        # The condition the main loop applies. row_idx == 0 alone was the bug.
        def standing(lap, idx):
            return lap == 1 and idx == 0

        assert standing(1, 0) is True
        assert standing(2, 0) is False
        assert standing(9, 0) is False
        assert standing(1, 5) is False


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


# ================= MULTI-MODULE BATTERY =================

class TestModulesInSeries:
    """The bench loads ONE module; req_power is the whole car's demand.

    Matching the module's real duty means matching its current, so
    R = N * V_module^2 / P_car. The bank sees only the module's ~50 V, so both
    current clamps divide the module voltage, not the battery voltage.
    """

    def test_power_term_scales_linearly_with_module_count(self):
        # N, not N^2: a bank across one module needs 1/N the resistance of one
        # across the whole battery to pull the same current.
        # 5 kW keeps BOTH results clear of the clamp and the MAX_RESISTANCE cap;
        # at higher power the single-module case clamps and this would compare
        # two clamp values rather than the power term.
        one = compute_target_resistance(50.0, 5_000.0, 180.0, 25.0, False, 55.0, 60.0,
                                        modules_in_series=1)
        nine = compute_target_resistance(50.0, 5_000.0, 180.0, 25.0, False, 55.0, 60.0,
                                         modules_in_series=9)
        assert one > 50.0 / 180.0        # clamp not binding
        assert nine < MAX_RESISTANCE     # cap not binding
        assert math.isclose(nine / one, 9.0, rel_tol=1e-9)

    def test_module_current_matches_what_the_car_would_draw(self):
        # The whole point of the rig: the module under test must see the same
        # current it would carry in the car.
        modules, v_module, p_car = 9, 50.0, 40_000.0
        v_battery = v_module * modules
        i_car = p_car / v_battery

        r = compute_target_resistance(v_module, p_car, 180.0, 25.0, False, 55.0, 60.0,
                                      modules_in_series=modules)
        assert math.isclose(v_module / r, i_car, rel_tol=1e-9)

    def test_module_dissipates_its_share_of_car_power(self):
        modules, v_module, p_car = 9, 50.0, 45_000.0
        r = compute_target_resistance(v_module, p_car, 180.0, 25.0, False, 55.0, 60.0,
                                      modules_in_series=modules)
        assert math.isclose((v_module ** 2) / r, p_car / modules, rel_tol=1e-9)

    def test_peak_car_power_lands_near_the_ladder_minimum(self):
        # The bank is a binary ladder 0.25..32 ohm. An ~81 kW lap demand should
        # ask for roughly its smallest step at the 180 A limit -- the rig is
        # sized for that, and a formula that misses it by a factor of N would put
        # peak demand nowhere near the hardware's range.
        r = compute_target_resistance(50.0, 81_000.0, 180.0, 25.0, False, 55.0, 60.0,
                                      modules_in_series=9)
        assert 0.25 <= r <= 0.35
        assert math.isclose(50.0 / r, 180.0, rel_tol=0.02)

    def test_current_clamp_uses_module_voltage(self):
        # Only the module's voltage is across the bank, so the floor is V_mod/I.
        r = compute_target_resistance(50.0, 1e12, 180.0, 25.0, False, 55.0, 60.0,
                                      modules_in_series=9)
        assert math.isclose(r, 50.0 / 180.0, rel_tol=1e-9)
        assert math.isclose(50.0 / r, 180.0, rel_tol=1e-9)

    def test_clamp_is_independent_of_module_count(self):
        # Series modules share one current, so the limit does not scale with N.
        floors = {compute_target_resistance(50.0, 1e12, 180.0, 25.0, False, 55.0, 60.0,
                                            modules_in_series=n)
                  for n in (1, 4, 9, 20)}
        assert len(floors) == 1

    def test_derate_clamp_also_uses_module_voltage(self):
        # At full derate the active limit floors at 1 A, so R = V_module / 1.
        r = compute_target_resistance(50.0, 1e12, 180.0, 60.0, True, 55.0, 60.0,
                                      modules_in_series=9)
        assert math.isclose(r, min(MAX_RESISTANCE, 50.0), rel_tol=1e-9)

    def test_defaults_to_single_module(self):
        explicit = compute_target_resistance(50.0, 20_000.0, 180.0, 25.0, False, 55.0, 60.0,
                                             modules_in_series=1)
        implicit = compute_target_resistance(50.0, 20_000.0, 180.0, 25.0, False, 55.0, 60.0)
        assert math.isclose(explicit, implicit, rel_tol=1e-12)

    def test_zero_or_negative_module_count_treated_as_one(self):
        baseline = compute_target_resistance(50.0, 20_000.0, 180.0, 25.0, False, 55.0, 60.0,
                                             modules_in_series=1)
        for bad in (0, -3):
            assert math.isclose(
                compute_target_resistance(50.0, 20_000.0, 180.0, 25.0, False, 55.0, 60.0,
                                          modules_in_series=bad),
                baseline, rel_tol=1e-12)

    def test_still_never_exceeds_bank_maximum(self):
        r = compute_target_resistance(50.0, 0.001, 180.0, 25.0, False, 55.0, 60.0,
                                      modules_in_series=9)
        assert r <= MAX_RESISTANCE


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
        assert 0.0 < p.drivetrain_efficiency <= 1.0

    def test_rotational_mass_factor_never_below_one(self):
        # Physical invariant, not a pinned value: spinning wheels, rotors and
        # drivetrain can only ADD effective mass under acceleration. Below 1.0
        # they would make the car easier to accelerate, which is nonsense.
        # The shipped default sits at exactly 1.0 -- the effect is deliberately
        # switched off until the real rotational inertia is measured.
        assert VehicleParams().rotational_mass_factor >= 1.0

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
