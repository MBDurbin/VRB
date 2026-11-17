"""
The resistance measurement in tools/dcir_from_log.py, on synthetic recordings
with a known answer, and the pulse profile it reads, against the rig's limits.
"""
import importlib.util
import math
import os

import numpy as np
import pandas as pd
import pytest

from control_logic import (BANK_OVERLOAD_FACTOR, MAX_RESISTANCE, PROFILES_DIR, RESISTOR_RESOLUTION,
                           lap_row_interval, load_lap_profile, profile_resistance,
                           steps_for_resistance)

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "tools", "dcir_from_log.py")
spec = importlib.util.spec_from_file_location("dcir_from_log", TOOL)
dcir = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dcir)

PROFILE = os.path.join(PROFILES_DIR, "Bench - DC Resistance Pulses.csv")

SERIES, PARALLEL = 12, 4
R0 = 1.2e-3      # each cell group's ohmic resistance: 4.8 mOhm a cell at 4P
R1 = 0.4e-3      # its polarisation, reached with time constant TAU
TAU = 3.0
I_REST = 0.8


def group_ohms(dt):
    """What a cell group's resistance should read dt seconds into a step."""
    return R0 + R1 * (1 - math.exp(-dt / TAU))


def make_log(pulses, t_end, drift=0.5e-3, rounded=True, r0=None):
    """A recording as the GUI writes it, of a module stepping into `pulses`.

    `pulses` = [(on_s, off_s, amps)]. Each cell group rests at 3.9 V plus
    `drift` volts a second (a module still recovering), drops I x R0 at once
    and a further step x R1 with time constant TAU, which decays after the
    pulse. `r0` gives each group its own ohmic resistance.
    """
    r0 = r0 if r0 is not None else [R0] * SERIES
    t = np.arange(0.0, t_end, 0.1) + 0.004 * np.sin(np.arange(int(round(t_end / 0.1))))
    amps = np.full_like(t, I_REST)
    eta = np.zeros_like(t)                      # polarisation, volts per ohm of R1
    for on, off, i in pulses:
        inside = (t >= on) & (t < off)
        amps[inside] = i
        eta[inside] += (i - I_REST) * (1 - np.exp(-(t[inside] - on) / TAU))
        after = t >= off
        eta[after] += (i - I_REST) * (1 - math.exp(-(off - on) / TAU)) * np.exp(-(t[after] - off) / TAU)
    cells = [3.9 + drift * t - amps * r - eta * R1 for r in r0]
    df = pd.DataFrame({"Timestamp": t, "Pack_V": np.sum(cells, axis=0), "Amps": amps,
                       "Power_kW": 0.0, "Max_Temp": 25.0, "SOC_Est": 90.0,
                       "FSM_State": "RUNNING", "Target_Res": 63.75})
    for k, v in enumerate(cells, start=1):
        df[f"Cell_{k}_V"] = v
    if rounded:
        # The GUI writes the module to 10 mV and each cell to 1 mV.
        df["Pack_V"] = df["Pack_V"].round(2)
        for k in range(1, SERIES + 1):
            df[f"Cell_{k}_V"] = df[f"Cell_{k}_V"].round(3)
    return df


# ================= MEASUREMENT =================

def test_pulses_are_runs_of_samples_under_load():
    assert dcir.find_pulses([0.8, 0.8, 20, 21, 0.8, 67, 67]) == [(2, 3), (5, 6)]


def test_reads_module_and_each_cell_at_1_and_10_s():
    rows, skipped = dcir.analyse(make_log([(60, 72, 67.0)], 120), PARALLEL)
    assert not skipped and len(rows) == 1
    r = rows[0]
    for tag, dt in (("1s", 1.0), ("10s", 10.0)):
        assert r[f"module_mohm_{tag}"] == pytest.approx(1000 * SERIES * group_ohms(dt), rel=0.03)
        for k in range(1, SERIES + 1):
            assert r[f"cell_{k}_mohm_{tag}"] == pytest.approx(
                1000 * PARALLEL * group_ohms(dt), rel=0.03)
    assert r["step_a"] == pytest.approx(67.0 - I_REST, abs=0.1)


def test_a_module_still_recovering_is_not_read_as_resistance():
    # 2 mV/s of recovery is 20 mV by the 10 s reading, a quarter of the 1.6 mOhm
    # group's 106 mV drop. The rest line carried forward takes it out.
    rows, _ = dcir.analyse(make_log([(60, 72, 67.0)], 120, drift=2e-3, rounded=False), PARALLEL)
    assert rows[0]["cell_1_mohm_10s"] == pytest.approx(1000 * PARALLEL * group_ohms(10.0), rel=0.005)


def test_names_the_worst_cell():
    r0 = [R0] * SERIES
    r0[6] = 2 * R0                              # cell 7 has twice the resistance
    rows, _ = dcir.analyse(make_log([(60, 72, 67.0)], 120, r0=r0), PARALLEL)
    assert rows[0]["cell_max_is_10s"] == "7"
    assert rows[0]["cell_median_mohm_10s"] == pytest.approx(1000 * PARALLEL * group_ohms(10.0), rel=0.03)


def test_a_step_without_its_rest_is_skipped():
    rows, skipped = dcir.analyse(make_log([(60, 72, 40.0), (75, 87, 67.0)], 120), PARALLEL)
    assert [r["pulse"] for r in rows] == [1]
    assert skipped and skipped[0][0] == 2 and "rest" in skipped[0][2]


def test_a_pulse_shorter_than_a_read_time_leaves_that_time_blank():
    rows, _ = dcir.analyse(make_log([(60, 65, 67.0)], 120), PARALLEL)
    assert math.isfinite(rows[0]["module_mohm_1s"])
    assert math.isnan(rows[0]["module_mohm_10s"])


def test_a_missing_cell_blanks_only_that_cell():
    df = make_log([(60, 72, 67.0)], 120)
    df["Cell_5_V"] = np.nan                     # the GUI writes an empty field
    r = dcir.analyse(df, PARALLEL)[0][0]
    assert math.isnan(r["cell_5_mohm_10s"])
    assert math.isfinite(r["cell_6_mohm_10s"]) and math.isfinite(r["cell_median_mohm_10s"])


def test_reads_the_csv_the_gui_writes(tmp_path):
    path = tmp_path / "telemetry_20261104_101500.csv"
    df = make_log([(60, 72, 67.0)], 120)
    df["Cell_5_V"] = ""
    df.to_csv(path, index=False)
    rows, _ = dcir.analyse(dcir.load_log(str(path)), PARALLEL)
    assert rows[0]["cell_1_mohm_10s"] == pytest.approx(1000 * PARALLEL * group_ohms(10.0), rel=0.03)


# ================= THE PULSE PROFILE =================

def profile_rows():
    rows, dropped = load_lap_profile(PROFILE)
    assert rows and dropped == 0
    return [(profile_resistance(r), lap_row_interval(rows, i)) for i, r in enumerate(rows)]


def test_the_profile_starts_and_ends_at_no_load():
    rows = profile_rows()
    assert rows[0][0] == MAX_RESISTANCE and rows[-1][0] == MAX_RESISTANCE


def test_every_step_has_its_rest_before_it_and_outlasts_the_read_times():
    rows = profile_rows()
    for (prev_ohms, prev_hold), (ohms, hold) in zip(rows, rows[1:]):
        if ohms < MAX_RESISTANCE:
            assert prev_ohms == MAX_RESISTANCE and prev_hold >= dcir.REST_WINDOW_S
            # The 10 s reading needs samples on both sides of it, after
            # the relays have switched: a 10 s pulse ends on it.
            assert hold >= max(dcir.READ_AT_S) + 1.0


@pytest.mark.parametrize("power_factor", [1.0, BANK_OVERLOAD_FACTOR])
@pytest.mark.parametrize("volts", [50.4, 46.0, 40.0])
def test_every_pulse_is_sent_as_asked_from_full_to_empty(volts, power_factor):
    # A request a bank rating would move lands on a different current than the
    # docstring promises. Each is split over two or more banks so none does.
    for ohms, _ in profile_rows():
        steps = steps_for_resistance(volts, ohms, 275.0, 25.0, False, 55.0, 80.0,
                                     power_factor=power_factor)
        assert steps * RESISTOR_RESOLUTION == ohms


def test_a_lap_takes_about_a_tenth_of_the_module():
    amp_seconds = sum(46.0 / ohms * hold for ohms, hold in profile_rows())
    assert 0.08 < amp_seconds / 3600 / 20.0 < 0.12
