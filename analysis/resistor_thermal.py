"""Transient and radiative temperature of the VRB's heated row, with the fan fixed.

What this answers
-----------------
The fan is fixed at 3500 CFM. How hot does the heated top row get during one
full-output discharge of a module -- and how much does radiation change that,
with the top of the housing open, or under a bare-aluminium pyramid reducer?

CFM Calculator.py answers the inverse, steady-state question (how much air holds
a surface limit). This file imports its geometry and correlations rather than
copying them, so the two can never disagree about the convection.

Model
-----
Load. "Full output" is the ladder at its 0.25 ohm floor, where only Bank 1 --
the four-element top row -- carries current. The bank then takes whatever the
module pushes through 0.25 ohm: the power is V^2/R, not a constant 8 kW. A full
module starts well above 8 kW and sags as it discharges, until the undervoltage
trip ends the run. Pack numbers come from rig_config, so this follows the config.

Element. Each TE2000B is a hollow ceramic tube with the resistance ribbon wound
on its outside. Heat is made at the surface and soaks inward, so early in a run
the surface leads the core by up to ~35 K. That is modelled with 1-D radial
conduction. By the peak, though, the sagging power has let the element catch up
-- the gradient is down to a few K -- so a lumped one-temperature model lands
within a few K of it there, reading slightly high.

Convection. Zukauskas, exactly as in CFM Calculator.py: whole-array aerodynamics,
heated-row area only, bypass removed from the core flow.

Radiation. A Monte Carlo ray tracer over the 3-D geometry. Bare aluminium
reflects 90-95% of thermal radiation like a mirror, so textbook view factors get
it wrong: radiation that meets the housing is not lost, it is sent somewhere
else. The tracer follows each ray through every reflection until a tube absorbs
it, the aluminium absorbs it, or it leaves through an opening. The unheated rows
below are cooled by the airflow, so radiation they absorb still leaves the hot
row -- they act as a sink, and their own temperature is solved for.

Airflow. 3500 CFM is the fan's FREE-AIR rating. Every housing adds resistance the
fan has to push against -- the tube bank, and above all the exit: a reducer
squeezes the whole flow through a small outlet, and that outlet's velocity head
is lost. A propeller wall fan has little pressure to give, so the flow a housing
actually gets can be far below 3500 CFM. system_pressure_Pa() estimates that
resistance; main() reports the peak temperature against DELIVERED flow, so the
real answer comes from reading the fan's own CFM-vs-pressure curve.

Limits -- TE datasheet 9-1773453-2 Rev E, page 3, whose two charts disagree:
  548 K (275 C)  zero-load point of the derating curve: the conventional
                 maximum element temperature.
  713 K (440 C)  70 C maximum full-load ambient plus the rise chart's ~372 C
                 rise at 100% load.

Altitude is modelled: the rig's thinner air (CFM Calculator.SITE_PRESSURE_RATIO,
0.846 for Provo) carries ~18% less mass per CFM than the 1-atm gas table.

Known biases, both directions, with their size (measured here, or by review):
  OPTIMISTIC   fan delivers its free-air 3500 CFM (it cannot through a housing;
               main() reports the peak against delivered flow)
  OPTIMISTIC   the result is the MEAN surface; TE's limits are for the hottest point.
               Local h varies around each tube: a 2-D (r, theta) check put the hot
               side +18 K above the mean for moderate variation, ~+38 K for strong
  OPTIMISTIC   the four elements share the power equally; the lowest-resistance one
               at +/-5% runs +12 to +14 K hotter (main() reports it)
  OPTIMISTIC   uniform flow over the tubes. The middle tubes sit over the fan hub;
               each 10% local velocity deficit adds ~+6 K
  OPTIMISTIC   inlet air at 300 K. The peak moves 0.82 K per K of inlet; a small
               closed lab reheated by the exhaust adds up to ~+6 K
  OPTIMISTIC   openings are black sinks: radiation leaving never comes back
  CONSERVATIVE the top row uses the 3-row average C2 (0.84); as the last row the
               air meets, its own is ~1.0: -16 to -19 K (main() reports it)
  CONSERVATIVE no resistance in the circuit but the pack and the bank: every 5 mOhm
               of relay contacts, cable, shunt and fuse is about -5.5 K
  CONSERVATIVE the bore is adiabatic; the unheated rows sit at their quasi-steady
               temperature, warmer than they really are early in a run
  NEUTRAL      resistance held at nominal. The datasheet allows +/-400 ppm/K, but
               Ni-Cr ribbon is ~+100 ppm/K, which helps slightly (-2 K)
  NEUTRAL      element mass, ceramic cp and k, and the cell OCV curve are estimates
               -- mass is the largest, and is swept
"""

import importlib.util
import math
import os
import sys
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
# rig_config lives one folder up, in the rig app.
sys.path.insert(0, os.path.dirname(_HERE))

from rig_config import RigConfig

_spec = importlib.util.spec_from_file_location("cfm_calculator",
                                               os.path.join(_HERE, "CFM Calculator.py"))
cfm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cfm)

# ------------------------------------------------------------------ inputs --
FAN_CFM = 3500.0                  # the fan's FREE-AIR rating; see "Airflow" above
DELIVERED_CFM_GRID = (3500.0, 3000.0, 2500.0, 2000.0, 1500.0, 1000.0, 750.0, 500.0)

# The fan (24" IDEALHOUSE shutter fan off Amazon) publishes no pressure curve --
# nor do other fans of its class; they quote free-air CFM only. So its curve is
# bracketed instead of guessed: a straight line from FAN_CFM at zero pressure down
# to a stall pressure somewhere in this band, generous for a 1500 RPM propeller
# wall fan. A verdict that holds across the whole band does not depend on it.
FAN_STALL_PA_BAND = (60.0, 100.0, 150.0)          # 0.24, 0.40, 0.60 in. of water
T_AMB_K = cfm.T_IN_K              # room air, and the start temperature of every surface
SIGMA = 5.670374419e-8
R_FLOOR_OHM = 0.25                # ladder minimum: Bank 1 only, the top row

LIMITS_K = {"275 C (derating curve)": 548.15,
            "440 C (rise chart)": 713.15}

EPS_TUBE = 0.90                   # green flame-proof paint; paints sit at 0.85-0.95
EPS_AL_BAND = (0.05, 0.10)        # polished .. mill-finish bare aluminium

# Element resistance tolerance: TE2000B1R0J, J = +/-5%. Worst case for the hottest
# element is one at -5% sharing the row with three at +5%.
ELEMENT_TOLERANCE = 0.05

# Tube-bank pressure drop, dp = N_L * chi * f * rho * V_max^2 / 2 (Incropera eq. 7.65).
# f read off Incropera Fig. 7.14 (staggered, P_T = S_T/D ~ 2.1, Re ~ 3e4); chi ~ 1 for
# P_T/P_L = 0.82. A chart reading, good to ~30% -- and the bank is the SMALL term.
BANK_FRICTION_F = 0.25
BANK_CHI = 1.0

N_HOT = cfm.ACTIVE_HEATED_RESISTORS            # 4, the top row
N_COLD = 7                                     # 3 + 4 in the unheated rows below
ELEMENT_LENGTH = cfm.A_cylinder / (math.pi * cfm.D)   # 0.499 m, consistent with A_cylinder

# Element construction. The datasheet gives no mass; ~2 kg is an estimate for a
# 60 mm x 510 mm alumina-silica tube plus ribbon, and the band brackets it.
ELEMENT_MASS_KG = 2.0
ELEMENT_MASS_BAND = (1.5, 2.0, 3.0)
CERAMIC_DENSITY = 2600.0          # kg/m^3, alumina-silica body
CERAMIC_K = 3.0                   # W/m K, steatite/mullite-type ceramic
# Heat capacity rises steeply with temperature. Incropera Table A.2 (Al2O3).
_CP_T = np.array([300.0, 400.0, 600.0, 800.0, 1000.0])
_CP_V = np.array([765.0, 940.0, 1110.0, 1180.0, 1225.0])

# Resting (open-circuit) voltage of one NMC cell against state of charge. A
# generic 21700 NMC curve -- the RS50 datasheet's own curve should replace it.
OCV_SOC = np.array([0.00, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00])
OCV_V = np.array([3.00, 3.30, 3.42, 3.52, 3.59, 3.65, 3.72, 3.80, 3.89, 3.98, 4.07, 4.20])


# ---------------------------------------------------------- air properties --
# Straight interpolation of Gas_Properties_ATMP.csv. Not get_gas_properties:
# that helper re-reads the file and rounds to six decimals on every call, which
# is both slow for a time-stepped model and, as CFM Calculator.py found, enough
# to make fixed-point loops cycle. Same table, same values, unrounded.
_gas = pd.read_csv(os.path.join(_HERE, "Gas_Properties_ATMP.csv"))
_gas.columns = _gas.columns.str.strip()
_GT = _gas["T (K)"].to_numpy(float)
_GCOL = {key: _gas[col].to_numpy(float) * scale for key, col, scale in (
    ("rho", "rho (kg/m3)", 1.0), ("cp", "Cp (kJ/kg K)", 1e3), ("mu", "mu*10^7 (N s/m2)", 1e-7),
    ("k", "k*10^3 (W/m K)", 1e-3), ("Pr", "Pr", 1.0))}


def air(temp_k):
    t = min(max(float(temp_k), _GT[0]), _GT[-1])
    return {key: float(np.interp(t, _GT, arr)) for key, arr in _GCOL.items()}


# -------------------------------------------------------------- convection --
def core_flow(fan_cfm=FAN_CFM):
    """(mass flow through the tubes, mass flux at the minimum area). Bypass removed.

    At the rig's altitude, not sea level: the fan moves the same volume, but each
    cubic metre carries SITE_PRESSURE_RATIO of the sea-level mass. This is the only
    place density enters the thermal model.
    """
    rho_in = air(T_AMB_K)["rho"] * cfm.SITE_PRESSURE_RATIO
    vol_core = fan_cfm * cfm.CFM_TO_M3S * (1 - cfm.BYPASS_FRACTION)
    return rho_in * vol_core, rho_in * cfm.calc_V_max(vol_core / cfm.duct_area)


def bank_h(T_surface, T_bulk, G_max):
    """Bank-average Zukauskas h, with CFM Calculator.py's own Re and Nu functions."""
    b, s = air(T_bulk), air(T_surface)
    Re = cfm.calc_Re_D(G_max, b["mu"])
    return cfm.calc_Nu_D(Re, b["Pr"], s["Pr"], cfm.NL) * b["k"] / cfm.D


REDUCER_CONTRACTION_K = 0.05     # gradual pyramid contraction, loss per outlet velocity head


def fan_operating_cfm(enc, stall_pa, free_air_cfm=FAN_CFM):
    """Flow the fan settles at when pushing through housing `enc`.

    Fan: pressure falls linearly from stall_pa at zero flow to nothing at its
    free-air rating. Housing: every term in system_pressure_Pa scales with the
    square of the flow. The operating point is where the two meet. Both are in
    standard air, so altitude cancels out of this crossing.
    """
    k = sum(system_pressure_Pa(enc, free_air_cfm)) / free_air_cfm ** 2
    b = stall_pa / free_air_cfm
    return (-b + math.sqrt(b * b + 4 * k * stall_pa)) / (2 * k)


def system_pressure_Pa(enc, fan_cfm=FAN_CFM):
    """(tube-bank drop, outlet penalty) in Pa of STATIC pressure the fan must supply.

    This is what to read against the fan's CFM-vs-static-pressure curve, so it is
    worked out in standard (sea-level) air, the density catalogue curves are
    published at. At altitude the fan's curve and these losses shrink by the same
    ratio, so reading one against the other still gives the right delivered CFM.

    Outlet penalty: a fan rated in free air already discards the velocity head of
    the air it throws out. An open top lets the air leave the box as fast as it
    entered it, so that costs the fan nothing extra. A reducer accelerates the
    whole flow through a smaller outlet; that ADDED velocity head, plus a small
    contraction loss, is the penalty -- and it grows with the square of the flow.
    NOT included: the vent duct beyond a reducer (friction and bends only add),
    and the fan's own inlet losses.
    """
    enc = enc or Enclosure()
    rho = air(T_AMB_K)["rho"]                            # standard air: see above
    vol = fan_cfm * cfm.CFM_TO_M3S
    # From the volume flow directly, not G / rho: core_flow's G is at site density.
    v_max = cfm.calc_V_max(vol * (1 - cfm.BYPASS_FRACTION) / cfm.duct_area)
    dp_bank = cfm.NL * BANK_CHI * BANK_FRICTION_F * rho * v_max ** 2 / 2
    if enc.reducer_height is None:
        return dp_bank, 0.0
    vp_box = rho * (vol / (enc.width * enc.depth)) ** 2 / 2
    vp_out = rho * (vol / enc.reducer_outlet ** 2) ** 2 / 2
    return dp_bank, max(0.0, vp_out - vp_box) + REDUCER_CONTRACTION_K * vp_out


def hot_row_convection_W(T_s, T_air_in, fan_cfm=FAN_CFM, h_factor=1.0):
    """Heat the heated row convects away at surface temperature T_s.

    Effectiveness form, as in CFM Calculator.py: Q = m cp (T_s - T_in)(1 - e^-NTU).
    T_air_in is the air arriving at the top row, warmed slightly by the heat the
    unheated rows below have absorbed as radiation and handed to the airflow.
    h_factor scales the bank-average h, to measure the 3-row-average C2 choice.
    """
    if T_s <= T_air_in:
        return 0.0
    m_dot, G = core_flow(fan_cfm)
    A = N_HOT * cfm.A_cylinder
    T_exit = T_air_in
    for _ in range(200):
        T_mean = 0.5 * (T_air_in + T_exit)
        cp = air(T_mean)["cp"]
        h = h_factor * bank_h(T_s, T_mean, G)
        q = m_dot * cp * (T_s - T_air_in) * -math.expm1(-h * A / (m_dot * cp))
        T_new = T_air_in + q / (m_dot * cp)
        if abs(T_new - T_exit) < 1e-9:
            break
        T_exit = T_new
    return q


# ------------------------------------------------------------- ray tracer --
@dataclass(frozen=True)
class Enclosure:
    """The housing around the tube bank.

    Coordinates: x across the rows, y along the tube axes, z up -- air flows +z.
    z = 0 is the fan plane. reducer_height=None means the top is simply open.

    From the team's Onshape parts: a 24 in cube (0.61 m inside), rows on the
    123.4 mm transverse pitch with the first tube 119.6 mm in from the wall, and
    the 12 in tall, 8 in outlet reducer. The STEP export carried shapes but not
    positions, so the two clearances below are still estimates. Also simplified:
    the real reducer starts its slope from a ~50 mm frame ledge (0.51 m square)
    rather than from the full wall.
    """
    width: float = 0.61
    depth: float = 0.61
    below_bottom_row: float = 0.15    # fan plane to the bottom row's axis
    above_top_row: float = 0.15       # top row's axis to where the box ends / reducer starts
    reducer_height: float = None      # None: open top
    reducer_outlet: float = 0.30      # square outlet side
    eps_al: float = 0.10
    specular: float = 1.0             # share of aluminium reflections that are mirror-like

    def tubes(self):
        """(x, z, heated) per tube. Rows from the bottom: 4, 3 (staggered), 4 heated."""
        x0 = (self.width - 3 * cfm.ST) / 2
        z3 = self.below_bottom_row
        z2, z1 = z3 + cfm.SL, z3 + 2 * cfm.SL
        return ([(x0 + i * cfm.ST, z3, False) for i in range(4)]
                + [(x0 + cfm.ST / 2 + i * cfm.ST, z2, False) for i in range(3)]
                + [(x0 + i * cfm.ST, z1, True) for i in range(4)])

    @property
    def z_box_top(self):
        return self.below_bottom_row + 2 * cfm.SL + self.above_top_row

    def label(self):
        if self.reducer_height is None:
            top = "open top"
        else:
            top = f"reducer {self.reducer_height:.2f} m tall -> {self.reducer_outlet:.2f} m outlet"
        return f"{top}, Al eps {self.eps_al:g}"


def _lambert(normals, rng):
    """Cosine-weighted directions about each unit normal (diffuse emission/reflection)."""
    n = len(normals)
    u1, u2 = rng.random(n), rng.random(n)
    sin_t, cos_t, phi = np.sqrt(u1), np.sqrt(1 - u1), 2 * np.pi * u2
    helper = np.where((np.abs(normals[:, 0]) < 0.9)[:, None], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0])
    t1 = np.cross(normals, helper)
    t1 /= np.linalg.norm(t1, axis=1, keepdims=True)
    t2 = np.cross(normals, t1)
    return ((sin_t * np.cos(phi))[:, None] * t1 + (sin_t * np.sin(phi))[:, None] * t2
            + cos_t[:, None] * normals)


_T_EPS = 1e-9
_TUBE, _CAP, _AL, _OUT_TOP, _OUT_BOTTOM = range(5)


def trace(tube_x, tube_z, tube_hot, y0, y1, width, depth, z_top, reducer_h, reducer_out,
          eps_al, specular, eps_tube, source, n_rays, seed, max_bounces=5000):
    """Follow n_rays emitted diffusely by the `source` tubes ('hot' or 'cold').

    Returns the share absorbed by the hot tubes, the cold tubes and the aluminium,
    the share leaving through the top and the bottom, and any lost to numerical
    leaks (which should be zero). Tubes are cylinders along y from y0 to y1;
    their end caps are perfect mirrors, so rays cannot slip inside a tube and the
    caps absorb nothing, which keeps reciprocity exact.
    """
    rng = np.random.default_rng(seed)
    xc, zc = np.asarray(tube_x, float), np.asarray(tube_z, float)
    hot = np.asarray(tube_hot, bool)
    R = cfm.D / 2
    src = np.flatnonzero(hot if source == "hot" else ~hot)

    # --- emission: uniform over the source tubes' curved surfaces
    k = rng.choice(src, n_rays)
    th = 2 * np.pi * rng.random(n_rays)
    nrm = np.stack([np.cos(th), np.zeros(n_rays), np.sin(th)], axis=1)
    O = np.stack([xc[k] + R * np.cos(th), y0 + (y1 - y0) * rng.random(n_rays),
                  zc[k] + R * np.sin(th)], axis=1)
    d = _lambert(nrm, rng)

    # --- planar surfaces: (point, inward normal, kind, bounds check)
    planes = [((0, 0, 0), (0, 0, 1), _OUT_BOTTOM, None)]
    box = lambda P: (P[:, 2] >= -1e-12) & (P[:, 2] <= z_top + 1e-12)  # noqa: E731
    planes += [((0, 0, 0), (1, 0, 0), _AL, box), ((width, 0, 0), (-1, 0, 0), _AL, box),
               ((0, 0, 0), (0, 1, 0), _AL, box), ((0, depth, 0), (0, -1, 0), _AL, box)]
    if reducer_h is None:
        planes.append(((0, 0, z_top), (0, 0, -1), _OUT_TOP, None))
    else:
        kx, ky = (width - reducer_out) / (2 * reducer_h), (depth - reducer_out) / (2 * reducer_h)
        inz = lambda P: (P[:, 2] >= z_top - 1e-12) & (P[:, 2] <= z_top + reducer_h + 1e-12)  # noqa: E731
        sx = lambda P: kx * (P[:, 2] - z_top)  # noqa: E731
        sy = lambda P: ky * (P[:, 2] - z_top)  # noqa: E731
        in_y = lambda P: inz(P) & (P[:, 1] >= sy(P) - 1e-9) & (P[:, 1] <= depth - sy(P) + 1e-9)  # noqa: E731
        in_x = lambda P: inz(P) & (P[:, 0] >= sx(P) - 1e-9) & (P[:, 0] <= width - sx(P) + 1e-9)  # noqa: E731
        u = lambda v: tuple(np.asarray(v) / np.linalg.norm(v))  # noqa: E731
        planes += [((0, 0, z_top), u((1, 0, -kx)), _AL, in_y),
                   ((width, 0, z_top), u((-1, 0, -kx)), _AL, in_y),
                   ((0, 0, z_top), u((0, 1, -ky)), _AL, in_x),
                   ((0, depth, z_top), u((0, -1, -ky)), _AL, in_x)]
        half = reducer_out / 2
        outlet = lambda P: ((np.abs(P[:, 0] - width / 2) <= half + 1e-9)  # noqa: E731
                            & (np.abs(P[:, 1] - depth / 2) <= half + 1e-9))
        planes.append(((0, 0, z_top + reducer_h), (0, 0, -1), _OUT_TOP, outlet))
    P0 = np.array([p[0] for p in planes], float)
    PN = np.array([p[1] for p in planes], float)

    tally = dict(hot=0, cold=0, al=0, out_top=0, out_bottom=0, lost=0)
    active = np.arange(n_rays)
    for _ in range(max_bounces):
        if active.size == 0:
            break
        o, v = O[active], d[active]
        m = active.size
        best_t = np.full(m, np.inf)
        best_kind = np.full(m, -1)
        best_tube = np.full(m, -1)
        best_n = np.zeros((m, 3))

        # curved tube surfaces
        ox, oz = o[:, 0:1] - xc[None, :], o[:, 2:3] - zc[None, :]
        a = v[:, 0:1] ** 2 + v[:, 2:3] ** 2
        b = 2 * (ox * v[:, 0:1] + oz * v[:, 2:3])
        c = ox ** 2 + oz ** 2 - R * R
        disc = b * b - 4 * a * c
        with np.errstate(invalid="ignore", divide="ignore"):
            t = (-b - np.sqrt(disc)) / (2 * a)
        yh = o[:, 1:2] + t * v[:, 1:2]
        ok = (disc > 0) & (a > 1e-14) & (t > _T_EPS) & (yh >= y0) & (yh <= y1)
        t = np.where(ok, t, np.inf)
        j = np.argmin(t, axis=1)
        tj = t[np.arange(m), j]
        take = tj < best_t
        best_t[take], best_kind[take], best_tube[take] = tj[take], _TUBE, j[take]

        # end caps (mirrors): y = y0 faces -y, y = y1 faces +y
        for yc, sgn in ((y0, 1.0), (y1, -1.0)):
            with np.errstate(invalid="ignore", divide="ignore"):
                tc = (yc - o[:, 1:2]) / v[:, 1:2]
            approaching = (sgn * v[:, 1:2]) > 0
            hx = o[:, 0:1] + tc * v[:, 0:1] - xc[None, :]
            hz = o[:, 2:3] + tc * v[:, 2:3] - zc[None, :]
            ok = approaching & (tc > _T_EPS) & (hx * hx + hz * hz <= R * R)
            tc = np.where(ok, tc, np.inf).min(axis=1)
            take = tc < best_t
            best_t[take], best_kind[take] = tc[take], _CAP
            best_n[take] = (0.0, -sgn, 0.0)

        # planes
        for i in range(len(planes)):
            dn = v @ PN[i]
            with np.errstate(invalid="ignore", divide="ignore"):
                tp = ((P0[i] - o) @ PN[i]) / dn
            ok = (dn < 0) & (tp > _T_EPS)
            if planes[i][3] is not None and ok.any():
                hit = o + np.where(ok, tp, 0.0)[:, None] * v
                ok &= planes[i][3](hit)
            tp = np.where(ok, tp, np.inf)
            take = tp < best_t
            best_t[take], best_kind[take] = tp[take], planes[i][2]
            best_n[take] = PN[i]

        P = o + np.where(np.isfinite(best_t), best_t, 0.0)[:, None] * v
        roll = rng.random(m)
        survive = np.zeros(m, bool)

        lost = best_kind < 0
        tally["lost"] += int(lost.sum())
        tally["out_top"] += int((best_kind == _OUT_TOP).sum())
        tally["out_bottom"] += int((best_kind == _OUT_BOTTOM).sum())

        tube = best_kind == _TUBE
        absorbed = tube & (roll < eps_tube)
        hit_hot = hot[np.where(absorbed, best_tube, 0)] & absorbed
        tally["hot"] += int(hit_hot.sum())
        tally["cold"] += int((absorbed & ~hit_hot).sum())
        refl = tube & ~absorbed
        if refl.any():
            jt = best_tube[refl]
            n_out = np.stack([(P[refl, 0] - xc[jt]) / R, np.zeros(refl.sum()),
                              (P[refl, 2] - zc[jt]) / R], axis=1)
            v[refl] = _lambert(n_out, rng)
            survive |= refl

        cap = best_kind == _CAP
        v[cap, 1] *= -1.0
        survive |= cap

        al = best_kind == _AL
        al_abs = al & (roll < eps_al)
        tally["al"] += int(al_abs.sum())
        al_ref = al & ~al_abs
        if al_ref.any():
            mirror = al_ref & (rng.random(m) < specular)
            if mirror.any():
                vn = np.einsum("ij,ij->i", v[mirror], best_n[mirror])
                v[mirror] = v[mirror] - 2 * vn[:, None] * best_n[mirror]
            diffuse = al_ref & ~mirror
            if diffuse.any():
                v[diffuse] = _lambert(best_n[diffuse], rng)
            survive |= al_ref

        O[active], d[active] = P, v
        active = active[survive]
    tally["lost"] += int(active.size)          # still bouncing at the cap: count, never hide
    return {key: val / n_rays for key, val in tally.items()}


@lru_cache(maxsize=None)
def exchange_factors(enc, n_rays=200_000, seed=1):
    """Gebhart factors for the heated row and the unheated rows in enclosure `enc`."""
    tubes = enc.tubes()
    y0 = (enc.depth - ELEMENT_LENGTH) / 2
    args = ([t[0] for t in tubes], [t[1] for t in tubes], [t[2] for t in tubes],
            y0, y0 + ELEMENT_LENGTH, enc.width, enc.depth, enc.z_box_top,
            enc.reducer_height, enc.reducer_outlet, enc.eps_al, enc.specular, EPS_TUBE)
    return {"hot": trace(*args, "hot", n_rays, seed),
            "cold": trace(*args, "cold", n_rays, seed + 1)}


# --------------------------------------------------------------- radiation --
def radiation(T_h, ex, fan_cfm=FAN_CFM):
    """Net radiation leaving the heated row, and the unheated rows' temperature.

    Returns (Q_rad_hot_W, T_cold_K, Q_cold_to_air_W). With ex=None radiation is
    switched off -- the "reflective housing makes it inconsequential" assumption.

    Exchange between surfaces i and j is eps_i A_i G_ij sigma (T_i^4 - T_j^4),
    with G the Gebhart factors from the tracer. Openings are black at room
    temperature. The aluminium is taken to sit at room temperature: it absorbs
    only 5-10% of what reaches it, and air sweeps both of its faces.
    The unheated rows settle where the radiation they absorb from the hot row
    equals what they lose -- mostly by convection into the passing air, the rest
    by radiation out of the openings and into the aluminium. Quasi-steady: they
    are treated as instantly at that balance, which overstates how warm they get
    early in a run and so understates their pull on the hot row -- conservative.
    """
    if ex is None:
        return 0.0, T_AMB_K, 0.0
    A_h, A_c = N_HOT * cfm.A_cylinder, N_COLD * cfm.A_cylinder
    gh, gc = ex["hot"], ex["cold"]
    _, G = core_flow(fan_cfm)
    ta4 = T_AMB_K ** 4

    def cold_balance(T_c):
        gain = EPS_TUBE * SIGMA * A_h * gh["cold"] * (T_h ** 4 - T_c ** 4)
        rad_out = EPS_TUBE * SIGMA * A_c * (gc["al"] + gc["out_top"] + gc["out_bottom"]) * (T_c ** 4 - ta4)
        conv = bank_h(T_c, T_AMB_K, G) * A_c * (T_c - T_AMB_K)
        return gain - rad_out - conv, conv

    lo, hi = T_AMB_K, max(T_h, T_AMB_K)
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if cold_balance(mid)[0] > 0:
            lo = mid
        else:
            hi = mid
    T_c = 0.5 * (lo + hi)
    q_h = EPS_TUBE * SIGMA * A_h * (gh["cold"] * (T_h ** 4 - T_c ** 4)
                                    + (gh["al"] + gh["out_top"] + gh["out_bottom"]) * (T_h ** 4 - ta4))
    return q_h, T_c, cold_balance(T_c)[1]


def hot_row_loss(T_h, ex, fan_cfm=FAN_CFM, h_factor=1.0):
    """Total heat leaving the heated row at surface temperature T_h, and its split."""
    q_rad, T_c, q_cold_air = radiation(T_h, ex, fan_cfm)
    m_dot, _ = core_flow(fan_cfm)
    T_air_in = T_AMB_K + q_cold_air / (m_dot * air(T_AMB_K)["cp"])
    q_conv = hot_row_convection_W(T_h, T_air_in, fan_cfm, h_factor)
    return q_conv + q_rad, {"conv_W": q_conv, "rad_W": q_rad, "T_cold_K": T_c, "T_air_in_K": T_air_in}


def steady_state(power_W, ex, fan_cfm=FAN_CFM):
    """Surface temperature the heated row settles at under a constant power."""
    lo, hi = T_AMB_K, float(_GT[-1])
    if hot_row_loss(hi, ex, fan_cfm)[0] < power_W:
        return float("inf"), None
    for _ in range(70):
        mid = 0.5 * (lo + hi)
        if hot_row_loss(mid, ex, fan_cfm)[0] < power_W:
            lo = mid
        else:
            hi = mid
    T = 0.5 * (lo + hi)
    return T, hot_row_loss(T, ex, fan_cfm)[1]


# ---------------------------------------------------------------- discharge --
def full_output_profile(dt=0.5, config=None, r_bank=R_FLOOR_OHM):
    """Module discharging into the bank's floor resistance until the undervoltage trip.

    Returns (t, power, current, terminal volts) arrays. Power is I^2 R: a fuller
    module drives more current through the same resistance, so it starts high and
    sags. The run ends when the loaded voltage reaches limits.min_volts -- the
    same trip that ends a real run. r_bank defaults to the nominal 0.25 ohm.
    """
    cfg = config or RigConfig.load()
    pack, lim = cfg.pack, cfg.limits
    soc, t, rows = 1.0, 0.0, []
    while soc > 0:
        v_oc = pack.series_count * float(np.interp(soc, OCV_SOC, OCV_V))
        i = v_oc / (r_bank + pack.resistance_ohm)
        v = i * r_bank
        if v <= lim.min_volts or i >= lim.max_amps:
            break
        rows.append((t, i * i * r_bank, i, v))
        soc -= i * dt / 3600.0 / pack.capacity_ah
        t += dt
    return tuple(np.array(col) for col in zip(*rows))


def constant_power_profile(power_W=8000.0, energy_wh=None, dt=0.5, config=None):
    """Flat power carrying `energy_wh` (default: the module's nameplate energy).

    Pass the energy a full-output run actually delivers to compare power SHAPE
    alone; the nameplate figure is a few percent higher and would add heat.
    """
    if energy_wh is None:
        energy_wh = (config or RigConfig.load()).pack.energy_wh
    t = np.arange(0.0, energy_wh * 3600.0 / power_W, dt)
    return t, np.full(t.size, power_W)


def hottest_element(tolerance=ELEMENT_TOLERANCE, r_element=4 * R_FLOOR_OHM, n=None):
    """(row resistance, hottest element's share of the row power) at a tolerance.

    Worst case: one element at the bottom of its tolerance, the rest at the top.
    Parallel elements see the same voltage, so each takes V^2/R_i and the lowest
    resistance runs hottest. The row resistance rises slightly too, which trims
    the total power -- both effects are returned so neither is double-counted.
    """
    n = n or N_HOT
    r_lo, r_hi = r_element * (1 - tolerance), r_element * (1 + tolerance)
    g = 1 / r_lo + (n - 1) / r_hi
    return 1 / g, (1 / r_lo) / g


# ------------------------------------------------------------------ element --
@dataclass(frozen=True)
class Element:
    """One TE2000B, modelled as a hollow ceramic tube heated at its outer surface."""
    mass_kg: float = ELEMENT_MASS_KG
    k: float = CERAMIC_K
    density: float = CERAMIC_DENSITY
    nodes: int = 20                    # 1 = lumped, for comparison

    @property
    def r_outer(self):
        return cfm.D / 2

    @property
    def r_inner(self):
        bore2 = self.r_outer ** 2 - self.mass_kg / (self.density * math.pi * ELEMENT_LENGTH)
        if bore2 <= 0:
            raise ValueError(f"{self.mass_kg} kg does not fit in a {cfm.D * 1e3:.0f} mm tube")
        return math.sqrt(bore2)


def cp_ceramic(T):
    return np.interp(T, _CP_T, _CP_V)


def loss_table(ex, fan_cfm=FAN_CFM, T_max=None, step=2.0, h_factor=1.0):
    """hot_row_loss on a grid, so the time-stepper interpolates instead of solving."""
    T = np.arange(T_AMB_K, (T_max or float(_GT[-1])) + step, step)
    return T, np.array([hot_row_loss(x, ex, fan_cfm, h_factor)[0] for x in T])


def run_transient(t_prof, P_prof, table, element=Element(), t_after=60.0):
    """Surface and core temperature of one heated element through a discharge.

    Every element starts at room temperature. The row's power and losses are
    split evenly over the four elements. The inner bore is taken as adiabatic,
    which is slightly conservative.
    Returns (t, T_surface, T_core, P) sampled once per second.
    """
    T_grid, Q_grid = table
    t_end = t_prof[-1] + t_after

    if element.nodes == 1:
        C_fixed = None
        r = np.array([element.r_outer])
    else:
        r = np.linspace(element.r_inner, element.r_outer, element.nodes)
        dr = r[1] - r[0]
        r_lo = np.maximum(r - dr / 2, r[0])
        r_hi = np.minimum(r + dr / 2, r[-1])
        vol = math.pi * (r_hi ** 2 - r_lo ** 2) * ELEMENT_LENGTH
        G = 2 * math.pi * element.k * ELEMENT_LENGTH / np.log(r[1:] / r[:-1])
        C_fixed = element.density * vol
        g_sum = np.zeros(r.size)
        g_sum[:-1] += G
        g_sum[1:] += G
        dt = 0.4 * float((C_fixed * _CP_V[0] / g_sum).min())

    if element.nodes == 1:
        dt = 0.5
    T = np.full(r.size, T_AMB_K)
    n_steps = int(math.ceil(t_end / dt))
    out_t, out_s, out_c, out_p = [], [], [], []
    next_sample = 0.0
    for step in range(n_steps + 1):
        t = step * dt
        P_row = float(np.interp(t, t_prof, P_prof, right=0.0)) if t <= t_prof[-1] else 0.0
        q_net = (P_row - float(np.interp(T[-1], T_grid, Q_grid))) / N_HOT
        if t >= next_sample:
            out_t.append(t)
            out_s.append(T[-1])
            out_c.append(T[0])
            out_p.append(P_row)
            next_sample += 1.0
        if element.nodes == 1:
            T = T + dt * q_net / (element.mass_kg * cp_ceramic(T))
        else:
            flow = G * (T[1:] - T[:-1])              # W from node j+1 into node j
            dQ = np.zeros(r.size)
            dQ[:-1] += flow
            dQ[1:] -= flow
            dQ[-1] += q_net                          # surface: generation minus losses
            T = T + dt * dQ / (C_fixed * cp_ceramic(T))
    return np.array(out_t), np.array(out_s), np.array(out_c), np.array(out_p)


# --------------------------------------------------------------- scenarios --
# The housing cases compared throughout. The reducer is the one in the team's
# Onshape assembly (VRB Assembly.zip): four sloped faces bolted to the top frame,
# 12 in tall, closing to an 8 in x 8 in outlet. The STEP export carried each
# part's shape but not its position, so the clearances above and below the tube
# rows are still estimates; radiation is insensitive to them (see main()).
REDUCER_HEIGHT_M = 12 * 0.0254
REDUCER_OUTLET_M = 8 * 0.0254
HOUSINGS = {
    "Convection only": None,
    "Open top": Enclosure(eps_al=0.10),
    "8 in reducer": Enclosure(reducer_height=REDUCER_HEIGHT_M, reducer_outlet=REDUCER_OUTLET_M, eps_al=0.10),
}


@lru_cache(maxsize=None)
def _table(enc, fan_cfm=FAN_CFM, h_factor=1.0):
    return loss_table(None if enc is None else exchange_factors(enc), fan_cfm, h_factor=h_factor)


def peak(t, Ts):
    i = int(np.argmax(Ts))
    return Ts[i], t[i]


def margins(T):
    return "  ".join(f"{T_lim - T:+6.0f} K" for T_lim in LIMITS_K.values())


def main():
    cfg = RigConfig.load()
    t_full, P_full, I_full, V_full = full_output_profile(config=cfg)
    E_full = np.trapezoid(P_full, t_full) / 3600

    print(f"VRB heated-row temperature -- fan fixed at {FAN_CFM:.0f} CFM, "
          f"{cfg.pack.cell_model} {cfg.pack.series_count}S{cfg.pack.parallel_count}P module, "
          f"air at {cfm.SITE_PRESSURE_RATIO:g} atm (altitude)")
    print(f"Limits: " + ", ".join(f"{k} = {v:.0f} K" for k, v in LIMITS_K.items()))
    print(f"\nFull output (0.25 ohm floor): {P_full[0] / 1e3:.2f} kW at {I_full[0]:.0f} A falling to "
          f"{P_full[-1] / 1e3:.2f} kW at the {cfg.limits.min_volts:g} V trip, "
          f"{t_full[-1] / 60:.1f} min, {E_full:.0f} Wh")

    print(f"\nSTEADY STATE at 8 kW, forever, if the fan really moved {FAN_CFM:.0f} CFM")
    print(f"  {'housing':20} {'T_s':>7} {'':6} {'radiation':>9} {'lower rows':>11}   margin to 275 C / 440 C")
    for name, enc in HOUSINGS.items():
        ex = None if enc is None else exchange_factors(enc)
        T, split = steady_state(8000.0, ex)
        print(f"  {name:20} {T:6.0f} K ({T - 273.15:3.0f} C) {split['rad_W'] / 80:7.0f} %  "
              f"{split['T_cold_K']:8.0f} K    {margins(T)}")

    print(f"\nONE FULL DISCHARGE from room temperature, at the free-air {FAN_CFM:.0f} CFM -- peak surface")
    print(f"  {'housing':20} " + "".join(f"{f'{m:g} kg element':>22}" for m in ELEMENT_MASS_BAND)
          + "    margin at 2 kg")
    runs = {}
    for name, enc in HOUSINGS.items():
        cells = []
        for m in ELEMENT_MASS_BAND:
            t, Ts, Tc, P = run_transient(t_full, P_full, _table(enc), Element(mass_kg=m))
            runs[(name, m)] = (t, Ts, Tc, P)
            T_pk, _ = peak(t, Ts)
            cells.append(f"{T_pk:6.0f} K ({T_pk - 273.15:3.0f} C)")
        T2, _ = peak(*runs[(name, 2.0)][:2])
        print(f"  {name:20} " + "".join(f"{c:>22}" for c in cells) + f"    {margins(T2)}")

    # ---- airflow: what each housing costs the fan, and what flow it needs
    print(f"\nAIRFLOW -- static pressure each housing needs from the fan at {FAN_CFM:.0f} CFM "
          f"(standard air, as fan curves are published)")
    print(f"  {'housing':34} {'tube bank':>10} {'outlet':>8} {'total':>8}")
    shown = {}
    for name, enc in HOUSINGS.items():
        key = "open top" if enc is None or enc.reducer_height is None else enc.label().split(",")[0]
        if key in shown:
            continue
        dp_b, dp_e = system_pressure_Pa(enc)
        shown[key] = dp_b + dp_e
        print(f"  {key:34} {dp_b:7.0f} Pa {dp_e:5.0f} Pa {dp_b + dp_e:5.0f} Pa  "
              f"({(dp_b + dp_e) / 249.09:.2f} in. water)")
    print("  A reducer's vent duct adds its own friction and bends on top. 3500 CFM is the fan's")
    print("  FREE-AIR figure: read its CFM-vs-static-pressure curve at these totals for the real flow.")

    print(f"\n  Peak surface through one full discharge, against the flow ACTUALLY delivered")
    grid = sorted(DELIVERED_CFM_GRID)
    sweep = {}
    print(f"  {'delivered CFM':>14} " + "".join(f"{n + ' ' + str(m) + ' kg':>28}"
                                                 for n in HOUSINGS for m in (2.0, 1.5)))
    for fan in grid[::-1]:
        row = []
        for name, enc in HOUSINGS.items():
            tab = _table(enc, fan)
            for m in (2.0, 1.5):
                pk, _ = peak(*run_transient(t_full, P_full, tab, Element(mass_kg=m))[:2])
                sweep.setdefault((name, m), []).append((fan, pk))
                row.append(f"{pk:6.0f} K ({pk - 273.15:3.0f} C)")
        print(f"  {fan:14.0f} " + "".join(f"{c:>28}" for c in row))

    lim = LIMITS_K["275 C (derating curve)"]
    print(f"\n  Least delivered flow that keeps the peak under 275 C ({lim:.0f} K):")
    for (name, m), pts in sweep.items():
        cfm_, pk = zip(*sorted(pts))                       # ascending flow, falling peak
        if pk[-1] > lim:
            need = f"more than {cfm_[-1]:.0f} CFM -- fails even at the fan's free-air rating"
        elif pk[0] <= lim:
            need = f"under {cfm_[0]:.0f} CFM"
        else:
            need = f"~{np.interp(lim, pk[::-1], cfm_[::-1]):.0f} CFM"
        print(f"    {name:20} {m:g} kg: {need}")

    # ---- the flow this fan will really give each housing
    print(f"\nWHAT THE FAN WILL ACTUALLY DELIVER -- its stall pressure is unpublished, so bracketed")
    print(f"  {'housing':18} {'stall pressure':>16} {'delivered':>11} {'2 kg peak':>16} {'1.5 kg peak':>16}")
    for name, enc in HOUSINGS.items():
        for p0 in FAN_STALL_PA_BAND:
            q = fan_operating_cfm(enc, p0)
            cells = []
            for m in (2.0, 1.5):
                cfm_, pk = zip(*sorted(sweep[(name, m)]))
                off_grid = q < cfm_[0]
                T = float(np.interp(q, cfm_, pk))
                cells.append(f"{'>' if off_grid else ''}{T:4.0f} K ({T - 273.15:3.0f} C)")
            print(f"  {name:18} {p0:8.0f} Pa ({p0 / 249.09:.2f}\") {q:7.0f} CFM " + "".join(f"{c:>17}" for c in cells))
    for name, enc in HOUSINGS.items():
        if enc is None or enc.reducer_height is None:
            continue
        cfm_, pk = zip(*sorted(sweep[(name, 2.0)]))
        if pk[-1] <= lim:
            q_need = float(np.interp(lim, pk[::-1], cfm_[::-1]))
            p_need = sum(system_pressure_Pa(enc, q_need))
            print(f"  For the {name} to pass at 2 kg the fan must push ~{q_need:.0f} CFM against "
                  f"{p_need:.0f} Pa ({p_need / 249.09:.1f} in. of water).")
            if p_need > max(FAN_STALL_PA_BAND):
                print(f"  That is beyond even the top of the stall band ({max(FAN_STALL_PA_BAND):.0f} Pa): "
                      f"blower territory, not a propeller wall fan.")

    # ---- the hottest element, not the average one
    print(f"\nHOTTEST ELEMENT -- tolerance puts more power in the lowest-resistance element "
          f"(2 kg, {FAN_CFM:.0f} CFM)")
    for tol in (0.02, ELEMENT_TOLERANCE):
        r_row, share = hottest_element(tol)
        t_t, P_t, _, _ = full_output_profile(config=cfg, r_bank=r_row)
        cells = []
        for name, enc in HOUSINGS.items():
            base, _ = peak(*runs[(name, 2.0)][:2])
            pk, _ = peak(*run_transient(t_t, P_t * share * N_HOT, _table(enc), Element())[:2])
            cells.append(f"{name} {pk:.0f} K ({pk - base:+.0f})")
        print(f"  +/-{tol * 100:.0f}% worst case, hottest takes {share * 100:.1f}% of the row:  "
              + ",  ".join(cells))

    # ---- the conservatism running the other way
    print(f"\nTOP-ROW h -- the model uses the 3-row average C2 = 0.84; as the last row the air meets")
    print(f"  the top row's own is ~1.0, i.e. h x {1 / 0.84:.2f}. Size of that conservatism (2 kg):")
    cells = []
    for name, enc in HOUSINGS.items():
        base, _ = peak(*runs[(name, 2.0)][:2])
        pk, _ = peak(*run_transient(t_full, P_full, _table(enc, FAN_CFM, 1 / 0.84), Element())[:2])
        cells.append(f"{name} {pk:.0f} K ({pk - base:+.0f})")
    print("  " + ",  ".join(cells))

    print(f"\nCOMPARISONS at 2 kg, open top")
    enc = HOUSINGS["Open top"]
    base, t_base = peak(*runs[("Open top", 2.0)][:2])
    t8, P8 = constant_power_profile(8000.0, energy_wh=E_full, config=cfg)
    T8, _ = peak(*run_transient(t8, P8, _table(enc), Element())[:2])
    tl, Tsl, _, _ = run_transient(t_full, P_full, _table(enc), Element(nodes=1))
    Tl, _ = peak(tl, Tsl)
    t1, Ts1, Tc1, _ = runs[("Open top", 2.0)]
    i_pk = int(np.argmax(Ts1))
    grad_pk = Ts1[i_pk] - Tc1[i_pk]
    grad_max = float(np.max(Ts1 - Tc1))
    print(f"  full output (V^2/R)            {base:6.0f} K")
    print(f"  flat 8 kW, same {E_full:.0f} Wh       {T8:6.0f} K   ({T8 - base:+.0f} K: the power's shape alone)")
    # The surface leads the bore by up to grad_max early in a run, but by the peak
    # the sagging power has let the element even out. The lumped model then reads
    # a little HIGH: the early overshoot shed heat the lumped element kept.
    print(f"  lumped element (no gradient)   {Tl:6.0f} K   ({Tl - base:+.0f} K; the surface leads the "
          f"bore by up to {grad_max:.0f} K early, only {grad_pk:.0f} K at the peak)")

    print(f"\nRADIATION-ONLY SENSITIVITY at 2 kg and {FAN_CFM:.0f} CFM (the flow penalty of an")
    print(f"  outlet is in AIRFLOW above, not here) -- including the clearance the STEP could not give")
    r_base, _ = peak(*runs[("8 in reducer", 2.0)][:2])
    red = dict(reducer_height=REDUCER_HEIGHT_M, reducer_outlet=REDUCER_OUTLET_M)
    variants = [
        ("reducer, polished Al (eps 0.05)", Enclosure(eps_al=0.05, **red)),
        ("reducer, diffuse Al reflection", Enclosure(eps_al=0.10, specular=0.0, **red)),
        ("reducer, top row 0.08 m below it", Enclosure(eps_al=0.10, above_top_row=0.08, **red)),
        ("reducer, top row 0.25 m below it", Enclosure(eps_al=0.10, above_top_row=0.25, **red)),
    ]
    print(f"  {'8 in reducer (baseline)':34} {r_base:6.0f} K")
    for label, e in variants:
        T, _ = peak(*run_transient(t_full, P_full, _table(e), Element())[:2])
        print(f"  {label:34} {T:6.0f} K   ({T - r_base:+.0f} K)")

    # ---- runs back to back: how long the bank must rest before the next one
    print(f"\nBACK TO BACK -- a second full discharge after a rest (2 kg, {FAN_CFM:.0f} CFM, fan left on)")
    t_run = t_full[-1]
    for rest_min in (2, 5, 10, 20):
        gap = rest_min * 60.0
        t2 = np.concatenate([t_full, [t_run + 0.5, t_run + gap - 0.5], t_full + t_run + gap])
        P2 = np.concatenate([P_full, [0.0, 0.0], P_full])
        row = []
        for name, enc in HOUSINGS.items():
            if enc is None:
                continue
            tt, Ts, _, _ = run_transient(t2, P2, _table(enc), Element())
            second = tt > t_run + gap
            pk2 = float(Ts[second].max())
            first, _ = peak(*runs[(name, 2.0)][:2])
            row.append(f"{name} {pk2:.0f} K ({pk2 - first:+.0f})")
        print(f"  rest {rest_min:>2} min:  " + ",  ".join(row))

    print("\nNOT MODELLED against the 275 C line: the hottest side of each tube runs above the mean")
    print("  surface reported here -- a 2-D (r, theta) check put it +18 K for moderate variation of")
    print("  h around the tube, up to ~+38 K for strong. The ribbon under the paint runs 1-14 K above")
    print("  the paint's surface, but TE's charts are surface figures, so that does not apply to them.")
    return runs, (t_full, P_full), sweep


# ------------------------------------------------------------------- plots --
# One colour per housing, fixed, the same in every chart. Validated as a
# categorical set (all pairs pass CVD and normal-vision separation). Aqua is
# under 3:1 against the surface, so every line carries a direct label.
_COLOUR = {"Convection only": "#2a78d6", "Open top": "#eb6834", "8 in reducer": "#1baf7a"}
_SURFACE, _INK, _INK2, _MUTED, _GRID, _AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"


def _style(ax):
    ax.set_facecolor(_SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(_AXIS)
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(colors=_MUTED, labelcolor=_INK2, labelsize=9)
    ax.grid(axis="y", color=_GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def _limit_label(name, T):
    """'275 °C derating curve' from the LIMITS_K entry (its key keeps plain ASCII for consoles)."""
    return f"{T - 273.15:.0f} °C {name.split('(')[-1].rstrip(')')}"


def _limit_lines(ax, x_text=None):
    """Dashed TE limit lines. With x_text=None the labels sit just outside the right
    edge, for charts whose lines run through the space a left-hand label would use."""
    for name, T in LIMITS_K.items():
        c = T - 273.15
        ax.axhline(c, color=_INK2, linewidth=1.0, linestyle=(0, (5, 3)), zorder=1)
        if x_text is None:
            ax.annotate(f"{T - 273.15:.0f} °C\n{name.split('(')[-1].rstrip(')')}", xy=(1.0, c),
                        xycoords=("axes fraction", "data"), xytext=(6, 0), textcoords="offset points",
                        color=_INK2, fontsize=8.5, va="center", annotation_clip=False)
        else:
            ax.text(x_text, c + 5, f"TE limit: {_limit_label(name, T)}", color=_INK2, fontsize=8.5,
                    va="bottom")


def plot_results(runs, profile, sweep=None, prefix="Thermal"):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.family"] = ["Segoe UI", "DejaVu Sans", "sans-serif"]

    t_full, P_full = profile
    t_end_min = t_full[-1] / 60

    # --- transient: housings, then element mass, then the power that drives both
    fig, (a1, a2, a3) = plt.subplots(3, 1, figsize=(9, 11), sharex=True,
                                     gridspec_kw={"height_ratios": [3, 3, 1.4]})
    fig.patch.set_facecolor(_SURFACE)
    for ax in (a1, a2, a3):
        _style(ax)

    # Open top and the reducer run within a few degrees of each other the whole
    # way -- that overlap is the finding -- so they share one direct label and the
    # legend carries each line's exact peak.
    for name in HOUSINGS:
        t, Ts, _, _ = runs[(name, 2.0)]
        c = Ts - 273.15
        i = int(np.argmax(c))
        a1.plot(t / 60, c, color=_COLOUR[name], linewidth=2, solid_capstyle="round", zorder=3,
                label=f"{name}: peak {c[i]:.0f} °C")
        a1.plot(t[i] / 60, c[i], "o", color=_COLOUR[name], markersize=5,
                markeredgecolor=_SURFACE, markeredgewidth=2, zorder=4)
    def clear_of(name, x_min, half_width_min, above):
        """y that keeps a label off its line across the label's whole width."""
        t, Ts, _, _ = runs[(name, 2.0)]
        span = (t / 60 >= x_min - half_width_min) & (t / 60 <= x_min + half_width_min)
        c = Ts[span] - 273.15
        return c.max() + 8 if above else c.min() - 10

    a1.text(4.2, clear_of("Convection only", 4.2, 1.0, above=True), "Convection only (no radiation)",
            color=_INK, fontsize=9, ha="center", va="bottom")
    gap = float(np.max(np.abs(runs[("8 in reducer", 2.0)][1] - runs[("Open top", 2.0)][1])))
    a1.text(4.8, clear_of("Open top", 4.8, 2.0, above=False),
            f"Open top and pyramid reducer, never more than {gap:.0f} °C apart",
            color=_INK, fontsize=9, ha="center", va="top")
    leg = a1.legend(loc="upper left", bbox_to_anchor=(0.0, 0.86), frameon=False, fontsize=9,
                    labelcolor=_INK, handlelength=2.2)
    leg.set_zorder(5)
    _limit_lines(a1, 0.1)
    a1.set_ylim(0, 470)
    a1.set_ylabel("Hot-row surface (°C)", color=_INK2, fontsize=10)
    a1.set_title("Housing: how much radiation helps  (2 kg elements)", loc="left",
                 color=_INK, fontsize=11, fontweight="semibold")

    # Mass is ordered, so one hue with line style as the second channel; each line
    # is labelled where the three have spread apart, clear of the peaks.
    styles = {1.5: (0, (1, 2)), 2.0: "solid", 3.0: (0, (6, 3))}
    for m in ELEMENT_MASS_BAND:
        t, Ts, _, _ = runs[("Open top", m)]
        c = Ts - 273.15
        i = int(np.argmax(c))
        a2.plot(t / 60, c, color=_COLOUR["Open top"], linewidth=2,
                linestyle=styles[m], zorder=3, label=f"{m:g} kg element: peak {c[i]:.0f} °C")
        # Late in the run the curves flatten and sit 25-35 C apart, so each label
        # fits under its own line without touching the next one down.
        span = (t / 60 >= 5.3) & (t / 60 <= 5.9)
        a2.text(5.6, float(c[span].min()) - 3, f"{m:g} kg", color=_INK, fontsize=9,
                ha="center", va="top")
    a2.legend(loc="upper left", bbox_to_anchor=(0.0, 0.86), frameon=False, fontsize=9,
              labelcolor=_INK, handlelength=2.6)
    _limit_lines(a2, 0.1)
    a2.set_ylim(0, 470)
    a2.set_ylabel("Hot-row surface (°C)", color=_INK2, fontsize=10)
    a2.set_title("Element mass: the biggest unknown  (open top)", loc="left",
                 color=_INK, fontsize=11, fontweight="semibold")

    a3.plot(t_full / 60, P_full / 1e3, color=_INK2, linewidth=2)
    a3.axhline(8.0, color=_MUTED, linewidth=0.8, linestyle=(0, (5, 3)))
    a3.text(t_end_min + 0.1, 8.05, "8 kW bank rating", color=_INK2, fontsize=8.5, va="bottom")
    a3.set_ylim(0, 10.5)
    a3.set_ylabel("Bank power (kW)", color=_INK2, fontsize=10)
    a3.set_xlabel("Time from start of a full-output discharge (min)", color=_INK2, fontsize=10)
    a3.set_title(f"Full output at the 0.25 Ω floor: the module runs dry after {t_end_min:.1f} min",
                 loc="left", color=_INK, fontsize=11, fontweight="semibold")
    a3.set_xlim(0, runs[("Convection only", 2.0)][0][-1] / 60)

    fig.text(0.01, 0.005, f"Fan at its free-air {FAN_CFM:.0f} CFM -- the reducer cannot actually get that "
             f"(see Peak_vs_Airflow); air at {cfm.SITE_PRESSURE_RATIO:g} atm. Runs start at room temperature.",
             color=_MUTED, fontsize=8)
    fig.tight_layout(rect=(0, 0.015, 1, 1))
    os.makedirs(cfm.PLOTS_DIR, exist_ok=True)
    name1 = os.path.join(cfm.PLOTS_DIR, f"{prefix}_Transient_Surface_Temp.png")
    fig.savefig(name1, dpi=200, facecolor=_SURFACE)
    plt.close(fig)

    # --- steady state at 8 kW, by housing
    fig, ax = plt.subplots(figsize=(9, 3.6))
    fig.patch.set_facecolor(_SURFACE)
    _style(ax)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=_GRID, linewidth=0.6)
    names = list(HOUSINGS)
    vals = [steady_state(8000.0, None if HOUSINGS[n] is None else exchange_factors(HOUSINGS[n]))[0] - 273.15
            for n in names]
    ypos = np.arange(len(names))[::-1]
    ax.barh(ypos, vals, height=0.5, color=[_COLOUR[n] for n in names], zorder=3)
    lim_c = [T - 273.15 for T in LIMITS_K.values()]
    for y, v in zip(ypos, vals):
        x = v + 6
        for c in lim_c:                       # hop a value label over a limit line it would sit on
            if v < c and c - 4 < x < c + 40:
                x = c + 6
        ax.text(x, y, f"{v:.0f} °C", color=_INK, fontsize=9.5, va="center")
    ax.set_yticks(ypos, names)
    for lbl in ax.get_yticklabels():
        lbl.set_color(_INK)
    for name, T in LIMITS_K.items():
        ax.axvline(T - 273.15, color=_INK2, linewidth=1.0, linestyle=(0, (5, 3)), zorder=2)
        ax.text(T - 273.15 - 4, -0.62, _limit_label(name, T), color=_INK2, fontsize=8.5,
                ha="right", va="center")
    ax.set_ylim(-0.8, len(names) - 0.5)
    ax.set_xlim(0, 500)
    ax.set_xlabel("Hot-row surface at steady state (°C)", color=_INK2, fontsize=10)
    # Worded from the numbers, not typed in, so the title can't outlive a change of input.
    verdicts = []
    for lim_name, T in LIMITS_K.items():
        n_ok = sum(v <= T - 273.15 for v in vals)
        verdicts.append(f"{'all' if n_ok == len(vals) else 'none' if n_ok == 0 else f'{n_ok} of {len(vals)}'}"
                        f" under {T - 273.15:.0f} °C")
    ax.set_title(f"If 8 kW ran forever: {', '.join(verdicts)}", loc="left",
                 color=_INK, fontsize=11, fontweight="semibold")
    fig.tight_layout()
    name2 = os.path.join(cfm.PLOTS_DIR, f"{prefix}_Steady_State.png")
    fig.savefig(name2, dpi=200, facecolor=_SURFACE)
    plt.close(fig)
    if not sweep:
        return name1, name2

    # --- peak vs the flow the fan actually delivers, over the pressure it must beat
    fig, (b1, b2) = plt.subplots(2, 1, figsize=(9, 8.5), sharex=True,
                                 gridspec_kw={"height_ratios": [3, 2]})
    fig.patch.set_facecolor(_SURFACE)
    for ax in (b1, b2):
        _style(ax)
    # The choice this chart informs is open top vs reducer, so convection-only (the
    # radiation-off bound, in the tables and the transient chart) stays out: its
    # 2 kg line sits right on the open top's 1.5 kg line and would read as one.
    real = [n for n, e in HOUSINGS.items() if e is not None]
    for name in real:
        for m, style in ((2.0, "solid"), (1.5, (0, (1, 2)))):
            cfm_, pk = zip(*sorted(sweep[(name, m)]))
            b1.plot(cfm_, np.array(pk) - 273.15, color=_COLOUR[name], linewidth=2, linestyle=style,
                    marker="o" if m == 2.0 else None, markersize=4, markeredgecolor=_SURFACE,
                    markeredgewidth=1.5, zorder=3, label=f"{name}, {m:g} kg")
    x_lab = 2250.0
    hi = max(np.interp(x_lab, *zip(*sorted(sweep[(n, 1.5)]))) for n in real) - 273.15
    lo = min(np.interp(x_lab, *zip(*sorted(sweep[(n, 2.0)]))) for n in real) - 273.15
    b1.text(x_lab, hi + 6, "1.5 kg elements", color=_INK, fontsize=9, ha="center", va="bottom")
    b1.text(x_lab, lo - 6, "2 kg elements", color=_INK, fontsize=9, ha="center", va="top")
    _limit_lines(b1)
    b1.set_ylim(150, 470)
    b1.set_ylabel("Peak hot-row surface (°C)", color=_INK2, fontsize=10)
    b1.set_title("One full discharge: peak surface vs the airflow actually delivered", loc="left",
                 color=_INK, fontsize=11, fontweight="semibold")
    b1.legend(loc="upper right", frameon=False, fontsize=8.5, labelcolor=_INK, ncol=2,
              handlelength=2.4, columnspacing=1.2, bbox_to_anchor=(1.0, 0.86))

    # Where this fan will actually operate with each housing, across the bracketed
    # stall pressures: shaded above, and as the crossings below.
    for name in real:
        qs = [fan_operating_cfm(HOUSINGS[name], p0) for p0 in FAN_STALL_PA_BAND]
        b1.axvspan(min(qs), max(qs), color=_COLOUR[name], alpha=0.12, zorder=0, linewidth=0)
        b1.text((min(qs) + max(qs)) / 2, 158, f"{name}:\nthis fan delivers\n{min(qs):.0f}-{max(qs):.0f} CFM",
                color=_INK, fontsize=8.5, ha="center", va="bottom")

    cfm_axis = np.linspace(min(DELIVERED_CFM_GRID), max(DELIVERED_CFM_GRID), 120)
    for p0 in FAN_STALL_PA_BAND:
        b2.plot(cfm_axis, p0 * (1 - cfm_axis / FAN_CFM), color=_MUTED, linewidth=1.2, zorder=2)
    b2.text(cfm_axis[-1] * 0.62, FAN_STALL_PA_BAND[-1] * 0.52,
            f"fan, stalling at\n{FAN_STALL_PA_BAND[0] / 249.09:.2f}-{FAN_STALL_PA_BAND[-1] / 249.09:.2f} in. water",
            color=_INK2, fontsize=8.5, ha="center", va="bottom")
    for name in real:
        enc = HOUSINGS[name]
        dp = np.array([sum(system_pressure_Pa(enc, c)) for c in cfm_axis])
        b2.plot(cfm_axis, dp, color=_COLOUR[name], linewidth=2, zorder=3)
        for p0 in FAN_STALL_PA_BAND:
            q = fan_operating_cfm(enc, p0)
            b2.plot(q, p0 * (1 - q / FAN_CFM), "o", color=_COLOUR[name], markersize=5,
                    markeredgecolor=_SURFACE, markeredgewidth=1.5, zorder=4)
        dp_full = sum(system_pressure_Pa(enc, FAN_CFM))
        label = f"{name}: needs {dp_full:.0f} Pa at {FAN_CFM:.0f} CFM"
        if dp[-1] > 200:                 # runs off the top: label beside the steep part
            x_at = float(np.interp(150.0, dp, cfm_axis))
            b2.text(x_at + 60, 150, label, color=_INK, fontsize=9, ha="left", va="center")
        else:                            # stays low: label well above its end, clear of the fan lines
            b2.text(cfm_axis[-1] - 30, dp[-1] + 22, label, color=_INK, fontsize=9, ha="right", va="bottom")
    b2.set_ylabel("Static pressure (Pa)", color=_INK2, fontsize=10)
    b2.set_xlabel("Airflow actually delivered (CFM)", color=_INK2, fontsize=10)
    b2.set_title("Fan vs housing: the flow settles where the curves cross", loc="left",
                 color=_INK, fontsize=11, fontweight="semibold")
    b2.set_xlim(min(DELIVERED_CFM_GRID), max(DELIVERED_CFM_GRID))
    b2.set_ylim(0, 200)
    fig.text(0.01, 0.005, f"Temperatures at {cfm.SITE_PRESSURE_RATIO:g} atm (altitude). Pressures in "
             "standard air, as fan curves are published; the reducer's vent duct adds more.\n"
             "The fan publishes no pressure curve, so it is bracketed: straight lines from 3500 CFM "
             "free-air to a stall pressure in the band shown.", color=_MUTED, fontsize=8)
    fig.tight_layout(rect=(0, 0.035, 1, 1))
    name3 = os.path.join(cfm.PLOTS_DIR, f"{prefix}_Peak_vs_Airflow.png")
    fig.savefig(name3, dpi=200, facecolor=_SURFACE)
    plt.close(fig)
    return name1, name2, name3


if __name__ == "__main__":
    results = main()
    for f in plot_results(*results):
        print(f"Saved: {os.path.abspath(f)}")
