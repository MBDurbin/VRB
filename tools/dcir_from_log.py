"""
dcir_from_log.py -- the module's DC resistance, from a recording of the pulse profile.

Run the rig on profiles/"Bench - DC Resistance Pulses.csv" with Record on, then:

    python tools/dcir_from_log.py                      # the newest logs/telemetry_*.csv
    python tools/dcir_from_log.py logs/telemetry_20261104_101500.csv

Each lap of the profile rests the module, pulses it for 12 s at 2.5, 1.25 and
0.75 ohm (about 20, 40 and 67 A from a full module) with 48 s rests between,
then discharges it at 1.5 ohm for 180 s. The pulses run 12 s, not 10, so the
10 s reading lands inside the pulse rather than on the sample where it ends. A lap takes about 2 Ah, a tenth of a
12S4P module, so 8 laps from full measure the resistance from 100% down to
about 20% charge. The 1.5 ohm block is measured too, as a fourth pulse.

The 0.75 ohm pulse needs the current limit above 67 A. With max_amps at 60 A,
as for the bench-supply test, the rig floors it to 1.0-1.25 ohm and that pulse
repeats the 1.25 ohm one.

For every step from rest into load, each voltage the log carries (the module
and each cell group) gives

    R(t) = (V_rest(t) - V(t)) / (I(t) - I_rest)

at t = 1 s and 10 s after the step. V_rest(t) is a straight line fitted to the
last 10 s of rest and carried forward, so a module still recovering from the
last pulse does not read as resistance. 1 s is mostly the cell's ohmic part;
10 s adds the fast polarisation, and is the figure for sag under a race load.

A cell-group figure is the resistance between two taps: the 4 cells in
parallel plus the collector plate between them. Times the parallel count, it
is what one cell would show, and is comparable to cell_dc_milliohm in
rig_config.json. Reads only; it never changes the config.

Cell voltages are the difference of two cumulative taps, so each carries a few
millivolts of DAQ error. That is under 1 mOhm per cell at the 20 A pulse and
about 0.2 at the 67 A one: trust the larger pulses.
"""

import glob
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rig_config import RigConfig

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")

# A sample is under load at or above this current. The ladder's lightest
# setting, 63.75 ohm, draws under 1 A from a full module.
LOAD_AMPS = 5.0

# Rest fitted before each step, and the least of it a step needs to be measured.
REST_WINDOW_S = 10.0
MIN_REST_SAMPLES = 5

# Seconds after the step at which the resistance is read, and the half-width of
# the local fit that reads it (about three samples at the rig's 10 Hz).
READ_AT_S = (1.0, 10.0)
READ_HALF_WIDTH_S = 0.3


def find_pulses(amps, load_amps=LOAD_AMPS):
    """(first, last) sample index of each run of samples at or above load_amps."""
    loaded = np.asarray(amps) >= load_amps
    pulses, start = [], None
    for i, on in enumerate(loaded):
        if on and start is None:
            start = i
        elif not on and start is not None:
            pulses.append((start, i - 1))
            start = None
    if start is not None:
        pulses.append((start, len(loaded) - 1))
    return pulses


def _fit_at(t, v, at):
    """A straight line through (t, v), evaluated at `at`. NaN if v has a gap."""
    if len(t) < 2 or not np.all(np.isfinite(v)):
        return float("nan")
    slope, intercept = np.polyfit(t, v, 1)
    return float(slope * at + intercept)


def _read_at(t, v, at, half_width=READ_HALF_WIDTH_S):
    """v at time `at` from the samples within half_width of it: a local line
    through them, or plain interpolation when fewer than two are that close."""
    near = np.abs(t - at) <= half_width
    if near.sum() >= 2:
        return _fit_at(t[near], v[near], at)
    if not np.all(np.isfinite(v)):
        return float("nan")
    return float(np.interp(at, t, v))


def measure_pulse(t, amps, volts, start, end, read_at=READ_AT_S,
                  rest_window=REST_WINDOW_S, min_rest=MIN_REST_SAMPLES, load_amps=LOAD_AMPS):
    """Resistance from one step into load. Returns (result, None) or (None, reason).

    `volts` maps a column name (Pack_V, Cell_1_V, ...) to its samples. The
    result holds the step's time and currents and, for each column, a list of
    ohms in the order of `read_at`: NaN where the pulse ended first or the
    column has a gap.
    """
    t, amps = np.asarray(t, float), np.asarray(amps, float)
    if start == 0:
        return None, "the log starts under load"
    edge = 0.5 * (t[start - 1] + t[start])
    rest = np.arange(start)[(t[:start] >= t[start - 1] - rest_window)]
    if np.any(amps[rest] >= load_amps):
        return None, f"less than {rest_window:g} s of rest before it"
    if len(rest) < min_rest or t[start - 1] - t[rest[0]] < 0.5 * rest_window:
        return None, f"less than {rest_window:g} s of rest before it"

    i_rest = float(np.mean(amps[rest]))
    pulse = np.arange(start, end + 1)
    tr, tp = t[rest] - edge, t[pulse] - edge
    result = {"t_edge": float(edge), "duration": float(t[end] - edge), "i_rest": i_rest,
              "i_load": float(np.mean(amps[pulse])), "ohms": {}}
    i_at = [(_read_at(tp, amps[pulse], dt) if dt <= tp[-1] else float("nan")) for dt in read_at]
    result["i_step"] = [i - i_rest for i in i_at]
    for name, v in volts.items():
        v = np.asarray(v, float)
        ohms = []
        for dt, i in zip(read_at, i_at):
            if not math.isfinite(i) or i - i_rest <= 0:
                ohms.append(float("nan"))
                continue
            v_rest = _fit_at(tr, v[rest], dt)
            v_load = _read_at(tp, v[pulse], dt)
            ohms.append((v_rest - v_load) / (i - i_rest))
        result["ohms"][name] = ohms
        if name.startswith("Cell_"):
            result.setdefault("rest_cell_v", []).append(_fit_at(tr, v[rest], 0.0))
    return result, None


def load_log(path):
    """The recording, by time, with the columns this needs as numbers."""
    df = pd.read_csv(path)
    for col in df.columns:
        if col in ("Timestamp", "Pack_V", "Amps", "SOC_Est") or col.startswith("Cell_"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["Timestamp", "Amps", "Pack_V"])
    return df.sort_values("Timestamp").drop_duplicates("Timestamp").reset_index(drop=True)


def cell_columns(df):
    """Cell_1_V, Cell_2_V, ... in cell order."""
    cols = [c for c in df.columns if c.startswith("Cell_") and c.endswith("_V")]
    return sorted(cols, key=lambda c: int(c.split("_")[1]))


def analyse(df, parallel, read_at=READ_AT_S, load_amps=LOAD_AMPS):
    """Every measurable step in the log. Returns (rows, skipped).

    One row per step: its time, charge, rest voltage, current step, and the
    module and per-cell resistance at each read time, in milliohms. A per-cell
    figure is the cell group's resistance times `parallel`.
    """
    cells = cell_columns(df)
    volts = {"Pack_V": df["Pack_V"].to_numpy(float)}
    volts.update({c: df[c].to_numpy(float) for c in cells})
    t, amps = df["Timestamp"].to_numpy(float), df["Amps"].to_numpy(float)
    soc = df["SOC_Est"].to_numpy(float) if "SOC_Est" in df.columns else None

    rows, skipped = [], []
    for n, (start, end) in enumerate(find_pulses(amps, load_amps), start=1):
        res, why = measure_pulse(t, amps, volts, start, end, read_at, load_amps=load_amps)
        if res is None:
            skipped.append((n, float(t[start] - t[0]), why))
            continue
        row = {"pulse": n, "start_s": round(res["t_edge"] - t[0], 2),
               "length_s": round(res["duration"], 1),
               "soc_est_pct": round(float(soc[start - 1]), 1) if soc is not None else float("nan"),
               "rest_v_per_cell": round(float(np.nanmean(res.get("rest_cell_v", [np.nan]))), 3),
               "step_a": round(res["i_step"][0], 1)}
        for k, dt in enumerate(read_at):
            tag = f"{dt:g}s"
            row[f"module_mohm_{tag}"] = 1000 * res["ohms"]["Pack_V"][k]
            per_cell = [1000 * parallel * res["ohms"][c][k] for c in cells]
            for c, value in zip(cells, per_cell):
                row[f"{c[:-2].lower()}_mohm_{tag}"] = value
            finite = [x for x in per_cell if math.isfinite(x)]
            row[f"cell_median_mohm_{tag}"] = float(np.median(finite)) if finite else float("nan")
            row[f"cell_max_mohm_{tag}"] = max(finite) if finite else float("nan")
            row[f"cell_max_is_{tag}"] = (cells[per_cell.index(max(finite))].split("_")[1]
                                         if finite else "")
        rows.append(row)
    return rows, skipped


def newest_log(folder=LOGS_DIR):
    logs = glob.glob(os.path.join(folder, "telemetry_*.csv"))
    return max(logs, key=os.path.getmtime) if logs else None


def _mohm(x, width):
    return f"{x:{width}.2f}" if math.isfinite(x) else f"{'-':>{width}}"


def main(argv):
    path = argv[1] if len(argv) > 1 else newest_log()
    if not path or not os.path.exists(path):
        print("No recording found. Record a run of 'Bench - DC Resistance Pulses.csv' "
              "and pass its CSV from logs/.")
        return 1

    pack = RigConfig.load().pack
    rows, skipped = analyse(load_log(path), pack.parallel_count)
    tags = [f"{dt:g}s" for dt in READ_AT_S]
    print(f"{os.path.basename(path)}: {len(rows)} steps measured, {len(skipped)} skipped.")
    print(f"Resistances in mOhm. Cell = the median cell group x {pack.parallel_count}P; "
          f"worst = the highest cell at {tags[-1]}.\n")
    print(f"{'Pulse':>5} {'Start s':>8} {'SOC %':>6} {'V/cell':>7} {'Step A':>7}"
          + "".join(f" {'Module ' + t:>10}" for t in tags)
          + "".join(f" {'Cell ' + t:>9}" for t in tags) + f" {'Worst':>12}")
    for r in rows:
        worst = f"#{r[f'cell_max_is_{tags[-1]}']} {_mohm(r[f'cell_max_mohm_{tags[-1]}'], 5)}"
        print(f"{r['pulse']:5d} {r['start_s']:8.1f} {r['soc_est_pct']:6.1f} "
              f"{r['rest_v_per_cell']:7.3f} {r['step_a']:7.1f}"
              + "".join(f" {_mohm(r[f'module_mohm_{t}'], 10)}" for t in tags)
              + "".join(f" {_mohm(r[f'cell_median_mohm_{t}'], 9)}" for t in tags)
              + f" {worst:>12}")
    for n, at, why in skipped:
        print(f"Step {n} at {at:.1f} s not measured: {why}.")

    big = [r for r in rows if r["step_a"] >= 50.0]
    if big:
        tag = tags[-1]
        median = float(np.median([r[f"cell_median_mohm_{tag}"] for r in big]))
        print(f"\nPer-cell resistance at {tag}, steps of 50 A or more: median {median:.2f} mOhm. "
              f"rig_config.json has cell_dc_milliohm = {pack.cell_dc_milliohm:g} mOhm.")

    out = os.path.splitext(path)[0] + "_dcir.csv"
    pd.DataFrame(rows).to_csv(out, index=False)
    print(f"\nEvery step and every cell group: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
