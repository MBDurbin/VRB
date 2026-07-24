# SIL Validation Checklist

Manual desk-test procedure for the behavior that unit tests **cannot** cover:
process/queue plumbing, GUI responsiveness, and timing. No battery, DAQ, or
Arduino required — plug in the SIL dongle and run `main_v2.py`.

Unit tests cover the decision math (`pytest`). This covers everything else.

---

## Setup

```bash
python -m pytest
```

All 33 tests must pass before starting. Then plug in the SIL dongle and:

```bash
python main_v2.py
```

Confirm the console prints `--> SIL KEY UNLOCKED on COMx` and both the SIL
plant-model window and the main telemetry GUI appear.

---

## A. Baseline pipeline

| # | Step | Expected |
|---|------|----------|
| A1 | Observe idle state | State pill reads `DISCONNECTED` (no resistor Arduino in SIL) |
| A2 | Drag the SIL **Simulated Load** slider | Current label + cyan graph track the slider within ~1s |
| A3 | Drag the SIL **Max Temp** slider | Heatmap (SHOW HEATMAP) cells recolor blue→red |
| A4 | Let it sit 60s | Voltage/current graphs scroll continuously, no freeze, no console spam |

## B. Safety trips

| # | Step | Expected |
|---|------|----------|
| B1 | Raise temp slider past **Max Temp** (default 65 °C) | State goes `FAULT` (red), console logs `OVERTEMP ALARM!` |
| B2 | While in FAULT, lower temp back to 25 °C | State **stays** `FAULT` — must not self-clear |
| B3 | Press **RESET** | Returns to `IDLE` |
| B4 | Raise current slider past **Max Amp + E-Stop Buffer** (187 A) | `FAULT`, console logs `OVERCURRENT ALARM!` |
| B5 | RESET, then press **E-STOP** with no fault present | Immediately `FAULT` regardless of prior state |

## C. FSM transition guards

| # | Step | Expected |
|---|------|----------|
| C1 | From `IDLE`, press **RUN** (skipping ARM) | Ignored — state stays `IDLE` |
| C2 | From `IDLE`, press **RESET** | Ignored (RESET is FAULT-only) |
| C3 | ARM → RUN with laps=3 | State `RUNNING`, lap counter reads `Lap: 1 / 3` |
| C4 | While `RUNNING`, press **ARM** | Ignored — no state change |
| C5 | While `RUNNING`, press **E-STOP** | Immediate `FAULT`, lap counter resets to `-- / --` |
| C6 | Let a 1-lap run finish | Auto-returns to `IDLE`, console logs `All 1 laps completed` |

## D. GUI-glitch safety (the important one)

| # | Step | Expected |
|---|------|----------|
| D1 | **Type** a new value into `Max Temp` (select-all, type `100`, press Enter) | No fault while typing. Limit applies only on Enter/focus-out (regression test for Risk #1) |
| D2 | Rapidly spin `Max Amp` up/down ~30x fast | GUI stays responsive; no queue backlog; limits settle on final value |
| D3 | Immediately after D2, press **E-STOP** | Responds instantly, never freezes (regression test for Risk #2) |
| D4 | Set `Derate Start` above `Max Temp`, enable derate, then RUN | No crash. Logic process stays alive (fail-safe full derate) |
| D5 | Set `Max Amp` to 0, then RUN | No crash, no divide-by-zero |
| D6 | Toggle heatmap open/closed 10x while `RUNNING` | No crash, temps keep updating, run continues |
| D7 | Close the heatmap window via its X while `RUNNING` | Main GUI unaffected, run continues |

## E. Recording / logging

| # | Step | Expected |
|---|------|----------|
| E1 | START RECORDING, run 1 lap, STOP RECORDING | `telemetry_*.csv` created with 68 columns + header |
| E2 | Open the CSV | Rows are contiguous, SOC decreases monotonically, no blank/garbage rows |
| E3 | START RECORDING then close the GUI without stopping | File closes cleanly, no truncation/corruption |

## F. Shutdown / process hygiene

| # | Step | Expected |
|---|------|----------|
| F1 | Close the main GUI while `RUNNING` | All processes exit; console prints `[LOGIC] Process cleanly shutdown.` |
| F2 | Check Task Manager after exit | No orphaned `python.exe` processes left behind |
| F3 | Tick **Trigger Hardware Interlock Fault** on the SIL window | `NI DAQ` + `TEMP SENSOR` pills go OFFLINE (red) |
| F4 | Untick it | Pills return ONLINE (green) |

---

## Fixed GUI-safety risks (steps D1 and D3 are their regression tests)

**Risk #1 — spinbox keystroke could trip a spurious fault. FIXED.**
`create_sidebar_spinbox()` left Qt's default `keyboardTracking=True`, so
`valueChanged` fired on *every keystroke*. Typing `100` into `Max Temp` emitted
`1`, then `10`, then `100` — and the intermediate `1.0` was pushed straight to
the logic process as the live max safe temp, instantly tripping OVERTEMP and
killing the load mid-run. Same hazard on `Max Amp`. Now
`setKeyboardTracking(False)`: the value commits only on Enter/arrows/focus-out.

**Risk #2 — E-STOP button could block the GUI thread. FIXED.**
`gui_cmd_queue` is `Queue(maxsize=10)` and every button used a *blocking*
`.put()`. Spinbox spam (Risk #1 made this worse) could fill those 10 slots while
the logic process was busy — notably during `auto_detect_resistor()`, which
blocks ~2-4s **per COM port** while probing. A full queue meant the E-STOP press
froze the GUI. All commands now route through `send_command_nonblocking()`,
which drops the oldest command rather than blocking, and never evicts a queued
STOP. Covered by `tests/test_gui_dispatch.py`.

### Residual limitation (by design, verify behavior in D3)

Fixing the queue guarantees the **GUI** stays responsive, but the logic process
still cannot *act* on a STOP while it is blocked inside `auto_detect_resistor()`
port scanning. This is acceptable because during a port scan `res_ser` is None —
the resistor Arduino is disconnected, so its own 2s serial watchdog
(`turnONAllRESISTORS()`) has already opened the main contactor and shed the load
in hardware. Confirm this on the bench before trusting it: the software E-STOP
and the Arduino watchdog are independent layers and both must work.
