"""
How configuration problems and ARM refusals are worded for the operator.

ARM blockers and advisories behave differently -- the rig refuses to arm on
the first and runs with the second -- so the GUI must never show them as one
undifferentiated list. Pure functions; no window is created.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gui_layout import config_problem_text, arm_refusal_summary
from rig_config import RigConfig


def test_clean_config_shows_nothing():
    assert config_problem_text(RigConfig.defaults()) == ""


def test_blockers_are_labelled_and_listed_before_advisories():
    cfg = RigConfig.defaults()
    cfg.daq.sample_period_s = 0.0                                   # advisory
    cfg.daq.voltage_channels = cfg.daq.voltage_channels[:11]        # blocker
    text = config_problem_text(cfg)
    assert text.startswith("WILL NOT ARM")
    assert text.index("voltage channels") < text.index("WARNING") < text.index("Sample period")


def test_advisories_alone_do_not_claim_to_block():
    cfg = RigConfig.defaults()
    cfg.daq.sample_period_s = 0.0
    text = config_problem_text(cfg)
    assert text.startswith("WARNING")
    assert "WILL NOT ARM" not in text


def test_refusal_summary():
    assert arm_refusal_summary([]) == ""
    assert arm_refusal_summary(["NI-DAQ OFFLINE"]) == "WILL NOT ARM: NI-DAQ OFFLINE"
    assert arm_refusal_summary(["a", "b", "c"]) == "WILL NOT ARM: a  (+2 more)"
