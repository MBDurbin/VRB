"""
Visual design system for the telemetry GUI.

One place for colour, type and spacing so the interface stays coherent as future
teams extend it. Widgets carry a `variant` property and pick up styling from the
global stylesheet rather than each carrying its own inline CSS string.

Two rules worth keeping if you change any of this:

  * Colour carries meaning. Red means a fault or a hard limit, amber means a
    warning or an armed-but-not-running state, green means healthy or active.
    Nothing decorative uses those three.
  * Live numbers render in a monospace face with tabular figures. Proportional
    digits change width as values update, so readouts jitter and are harder to
    read at a glance on a moving vehicle rig.
"""

# ================= PALETTE =================

BG            = "#0d1117"   # window background
SURFACE       = "#161b22"   # cards, panels
SURFACE_HIGH  = "#21262d"   # raised controls, inputs
SURFACE_HOVER = "#30363d"
BORDER        = "#30363d"
BORDER_SUBTLE = "#21262d"

TEXT          = "#e6edf3"
TEXT_DIM      = "#8b949e"
TEXT_MUTED    = "#6e7681"

ACCENT        = "#2f81f7"   # interactive / informational
ACCENT_HOVER  = "#4a92f8"
ACCENT_DIM    = "#1f6feb"

SUCCESS       = "#3fb950"
SUCCESS_DIM   = "#238636"
WARNING       = "#d29922"
WARNING_DIM   = "#9e6a03"
DANGER        = "#f85149"
DANGER_DIM    = "#da3633"

# Telemetry trace colours, distinguishable for the common colour-vision types.
TRACE_VOLTAGE = "#e3b341"
TRACE_CURRENT = "#39c5cf"

# Thermal map ramp. Multi-stop rather than a straight blue-to-red interpolation:
# blending those two endpoints directly passes through a muddy purple midpoint,
# and mid-range cells become impossible to rank against each other. Routing via
# teal and amber keeps every step of the ramp visually ordered.
HEAT_STOPS = [
    (0.00, "#1f6feb"),  # cool
    (0.35, "#39c5cf"),  # teal
    (0.68, "#d29922"),  # amber
    (1.00, "#f85149"),  # at limit
]
HEAT_COLD     = HEAT_STOPS[0][1]
HEAT_HOT      = HEAT_STOPS[-1][1]


# ================= TYPOGRAPHY =================

FONT_UI = '"Segoe UI", "Inter", system-ui, sans-serif'
FONT_MONO = '"Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace'

SIZE_DISPLAY = 30
SIZE_HEADING = 15
SIZE_BODY = 13
SIZE_SMALL = 12
SIZE_CAPTION = 10


# ================= SPACING =================

GAP_XS = 4
GAP_SM = 8
GAP_MD = 12
GAP_LG = 16
RADIUS = 6
RADIUS_SM = 4


def app_stylesheet() -> str:
    """Global stylesheet. Applied once to the QApplication."""
    return f"""
    QWidget {{
        background-color: {BG};
        color: {TEXT};
        font-family: {FONT_UI};
        font-size: {SIZE_BODY}px;
    }}

    QToolTip {{
        background-color: {SURFACE_HIGH};
        color: {TEXT};
        border: 1px solid {BORDER};
        padding: 6px 8px;
    }}

    /* ---------- Buttons ---------- */

    QPushButton {{
        background-color: {SURFACE_HIGH};
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
        padding: 9px 16px;
        font-size: {SIZE_BODY}px;
        font-weight: 600;
    }}
    QPushButton:hover  {{ background-color: {SURFACE_HOVER}; border-color: {TEXT_MUTED}; }}
    QPushButton:pressed{{ background-color: {SURFACE}; }}
    QPushButton:disabled {{ color: {TEXT_MUTED}; border-color: {BORDER_SUBTLE}; }}

    QPushButton[variant="primary"] {{
        background-color: {ACCENT_DIM}; border-color: {ACCENT}; color: #ffffff;
    }}
    QPushButton[variant="primary"]:hover {{ background-color: {ACCENT_HOVER}; }}

    QPushButton[variant="success"] {{
        background-color: {SUCCESS_DIM}; border-color: {SUCCESS}; color: #ffffff;
    }}
    QPushButton[variant="success"]:hover {{ background-color: {SUCCESS}; }}

    QPushButton[variant="warning"] {{
        background-color: {WARNING_DIM}; border-color: {WARNING}; color: #ffffff;
    }}
    QPushButton[variant="warning"]:hover {{ background-color: {WARNING}; color: #14171c; }}

    /* The E-STOP. Deliberately the loudest control on screen -- do not tone
       this down for visual balance. */
    QPushButton[variant="danger"] {{
        background-color: {DANGER_DIM};
        border: 2px solid {DANGER};
        color: #ffffff;
        font-size: {SIZE_HEADING}px;
        font-weight: 800;
        padding: 9px 22px;
        letter-spacing: 0.5px;
    }}
    QPushButton[variant="danger"]:hover  {{ background-color: {DANGER}; }}
    QPushButton[variant="danger"]:pressed{{ background-color: #b62324; }}

    QPushButton:checked {{
        background-color: {DANGER_DIM}; border-color: {DANGER}; color: #ffffff;
    }}

    /* ---------- Inputs ---------- */

    QDoubleSpinBox, QSpinBox, QLineEdit {{
        background-color: {SURFACE_HIGH};
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: {RADIUS_SM}px;
        padding: 5px 7px;
        font-family: {FONT_MONO};
        font-size: {SIZE_SMALL}px;
        selection-background-color: {ACCENT_DIM};
    }}
    QDoubleSpinBox:focus, QSpinBox:focus, QLineEdit:focus {{ border-color: {ACCENT}; }}
    QDoubleSpinBox:hover, QSpinBox:hover, QLineEdit:hover {{ border-color: {TEXT_MUTED}; }}

    QSpinBox::up-button, QSpinBox::down-button,
    QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
        background-color: {SURFACE_HOVER};
        border: none;
        width: 16px;
    }}
    QSpinBox::up-button:hover, QSpinBox::down-button:hover,
    QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
        background-color: {ACCENT_DIM};
    }}

    QCheckBox {{ spacing: {GAP_SM}px; font-size: {SIZE_SMALL}px; }}
    QCheckBox::indicator {{
        width: 15px; height: 15px;
        border: 1px solid {BORDER};
        border-radius: 3px;
        background-color: {SURFACE_HIGH};
    }}
    QCheckBox::indicator:checked {{
        background-color: {ACCENT_DIM};
        border-color: {ACCENT};
        image: none;
    }}
    QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}

    /* ---------- Structure ---------- */

    QLabel[variant="section"] {{
        color: {TEXT_DIM};
        font-size: {SIZE_CAPTION}px;
        font-weight: 700;
        letter-spacing: 1.2px;
        padding-bottom: {GAP_XS}px;
    }}
    QLabel[variant="caption"] {{
        color: {TEXT_MUTED};
        font-size: {SIZE_CAPTION}px;
        letter-spacing: 0.6px;
        font-weight: 600;
    }}
    QLabel[variant="mono"] {{
        font-family: {FONT_MONO};
        font-size: {SIZE_SMALL}px;
        color: {TEXT};
    }}

    QFrame[variant="card"] {{
        background-color: {SURFACE};
        border: 1px solid {BORDER_SUBTLE};
        border-radius: {RADIUS}px;
    }}
    QFrame[variant="divider"] {{
        background-color: {BORDER_SUBTLE};
        max-height: 1px;
        border: none;
    }}

    /* ---------- Tabs, scroll, dialogs ---------- */

    QTabWidget::pane {{
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
        top: -1px;
        background-color: {SURFACE};
    }}
    QTabBar::tab {{
        background-color: transparent;
        color: {TEXT_DIM};
        padding: 9px 18px;
        border: 1px solid transparent;
        border-top-left-radius: {RADIUS}px;
        border-top-right-radius: {RADIUS}px;
        font-weight: 600;
    }}
    QTabBar::tab:selected {{
        background-color: {SURFACE};
        color: {TEXT};
        border-color: {BORDER};
        border-bottom-color: {SURFACE};
    }}
    QTabBar::tab:hover:!selected {{ color: {TEXT}; }}

    QScrollArea {{ border: none; background-color: transparent; }}
    QScrollBar:vertical {{
        background: transparent; width: 10px; margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {SURFACE_HOVER}; border-radius: 5px; min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {TEXT_MUTED}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

    QDialog {{ background-color: {BG}; }}
    QMessageBox {{ background-color: {SURFACE}; }}
    QMessageBox QLabel {{ color: {TEXT}; }}
    """


# ================= DYNAMIC STYLING HELPERS =================

def restyle(widget, variant):
    """Change a widget's variant and force Qt to re-apply the stylesheet.

    Qt only evaluates property selectors at polish time, so a property changed
    after construction needs an explicit unpolish/polish to take effect.
    """
    if widget.property("variant") == variant:
        return
    widget.setProperty("variant", variant)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


# Foreground / background / border for each FSM state. Keyed by the state string
# the logic process emits.
FSM_COLOURS = {
    "DISCONNECTED": (TEXT_MUTED, SURFACE, BORDER),
    "IDLE":         (TEXT, SURFACE, BORDER),
    "ARMED":        ("#14171c", WARNING, WARNING),
    "RUNNING":      ("#0d1117", SUCCESS, SUCCESS),
    "FAULT":        ("#ffffff", DANGER_DIM, DANGER),
}


def fsm_style(state):
    """Inline style for the state banner. Unknown states fall back to neutral."""
    fg, bg, border = FSM_COLOURS.get(state, FSM_COLOURS["DISCONNECTED"])
    return (
        f"color: {fg}; background-color: {bg}; border: 2px solid {border}; "
        f"border-radius: {RADIUS}px; font-family: {FONT_UI}; "
        f"font-size: 22px; font-weight: 800; letter-spacing: 2px; padding: 10px 14px;"
    )


def pill_style(online):
    """Connection status pill."""
    if online:
        fg, bg, border = SUCCESS, "rgba(63, 185, 80, 0.12)", SUCCESS_DIM
    else:
        fg, bg, border = TEXT_MUTED, "transparent", BORDER
    return (
        f"color: {fg}; background-color: {bg}; border: 1px solid {border}; "
        f"border-radius: 10px; padding: 4px 12px; font-size: {SIZE_CAPTION}px; "
        f"font-weight: 700; letter-spacing: 0.6px;"
    )


def _hex_to_rgb(value):
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def heat_colour(temp_c, max_safe_temp, min_temp=20.0):
    """Map a temperature onto the thermal ramp.

    Returns (hex colour, ratio) where ratio is 0.0 at min_temp and 1.0 at the
    safe limit. Interpolates across HEAT_STOPS so the ramp stays perceptually
    ordered from cool through to at-limit.
    """
    span = max(1e-6, max_safe_temp - min_temp)
    ratio = max(0.0, min(1.0, (temp_c - min_temp) / span))

    for (lo_pos, lo_hex), (hi_pos, hi_hex) in zip(HEAT_STOPS, HEAT_STOPS[1:]):
        if ratio <= hi_pos or hi_pos == 1.0:
            local = 0.0 if hi_pos == lo_pos else (ratio - lo_pos) / (hi_pos - lo_pos)
            local = max(0.0, min(1.0, local))
            lo, hi = _hex_to_rgb(lo_hex), _hex_to_rgb(hi_hex)
            r, g, b = (int(a + (z - a) * local) for a, z in zip(lo, hi))
            return f"#{r:02x}{g:02x}{b:02x}", ratio

    return HEAT_HOT, ratio


def delta_v_style(delta_v, warn=0.15, crit=0.30):
    """Cell imbalance readout. Escalates through healthy / warning / critical."""
    if delta_v > crit:
        fg, bg, border = "#ffffff", DANGER_DIM, DANGER
    elif delta_v > warn:
        fg, bg, border = "#14171c", WARNING, WARNING
    else:
        fg, bg, border = SUCCESS, SURFACE, BORDER_SUBTLE
    return (
        f"color: {fg}; background-color: {bg}; border: 1px solid {border}; "
        f"border-radius: {RADIUS_SM}px; padding: 7px; font-family: {FONT_MONO}; "
        f"font-size: {SIZE_HEADING}px; font-weight: 700;"
    )
