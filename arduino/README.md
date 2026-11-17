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
sent over serial, plus a main contactor. The word is most significant bank
first, so the last character is bank 1, 0.25 Ω, on pin 5; the full table is in
`docs/hardware_topology.md`. `tests/test_firmware_bank_map.py` checks the
sketch against the host two ways. It reads the pin table, the contactor pin and
the watchdog timeout straight out of this file and compares them with
`COMMAND_BANKS` and `BANK_RELAY_PIN`; that part needs nothing installed and
always runs. It also compiles the sketch for the PC and checks that every
command the host can send puts exactly the intended banks in circuit; that part
needs `g++` or `clang++` on PATH and skips without one. Run it after changing a
pin here.

To check a change still builds for the board, without uploading anything:

```bash
arduino-cli compile --fqbn arduino:avr:uno arduino/resistor_bank_controller
```

(`arduino-cli` ships inside the Arduino IDE, under
`resources/app/lib/backend/resources`. Use the board's own `--fqbn`.)

**This sketch carries two independent safety layers.**

1. *The serial watchdog.* A 2 second serial timeout calls `shedAllLoad()`,
   which opens every relay including the main contactor and sheds the load.
   That runs whether or not the host is healthy, and it is what actually
   protects the rig when the Python side stalls — during a COM-port scan, or if
   the DAQ process hangs. Do not remove or lengthen that timeout without
   understanding what depends on it.
2. *The chip's own watchdog timer.* The serial watchdog is checked by `loop()`,
   so it only works while `loop()` is running. `loop()` resets the hardware
   watchdog every pass; if a pass ever takes more than 2 s the chip restarts,
   `setup()` opens every relay, and the start-up banner tells the host, which
   faults as `CONTROLLER RESET`. Built in on the classic AVR boards (Uno, Nano,
   Mega) and left out elsewhere. **A Nano still on its old bootloader does not
   clear the watchdog after a watchdog restart**: the board then restarts over
   and over until its power is cycled. The relays stay released the whole time,
   so the load stays off, but nothing will run until the board is power-cycled.
   Burning the newer (Optiboot) bootloader removes the caveat.

The sketch acts on exactly four messages: `?WHOAMI`, `alive`, `KILL`, and a
resistance command of exactly 8 binary digits, one per ladder relay. Every
message is identified before anything happens, and only a valid resistance
command closes the main contactor. Anything else (noise, a line cut short, a
word of the wrong length) is rejected with an `Error: Rejected` reply and moves
no relay. **`00000000` is rejected too.** It would bypass every bank at once, a
dead short, and the sketch used to turn it into `00000001`: bank 1 alone, the
heaviest load the ladder has. The host never sends it.

Only `alive` and a valid resistance command reset the watchdog. Garbage on the
line, the handshake and `KILL` do not, so the bank sheds 2 s after real control
traffic stops, however much else is arriving. The host sends `alive` every
0.5 s **while RUNNING only**. That keeps the watchdog fed between rows, so a
slow lap profile or one lost command cannot open the main contactor mid-run;
`alive` itself never moves a relay. In every other state the host sends `KILL`
every 0.5 s instead: the bank must be disconnected whenever the rig is not
running, so it says so again rather than saying `alive`, and a single `KILL`
lost on the wire is repeated half a second later. Because `KILL` does not feed
the watchdog, the watchdog still lapses in FAULT as a second guarantee.

**The host reads what the sketch prints**, without waiting for it
(`CONTROLLER_LINES` in `control_logic.py`). Three lines matter, and their first
words must not be reworded without changing the Python:

| The sketch prints | The host takes it as |
|---|---|
| `Arduino Ready...` more than 5 s after the port was opened | the board restarted: `CONTROLLER RESET` if armed or running |
| `No Signal...` while RUNNING | the sketch's own watchdog shed the load: `CONTROLLER SHED LOAD` |
| `Error: ...` | a line was refused: reported, and while running the setting is sent again, once |

`python tools/relay_timing_check.py` (no hardware, ~20 s) runs the host's
real control loop and replays everything it sends through these rules. It fails
if the main relay would open mid-run, or if the watchdog would go more than 1 s
unfed while the bank is connected. Run it after changing the heartbeat, the lap
playback or these rules. It checks the host's traffic, not the flashed firmware.

Bench check after flashing, with the battery disconnected and the relay supply
on: open the serial monitor at 9600 baud, newline line ending, and send `hello`,
`0101`, `00000000` and `KILL`. Each must leave the main contactor silent.
`00010000` must close it. Then send `hello` about once a second: the contactor
must still drop out about 2 s after `00010000`, with a `No Signal` reply,
because junk does not reset the watchdog. Last, with `00010000` applied, send
`00000000`: the reply must be `Error: Rejected` and no relay may move.

The hardware watchdog cannot be exercised from the serial monitor, because a
healthy `loop()` always resets it. The check that it is not misfiring is that
the board never prints `Arduino Ready` again after start-up while it sits
idle for a minute and through `tools/bench_relay_check.py`'s `walk`.

### temperature_sensor_array

Reads 48 DS18B20 sensors across 6 buses and streams one CSV line per bus:

```
<bus number>,<t1>,<t2>,...,<t8>
```

A disconnected sensor prints `ERR` rather than a number, and the Python parser
skips those readings rather than writing a zero — a zero would drag the max-temp
calculation down and could mask a real overtemp.

Skipping leaves that sensor's last good value in place, so the DAQ also tracks
how long each sensor has gone without a valid reading. Once armed, any sensor
with no reading for longer than the temperature staleness timeout (3 s by
default) faults the rig as `TEMP SENSOR FAULT`, and the console names the bus and
sensor. A sensor that reads `ERR` from power-up counts too, so a known-dead
sensor has to be fixed before the rig can be armed.

**Bus-bar sensors.** The sketch has a place for one more DS18B20 on each bus
bar between two series groups, eleven on a 12S module. They go on pin 8, and
pin 9 as well if one bus is too long, and are sent on a line of their own after
the six cell buses:

```
B,<t1>,<t2>,...,<t11>
```

`BUSBAR_SENSOR_COUNT` is 0 as shipped, which leaves all of it out of the build.
To fit them, fill in `busbarSensors[]` (address, and bus 0 or 1, for each bar in
order), set `BUSBAR_SENSOR_COUNT`, flash, and set `busbar_sensor_count` in
`rig_config.json` to the same number. The host takes the line only when it
carries exactly that many readings. The steps are at the top of that part of
the sketch and in `docs/hardware_topology.md`.

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
