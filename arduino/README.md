# Arduino firmware

The three sketches this rig depends on. **These are now the source of truth** —
open them from here in the Arduino IDE, not from a copy elsewhere on disk.

Each sketch sits in a folder matching its filename, which the Arduino IDE
requires. Opening `resistor_bank_controller/resistor_bank_controller.ino` opens
the sketch correctly; opening a bare `.ino` from a mismatched folder does not.

## The sketches

| Folder | Device | Baud | `?WHOAMI` reply |
|---|---|---|---|
| `resistor_bank_controller/` | Relay ladder driving the resistor bank | 9600 | `RESISTOR_CTRL` |
| `temperature_sensor_array/` | 6 OneWire buses × 8 DS18B20 sensors | 115200 | `TEMP_SENSOR` |
| `ds18b20_address_scanner/` | Bench utility, not part of the running rig | 9600 | — |

All devices are found by COM-port sweep: the host opens each port, sends
`?WHOAMI\n`, and matches the reply. There are no fixed port assignments, so the
reply strings above must not be changed without changing the Python to match
(`auto_detect_resistor()` in `control_logic.py`, and the watchdog thread in
`hardware_manager.py`).

### resistor_bank_controller

Drives the binary ladder — 0.25, 0.5, 1, 2, 4, 8, 16, 32 Ω — from an 8-bit word
sent over serial, plus a main contactor.

**This sketch carries an independent safety layer.** A 2 second serial timeout
calls `turnONAllRESISTORS()`, which opens every relay including the main
contactor and sheds the load. That runs whether or not the host is healthy, and
it is what actually protects the rig when the Python side stalls — during a
COM-port scan, or if the DAQ process hangs. Do not remove or lengthen that
timeout without understanding what depends on it.

The host feeds this watchdog with an `alive\n` heartbeat while idle. Note that
`alive` returns *before* the block that re-closes the main contactor, so a
heartbeat keeps the watchdog fed without re-energising the bank; only a binary
command does that.

### temperature_sensor_array

Reads 48 DS18B20 sensors across 6 buses and streams one CSV line per bus:

```
<bus number>,<t1>,<t2>,...,<t8>
```

A disconnected sensor prints `ERR` rather than a number, and the Python parser
skips those readings rather than writing a zero — a zero would drag the max-temp
calculation down and could mask a real overtemp.

**The 48 sensor ROM addresses are hardcoded in this sketch.** Changing
`sensors_per_bus` or `temp_bus_count` in `rig_config.json` does *not* change what
the firmware reads; the addresses here must be edited and the sketch re-flashed.
Use the address scanner below to find them.

Resolution is set to 9-bit deliberately — a faster conversion matters more than
0.0625 °C precision when sweeping 48 sensors.

### ds18b20_address_scanner

Bench tool. Put a sensor (or a bus of them) on pin 2, open the serial monitor,
and it prints the ROM addresses it finds. Use it to fill in the address tables in
`temperature_sensor_array` after replacing a sensor.

Not flashed to anything during a run.

## Not in this repo

`main_v2.py` probes for a dongle replying `SIL_KEY`, which unlocks
software-in-the-loop mode. That sketch is not in this folder and was not found
alongside the others — if it exists it is on another machine, and it should be
added here.

## Editing these from the Arduino IDE

The IDE defaults to its own sketchbook folder, so it will not see these unless
you point it here. Either:

- **File → Open** and browse to the `.ino` in this folder, or
- **File → Preferences → Sketchbook location**, set it to this `arduino/`
  directory so all three appear under File → Sketchbook.

The second is worth doing once. Editing a copy in `Documents/Arduino` and
flashing that is how firmware and repo drift apart, which is exactly the state
this folder was created to end.
