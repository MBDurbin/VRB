"""
The tap matching in tools/tap_map_check.py, without a DAQ: which input reads
which cell tap, and when the readings cannot say.
"""
import importlib.util
import os

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "tools", "tap_map_check.py")
spec = importlib.util.spec_from_file_location("tap_map_check", TOOL)
tap_map_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tap_map_check)

match_taps = tap_map_check.match_taps
suggested_channels = tap_map_check.suggested_channels
floating_inputs = tap_map_check.floating_inputs

WIRED = ["m/ai1", "m/ai2", "m/ai3", "m/ai4", "m/ai5", "m/ai6", "m/ai7", "m/ai8",
         "m/ai9", "m/ai16", "m/ai17", "m/ai18"]


def readings(top=44.0, extra=None):
    """Each wired input at its tap's voltage, plus any `extra` inputs."""
    out = {ch: top * (k + 1) / 12 for k, ch in enumerate(WIRED)}
    out.update(extra or {})
    return out


def test_a_clean_wiring_gives_the_channel_list_tap_1_first():
    per_channel, per_tap = match_taps(readings(), 44.0, 12)
    assert suggested_channels(per_tap) == WIRED
    assert per_channel["m/ai9"] == 9


def test_ground_and_unwired_inputs_are_told_apart():
    per_channel, _ = match_taps(readings(extra={"m/ai0": 0.02, "m/ai10": 1.9}), 44.0, 12)
    assert per_channel["m/ai0"] == 0             # the ground: 0 V
    assert per_channel["m/ai10"] is None         # half a tap from anything


def test_an_input_that_follows_the_read_order_is_floating():
    # Read up, ai10 copies ai9 before it; read down, it copies ai16.
    up = readings(extra={"m/ai10": 44.0 * 9 / 12})
    down = readings(extra={"m/ai10": 44.0 * 10 / 12})
    assert floating_inputs(up, down, 44.0 / 12) == {"m/ai10"}


def test_an_input_copying_its_neighbour_is_flagged_not_guessed():
    # A floating 9205 input read after ai7 holds ai7's voltage.
    _, per_tap = match_taps(readings(extra={"m/ai10": 44.0 * 7 / 12}), 44.0, 12)
    assert per_tap[7] == ["m/ai7", "m/ai10"]
    assert suggested_channels(per_tap) is None


def test_a_missing_tap_gives_no_list():
    r = readings()
    del r["m/ai8"]
    _, per_tap = match_taps(r, 44.0, 12)
    assert per_tap[8] == [] and suggested_channels(per_tap) is None


def test_a_divider_a_little_off_still_matches():
    # 3% low is a third of a cell at tap 12, inside the match window.
    _, per_tap = match_taps({ch: v * 0.97 for ch, v in readings().items()}, 44.0, 12)
    assert suggested_channels(per_tap) == WIRED
