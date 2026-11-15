"""
mission_planner.py - the spec-compliant path planner (Sections 8, 9, 10, 13 of the
Mars Surface Navigation System design specification).

    MissionPlanner(terrain, ...).plan(start_enu, goal_enu, mode="walk" | "rover")

What it implements (spec section in brackets)
  * hierarchical search: coarse A* on the native ~20 m grid, then corridor refinement on a
    finer grid                                                                 [9.4]
  * uncertainty-aware: sigma is propagated along every candidate path and hazard buffers are
    n_sigma * predicted sigma; paths that cannot keep sigma small near a hazard are rejected
    or get a fix stop before it                                                [9.2]
  * localization-aware routing: per-cell localizability penalty, optimised fix stops whose
    time is counted in the ETA                                                 [9.3, 8.2]
  * hard constraints: slope / cliff / soil limits, dose budget, shelter reachability on
    EVERY consumable with margin, weather and SEP time windows                 [9.1, SYS-9]
  * walking and rover modes with different cost layers and limits             [9.4]
  * outputs in Site ENU: waypoints, ETA, energy, risk, turn-back point, fix stops, predicted
    sigma profile, segment difficulty, alerts                                  [9.4, 13]
  * an independent verifier that re-checks the finished route                 [14, 15.3]
Replanning with D* Lite lives in replan.py.
"""
import math
import time as _time
from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage

from frames import SiteFrame
from environment import Weather, SEPAlert, Consumables, dose_msv
from localization import (LocParams, LocalizabilityLayer, LocalizationState, confidence_level,
                          sigma_move, best_fix, fuse, METHODS, CONF_BOUNDS)
import kernels as K
from kernels import MOVES, WALK, ROVER

# cost weights: (time, energy s/kJ, risk s per risk-metre, localization s per low-loc metre)
PROFILES_MISSION = {
    "min_eta":  (1.0, 0.0, 0.25, 0.15),   # spec 9.1: minimise ETA; risk / localization are tie-breakers
    "balanced": (1.0, 5.0, 1.0, 0.5),
    "safest":   (1.0, 5.0, 4.0, 1.0),
}


@dataclass
class WalkAgent:
    body_mass_kg: float = 80.0
    suit_mass_kg: float = 60.0
    suit_speed_factor: float = 0.55
    max_slope_deg: float = 25.0        # spec 8.1: ~20-25 deg walking (est.)
    soft_slope_deg: float = 15.0
    rough_max: float = None            # no extra roughness / sand ban on foot
    sand_max: float = None
    name: str = "walk"

    def params(self):
        P = np.zeros(11)
        P[K.P_MASS] = self.body_mass_kg + self.suit_mass_kg
        P[K.P_C2] = self.suit_speed_factor
        P[K.P_SOFT], P[K.P_MAXS] = self.soft_slope_deg, self.max_slope_deg
        P[K.P_SIDE], P[K.P_DOWN0], P[K.P_DOWN1], P[K.P_ROUGH] = 12.0, 8.0, 7.0, 1.5
        P[K.P_VFLAT], P[K.P_ETA] = 1.0, 1.0
        return P

    mode_id = WALK


@dataclass
class RoverAgent:
    mass_kg: float = 1800.0            # (est.) pressurised rover + crew
    v_flat_ms: float = 2.2             # ~8 km/h on flat ground (est.)
    crr: float = 0.10                  # rolling resistance on regolith (est.)
    drivetrain_eff: float = 0.70
    hotel_w: float = 300.0
    max_slope_deg: float = 15.0        # spec 8.1: ~15-20 deg rover (est.)
    soft_slope_deg: float = 10.0
    rough_max: float = 2.0             # ban blocky ground (rock abundance proxy)
    sand_max: float = 0.6              # ban sand ripples / dunes
    name: str = "rover"

    def params(self):
        P = np.zeros(11)
        P[K.P_VFLAT], P[K.P_MASS], P[K.P_C2], P[K.P_ETA] = self.v_flat_ms, self.mass_kg, self.crr, self.drivetrain_eff
        P[K.P_SOFT], P[K.P_MAXS], P[K.P_HOTEL] = self.soft_slope_deg, self.max_slope_deg, self.hotel_w
        P[K.P_SIDE], P[K.P_DOWN0], P[K.P_DOWN1], P[K.P_ROUGH] = 10.0, 10.0, 6.0, 1.5
        return P

    mode_id = ROVER


def _hmin(mode, P, W):
    """Smallest possible cost per metre (time + energy only; risk terms are >= 0), so A*
    stays admissible. Weather multipliers are >= 1 so they cannot break it."""
    gs = np.linspace(-0.47, 0.47, 1001)
    dts, des = [], []
    for g in gs:
        dt, dE = K.edge_phys(mode, 1.0, g, 0.0, 0.0, 0.0, 0.0, 0.0, P)
        dts.append(dt)
        des.append(dE)
    # time and energy minima can occur at different gradients: summing the two minima is a
    # (looser) lower bound on the minimum of the sum
    return float(W[0] * min(dts) + W[1] * min(des) / 1000.0) * 0.999


@dataclass
class MissionPlan:
    mode: str
    profile: str
    rows: np.ndarray                 # dense route, global 20 m-grid coordinates
    cols: np.ndarray
    enu: np.ndarray                  # (N, 3) Site ENU
    sigma: np.ndarray                # predicted 1-sigma along the route (m)
    t_s: np.ndarray                  # elapsed time at each point (s, includes stops)
    e_j: np.ndarray                  # cumulative energy (J)
    dist_m: np.ndarray
    stops: list                      # [{idx, method, duration_s}]
    return_margin: np.ndarray        # fraction of the tightest resource left after reserving the return
    eta_min: float = 0.0
    energy_kj: float = 0.0
    risk_score: float = 0.0
    turn_back_idx: int = 0
    waypoint_idx: list = field(default_factory=list)
    segment_difficulty: list = field(default_factory=list)
    alerts: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    verdict: str = ""
    dose_msv: float = 0.0
    stats: dict = field(default_factory=dict)
    consumables: dict = field(default_factory=dict)
    sep: dict = field(default_factory=dict)
    confidence_timeline: list = field(default_factory=list)

    # ---- spec 13: Planner -> HUD
    def hud_json(self):
        wi = self.waypoint_idx
        stop_by_idx = {s["idx"]: s for s in self.stops}
        # fix stops refer to waypoint numbers
        fix = []
        for s in self.stops:
            k = int(np.argmin([abs(i - s["idx"]) for i in wi]))
            fix.append({"at_waypoint": k, "method": s["method"], "duration_s": int(round(s["duration_s"]))})
        tb = self.turn_back_idx
        return {
            "waypoints_enu_m": [[round(float(v), 2) for v in self.enu[i]] for i in wi],
            "eta_min": round(self.eta_min, 1),
            "turn_back_point_enu_m": [round(float(self.enu[tb][0]), 2), round(float(self.enu[tb][1]), 2)],
            "fix_stops": fix,
            "predicted_sigma_m": [round(float(self.sigma[i]), 2) for i in wi],
            "segment_difficulty": self.segment_difficulty,
            "alerts": self.alerts,
        }

    def to_json(self):
        d = self.hud_json()
        d.update({
            "mode": self.mode, "profile": self.profile, "verdict": self.verdict,
            "distance_km": round(float(self.dist_m[-1]) / 1000, 3),
            "energy_kJ": round(self.energy_kj, 1),
            "risk_score": round(self.risk_score, 3),
            "dose_mSv": round(self.dose_msv, 4),
            "confidence_at_goal": confidence_level(float(self.sigma[-1])),
            "return_checked": getattr(self, "return_checked", True),
            "min_return_margin_frac": round(float(self.return_margin.min()), 3) if getattr(self, "return_checked", True) else None,
            "consumables_at_goal": self.consumables,
            "confidence_timeline": self.confidence_timeline,
            "violations": self.violations,
            "sep": self.sep,
            "search_stats": self.stats,
        })
        return d


REC_CAP = 120000       # states kept for the 'replay the search' view


class MissionPlanner:
    def __init__(self, terrain, frame=None, loc_layer=None, loc_params=None, weather=None,
                 sep=None, consumables=None, agents=None, shelters_enu=((0.0, 0.0),),
                 refine_factor=4, corridor_m=120.0, chunk_m=1000.0, margin_m=2500.0,
                 max_window_cells=2_500_000, verbose=False):
        self.t = terrain
        self.frame = (frame or SiteFrame()).anchor_to_terrain(terrain)
        self.lp = loc_params or LocParams()
        self.loc = loc_layer if loc_layer is not None else LocalizabilityLayer(terrain, verbose=verbose)
        self.weather = weather or Weather()
        self.sep = sep or SEPAlert()
        self.cons = consumables or Consumables()
        self.agents = agents or {"walk": WalkAgent(), "rover": RoverAgent()}
        self.shelters_enu = [tuple(s) for s in shelters_enu]
        self.refine_factor, self.corridor_m = int(refine_factor), corridor_m
        self.chunk_m, self.margin_m = chunk_m, margin_m
        self.max_window_cells = int(max_window_cells)   # search memory is ~40 bytes x 10 sigma levels per cell
        self.verbose = verbose
        self._dyn_hazards = []          # [((x, y) ENU, radius_m)] new hazards seen by sensors
        self.LV = self.lp.levels()
        self.LP = self.lp.as_array()

    def warmup(self):
        """Compile every numba kernel now (A*, return field, D* Lite, refinement) so first-call JIT
        time (several seconds) never lands inside a mission. Kernels are cached on disk, so
        after the first run on a machine this takes well under a second."""
        for mode in self.agents:
            try:
                self.plan((0.0, 0.0), (120.0, 80.0), mode=mode, refine=True)
            except Exception:           # noqa: BLE001 - warm-up must never raise
                pass
        try:
            from replan import DStarReplanner
            rp = DStarReplanner(self, (0.0, 0.0), (120.0, 80.0), "walk")
            rp.compute()
            rp.block_hazard((60.0, 40.0), 20.0)
            rp.compute()
        except Exception:               # noqa: BLE001
            pass
        self._dyn_hazards = []
        return self

    # ------------------------------------------------------------ search window
    def _bounds(self, pts, m):
        H, W = self.t.shape
        return (max(0, min(p[0] for p in pts) - m), min(H, max(p[0] for p in pts) + m + 1),
                max(0, min(p[1] for p in pts) - m), min(W, max(p[1] for p in pts) + m + 1))

    def fit_window(self, pts, m_wanted):
        """Search window around `pts`, with as much margin as fits in max_window_cells.
        On the real 22-million-cell DEM an unbounded window needs gigabytes and minutes."""
        m = int(m_wanted)
        while True:
            r0, r1, c0, c1 = self._bounds(pts, m)
            if (r1 - r0) * (c1 - c0) <= self.max_window_cells:
                return (r0, r1, c0, c1)
            if m == 0:
                side = math.sqrt(self.max_window_cells) * self.t.res / 1000.0
                raise ValueError(
                    f"The start, goal and shelter are spread over {(r1 - r0) * self.t.res / 1000:.0f} x "
                    f"{(c1 - c0) * self.t.res / 1000:.0f} km, more than the planner can search at once "
                    f"(about {side:.0f} km square). Choose points closer together and closer to the lander.")
            m = int(m * 0.7) if m > 8 else 0

    def _precheck_return(self, ctx, rc_start, rc_goal, CONS, t0, e0, on_site, deadline, mode):
        """Cheap feasibility test from the return-to-shelter field BEFORE the heavy search: if the crew
        cannot get back from the start (or from the goal) even with zero outbound cost, say so now."""
        cons = self.cons
        for name, (r, c), goal in (("start", rc_start, False), ("goal", rc_goal, True)):
            rT, rE = float(ctx["retT"][r, c]), float(ctx["retE"][r, c])
            if K.res_ok(t0, e0, rT, rE, goal, CONS, cons.return_margin, cons.reserve_frac, float(on_site), float(deadline)):
                continue
            how = "on foot" if mode == "walk" else "by rover"
            if rT >= 1e11:
                why = "no safe route back to the shelter exists from there"
            else:
                why = (f"the trip back to the shelter alone would take about {rT / 60:.0f} min {how}, which already "
                       f"exceeds the consumables, safety margin or time window available")
            raise RuntimeError(f"The {name} point is too far from the shelter for this EVA: {why}. "
                               f"Pick a {name} closer to the lander{' or switch to the rover' if mode == 'walk' else ''}.")

    # ------------------------------------------------------------ helpers
    def to_rc(self, p):
        """Accept ENU metres (x, y), {'rc': (r, c)} or {'lonlat': (lon, lat)}."""
        if isinstance(p, dict):
            if "rc" in p:
                return int(p["rc"][0]), int(p["rc"][1])
            if "lonlat" in p:
                return self.t.lonlat_to_rc(*p["lonlat"])
            p = p["enu"]
        return self.frame.enu_to_rc(self.t, float(p[0]), float(p[1]))

    @staticmethod
    def _snap(nogo, r, c, radius=25):
        if not nogo[r, c]:
            return r, c
        r0, r1 = max(0, r - radius), min(nogo.shape[0], r + radius + 1)
        c0, c1 = max(0, c - radius), min(nogo.shape[1], c + radius + 1)
        sub = ~nogo[r0:r1, c0:c1]
        if not sub.any():
            raise ValueError("Point is inside a large no-go area (cliff / bad data).")
        rr, cc = np.nonzero(sub)
        k = np.argmin((rr + r0 - r) ** 2 + (cc + c0 - c) ** 2)
        return int(rr[k] + r0), int(cc[k] + c0)

    def _dyn_mask(self, row_coords, col_coords):
        """Mask of newly observed hazards on any window (coords are 20 m-grid coordinates)."""
        m = np.zeros((len(row_coords), len(col_coords)), bool)
        for enu, rad in self._dyn_hazards:
            r, c = self.to_rc(enu)
            m |= ((np.asarray(row_coords)[:, None] - r) ** 2 + (np.asarray(col_coords)[None, :] - c) ** 2
                  <= (rad / self.t.res) ** 2)
        return m

    def clear_dynamic_hazards(self):
        self._dyn_hazards = []

    def _weights(self, profile):
        return np.array(PROFILES_MISSION[profile], dtype=np.float64)

    def _deadline_s(self):
        d = self.weather.window_end_min * 60.0
        if self.sep.active:
            d = min(d, (self.sep.onset_min - self.sep.return_margin_min) * 60.0)
        return d

    def _worst_mult(self):
        WB, _ = self.weather.sampled()
        return float(WB[0].max()), float(WB[1].max())

    # ------------------------------------------------------------ main entry
    def plan(self, start, goal, mode="walk", profile="min_eta", loc_state=None, refine=True,
             t_elapsed_s=0.0, e_used_j=0.0, enforce_return=True, on_site_s=None):
        t_wall = _time.time()
        agent = self.agents[mode]
        mid = agent.mode_id          # numba kernels take the INTEGER mode id (a string silently picks the wrong branch)
        P = agent.params()
        W = self._weights(profile)
        nogo, hazd = self.t.mode_layers(mode, agent.max_slope_deg, agent.rough_max, agent.sand_max)
        sig0 = loc_state.sigma_m() if loc_state is not None else self.lp.sigma0_m
        cons = self.cons
        CONS = cons.coeffs(mode)
        on_site = cons.on_site_s if on_site_s is None else on_site_s
        WB, bin_s = self.weather.sampled()
        deadline = self._deadline_s()
        dm_w, em_w = self._worst_mult()

        for name, p_ in (("start", start), ("goal", goal)):
            r_, c_ = self.to_rc(p_)
            if not self.t.in_bounds(r_, c_):
                raise ValueError(f"The {name} point is outside the terrain model "
                                 f"(it covers about +/-{self.t.res * min(self.t.shape) / 2000:.0f} km around the lander).")
        sr, sc = self._snap(nogo, *self.to_rc(start))
        gr, gc = self._snap(nogo, *self.to_rc(goal))
        for name, (r, c) in (("start", (sr, sc)), ("goal", (gr, gc))):
            if not self.t.in_bounds(r, c):
                raise ValueError(f"{name} is outside the terrain")
        shel = [self._snap(nogo, *self.to_rc(s)) for s in self.shelters_enu] if enforce_return else []
        pts = [(sr, sc), (gr, gc)] + shel      # without the return rule the lander does not constrain the window
        span = self.t.res * math.hypot(gr - sr, gc - sc)
        hmin = _hmin(mid, P, W)

        tried, ok, last = set(), False, None
        for grow in (1.0, 2.5, None):
            if grow is None:
                if self.t.shape[0] * self.t.shape[1] > self.max_window_cells:
                    break                                   # never search the whole real DEM
                bnds = (0, self.t.shape[0], 0, self.t.shape[1])
            else:
                bnds = self.fit_window(pts, int((self.margin_m + 0.5 * span * grow) / self.t.res))
            if bnds in tried:
                continue                                    # the window cannot grow any further
            tried.add(bnds)
            r0, r1, c0, c1 = bnds
            ctx = self._window_ctx(r0, r1, c0, c1, nogo, hazd, shel, mid, P, dm_w, em_w)
            if enforce_return and len(tried) == 1:
                self._precheck_return(ctx, (sr - r0, sc - c0), (gr - r0, gc - c0), CONS, t_elapsed_s, e_used_j,
                                      on_site, deadline, mode)
            ok, ids, meths, sigs, Ts, Es, stats, rec, rec_step = K.search_aug(
                ctx["elev"], ctx["slope"], ctx["rough"], ctx["sand"], ctx["hazd"], ctx["nogo"],
                ctx["locS"], ctx["locO"], ctx["open"], ctx["retT"], ctx["retE"],
                float(self.t.res), sr - r0, sc - c0, gr - r0, gc - c0, float(sig0),
                float(t_elapsed_s), float(e_used_j), mid, P, W, self.LV, self.LP, WB, bin_s,
                CONS, cons.return_margin, cons.reserve_frac, float(on_site), float(deadline),
                bool(enforce_return), hmin, MOVES, REC_CAP)
            last = (stats, ctx, sr - r0, sc - c0)
            if ok:
                break
        if not ok:
            raise RuntimeError(self._explain_failure(*last))
        K_ = len(self.LV)
        Wd = c1 - c0
        cell = ids // K_
        rows = (cell // Wd + r0).astype(np.float64)
        cols = (cell % Wd + c0).astype(np.float64)
        coarse_stats = {"expanded_states": int(stats[0]), "pruned_hazard_buffer": int(stats[1]),
                        "pruned_return_margin": int(stats[2]), "pruned_lost": int(stats[3]),
                        "fix_stops_considered": int(stats[4]), "window_cells": int((r1 - r0) * (c1 - c0)),
                        "coarse_s": round(_time.time() - t_wall, 2)}
        route = dict(rows=rows, cols=cols, meth=meths, sig=sigs, T=Ts, E=Es)
        search_eta_s = float(Ts[-1] - t_elapsed_s)
        refined = False
        if refine and self.refine_factor > 1:
            fr = self._refine(route, ctx, (r0, c0), mode, agent, P, W, WB, bin_s, CONS, on_site,
                              deadline, enforce_return, hmin, nogo, hazd)
            if fr is not None:
                route, refined = fr, True
        plan = self._finalise(route, ctx, (r0, c0), mode, agent, P, profile, WB, bin_s, CONS,
                              on_site, deadline, sig0, t_elapsed_s, e_used_j, enforce_return)
        refine_note = None
        if refined and plan.violations:
            # the refined route must pass the same independent verifier as the coarse one;
            # if it does not, keep the coarse route (whose constraints the search guaranteed)
            alt = self._finalise(dict(rows=rows, cols=cols, meth=meths, sig=sigs, T=Ts, E=Es), ctx, (r0, c0),
                                 mode, agent, P, profile, WB, bin_s, CONS, on_site, deadline, sig0,
                                 t_elapsed_s, e_used_j, enforce_return)
            if len(alt.violations) < len(plan.violations):
                plan, refined = alt, False
                refine_note = "refined route failed verification; coarse route kept"
        if refine_note:
            plan.alerts.append({"type": "refinement", "msg": refine_note})
        coarse_stats["search_vs_verifier_time_diff_pct"] = None
        plan.stats = dict(coarse_stats, engine="A* over (cell, sigma bucket)", refined=refined, planning_s=round(_time.time() - t_wall, 2),
                          hierarchy="coarse A* on %g m grid%s" % (
                              self.t.res, " + corridor refinement on %g m grid" % (self.t.res / self.refine_factor)
                              if refined else ""))
        if not refined:
            plan.stats["search_vs_verifier_time_diff_pct"] = round(
                100.0 * abs(search_eta_s - (plan.t_s[-1] - t_elapsed_s)) / max(plan.t_s[-1] - t_elapsed_s, 1.0), 2)
        plan.stats["nogo_pct_in_window"] = round(100.0 * float(ctx["nogo"].mean()), 1)
        plan.stats["fine_chunks"] = int(getattr(self, "_last_chunks", 0)) if refined else 0
        plan.stats["sigma_levels"] = len(self.LV)
        plan.stats["coarse_route_points"] = int(len(rows))
        # what the search explored, for the 'replay the search' view (bounded; see kernels.search_aug)
        plan._search = {"rec": rec, "step": int(rec_step), "K": len(self.LV), "origin": (r0, c0), "wshape": (r1 - r0, c1 - c0),
                        "expanded": int(stats[0]), "coarse_route": (rows.copy(), cols.copy())}
        return plan

    # ------------------------------------------------------------ window of layers
    def _window_ctx(self, r0, r1, c0, c1, nogo, hazd, shel, mode, P, dm_w, em_w):
        t = self.t
        sl = (slice(r0, r1), slice(c0, c1))
        ctx = {k: np.ascontiguousarray(getattr(t, k)[sl]) for k in ("elev", "slope", "rough")}
        ctx["sand"] = np.ascontiguousarray(t.sand[sl])
        ctx["nogo"] = np.ascontiguousarray(nogo[sl])
        ctx["hazd"] = np.ascontiguousarray(hazd[sl])
        if self._dyn_hazards:
            dyn = self._dyn_mask(np.arange(r0, r1), np.arange(c0, c1))
            for sr_, sc_ in shel:
                if r0 <= sr_ < r1 and c0 <= sc_ < c1:
                    dyn[sr_ - r0, sc_ - c0] = False
            ctx["nogo"] = np.ascontiguousarray(ctx["nogo"] | dyn)
            ctx["hazd"] = (ndimage.distance_transform_edt(~ctx["nogo"]) * t.res).astype(np.float32)
        S, O, OP = self.loc.window(np.arange(r0, r1), np.arange(c0, c1))
        ctx["locS"], ctx["locO"], ctx["open"] = S, O, OP
        shel_idx = np.array([(r - r0) * (c1 - c0) + (c - c0) for r, c in shel
                             if r0 <= r < r1 and c0 <= c < c1], dtype=np.int64)
        req = self.lp.n_sigma * self.lp.sigma_levels[0] + self.lp.base_buffer_m
        if len(shel_idx) == 0:
            ctx["retT"] = np.full(ctx["elev"].shape, 1e12)
            ctx["retE"] = np.full(ctx["elev"].shape, 1e12)
        else:
            T_, E_ = K.return_field(ctx["elev"], ctx["slope"], ctx["rough"], ctx["sand"], ctx["hazd"],
                                    ctx["nogo"], float(t.res), shel_idx, mode, P, dm_w, em_w, req, MOVES)
            ctx["retT"], ctx["retE"] = T_.reshape(ctx["elev"].shape), E_.reshape(ctx["elev"].shape)
        ctx["origin"] = (r0, c0)
        return ctx

    def _explain_failure(self, stats, ctx, sr, sc):
        why = []
        if stats[1] > 0:
            why.append(f"{int(stats[1])} moves rejected by the uncertainty-inflated hazard buffer")
        if stats[2] > 0:
            why.append("the round trip to this goal and back would exceed the consumables, safety margin or time window "
                       f"({int(stats[2])} candidate moves rejected because the crew could no longer get back to the shelter with margin)")
        if stats[3] > 0:
            why.append(f"{int(stats[3])} moves rejected because position confidence would be lost")
        if ctx["retT"][sr, sc] >= 1e11:
            why.append("the start cell cannot reach any shelter under the current limits")
        return ("No feasible route under the hard constraints: " + ("; ".join(why) or "terrain is disconnected")
                + ". Try a closer goal, a better position fix at the start, or (on foot) the rover.")

    # ------------------------------------------------------------ corridor refinement (spec 9.4)
    def _refine(self, route, ctx, origin, mode, agent, P, W, WB, bin_s, CONS, on_site, deadline,
                enforce, hmin, nogo, hazd):
        res = self.t.res
        rows, cols, meth = route["rows"], route["cols"], route["meth"]
        n = len(rows)
        if n < 3:
            return None
        cum = np.concatenate([[0], np.cumsum(np.hypot(np.diff(rows), np.diff(cols)) * res)])
        bnd = [0]
        for i in range(1, n - 1):
            if cum[i] - cum[bnd[-1]] >= self.chunk_m and meth[i] == 0 and meth[i + 1] == 0:
                bnd.append(i)
        bnd.append(n - 1)
        self._last_chunks = len(bnd) - 1
        out = {k: [] for k in ("rows", "cols", "meth", "sig", "T", "E")}
        state = (float(route["sig"][0]), float(route["T"][0]), float(route["E"][0]))
        for a, b in zip(bnd[:-1], bnd[1:]):
            res_c = self._fine_chunk(rows[a:b + 1], cols[a:b + 1], state, ctx, mode, agent, P, W, WB,
                                     bin_s, CONS, on_site, deadline, enforce, hmin)
            if res_c is None:
                return None
            first = 0 if not out["rows"] else 1       # the chunk boundary point is shared
            for k in out:
                out[k].append(res_c[k][first:])
            state = (float(res_c["sig"][-1]), float(res_c["T"][-1]), float(res_c["E"][-1]))
        return {k: np.concatenate(v) for k, v in out.items()}

    def _fine_chunk(self, pr, pc, state, ctx, mode, agent, P, W, WB, bin_s, CONS, on_site,
                    deadline, enforce, hmin):
        t, f = self.t, self.refine_factor
        cor = int(math.ceil(self.corridor_m / t.res)) + 2
        H, Wd = t.shape
        r0, r1 = max(0, int(math.floor(pr.min())) - cor), min(H, int(math.ceil(pr.max())) + cor + 1)
        c0, c1 = max(0, int(math.floor(pc.min())) - cor), min(Wd, int(math.ceil(pc.max())) + cor + 1)
        ft = t.upsampled(r0, r1, c0, c1, f)
        nogo, hazd = ft.mode_layers(mode, agent.max_slope_deg, agent.rough_max, agent.sand_max)
        rows_c = r0 + (np.arange(ft.shape[0]) + 0.5) / f - 0.5
        cols_c = c0 + (np.arange(ft.shape[1]) + 0.5) / f - 0.5
        if self._dyn_hazards:
            nogo = nogo | self._dyn_mask(rows_c, cols_c)
            hazd = (ndimage.distance_transform_edt(~nogo) * ft.res).astype(np.float32)
        fr = (pr - r0 + 0.5) * f - 0.5
        fc = (pc - c0 + 0.5) * f - 0.5
        mark = np.zeros(ft.shape, bool)
        for i in range(len(fr) - 1):
            k = max(2, int(math.ceil(math.hypot(fr[i + 1] - fr[i], fc[i + 1] - fc[i]) * 2)))
            q = np.linspace(0, 1, k)
            ri = np.clip(np.rint(fr[i] + q * (fr[i + 1] - fr[i])).astype(int), 0, ft.shape[0] - 1)
            ci = np.clip(np.rint(fc[i] + q * (fc[i + 1] - fc[i])).astype(int), 0, ft.shape[1] - 1)
            mark[ri, ci] = True
        corr = ndimage.distance_transform_edt(~mark) * ft.res <= self.corridor_m
        nogo_s = np.ascontiguousarray(nogo | ~corr)
        try:
            s = self._snap(nogo_s, int(round(fr[0])), int(round(fc[0])), radius=6)
            g = self._snap(nogo_s, int(round(fr[-1])), int(round(fc[-1])), radius=6)
        except ValueError:
            return None
        S, O, OP = self.loc.window(rows_c, cols_c)
        cr0, cc0 = ctx["origin"]
        cg = np.meshgrid(rows_c - cr0, cols_c - cc0, indexing="ij")
        retT = ndimage.map_coordinates(ctx["retT"], cg, order=1, mode="nearest")
        retE = ndimage.map_coordinates(ctx["retE"], cg, order=1, mode="nearest")
        bad = ndimage.map_coordinates(ctx["retT"], cg, order=0, mode="nearest") >= 1e11
        retT[bad] = 1e12
        retE[bad] = 1e12
        sig0, T0, E0 = state
        ok, ids, meths, sigs, Ts, Es, stats, rec, rec_step = K.search_aug(
            np.ascontiguousarray(ft.elev), np.ascontiguousarray(ft.slope), np.ascontiguousarray(ft.rough),
            np.ascontiguousarray(ft._sand), np.ascontiguousarray(hazd), nogo_s, S, O, OP, retT, retE,
            float(ft.res), s[0], s[1], g[0], g[1], sig0, T0, E0, agent.mode_id, P, W, self.LV, self.LP,
            WB, bin_s, CONS, self.cons.return_margin, self.cons.reserve_frac, float(on_site),
            float(deadline), bool(enforce), hmin, MOVES, 0)
        if not ok:
            return None
        cell = ids // len(self.LV)
        Wf = ft.shape[1]
        rf, cf = cell // Wf, cell % Wf
        return {"rows": r0 + (rf + 0.5) / f - 0.5, "cols": c0 + (cf + 0.5) / f - 0.5,
                "meth": meths, "sig": sigs, "T": Ts, "E": Es}

    # ------------------------------------------------------------ verification + assembly
    def _resample(self, rows, cols, stopflag, step_m):
        res = self.t.res
        orr, occ, osf = [rows[0]], [cols[0]], [stopflag[0]]
        for i in range(1, len(rows)):
            d = math.hypot(rows[i] - rows[i - 1], cols[i] - cols[i - 1]) * res
            k = max(1, int(math.ceil(d / step_m)))
            for j in range(1, k + 1):
                fr = j / k
                orr.append(rows[i - 1] + fr * (rows[i] - rows[i - 1]))
                occ.append(cols[i - 1] + fr * (cols[i] - cols[i - 1]))
                osf.append(bool(stopflag[i]) if j == k else False)
        return np.array(orr), np.array(occ), np.array(osf), 

    def verify_route(self, rows, cols, stopflag, mode, ctx=None, sig0=None, t0=0.0, e0=0.0,
                     enforce_return=True, on_site_s=None, step_m=10.0, stop_method=None):
        """INDEPENDENT re-check of a finished route (spec 14 'planner output checked
        independently', spec 15.3 'hazard violations = 0'). It does not reuse the search's
        bookkeeping: it resamples the route to <= step_m, re-propagates sigma / time / energy
        with the same physics kernels, and tests every constraint against a sub-cell hazard
        map. Returns a dict of per-sample arrays and a list of violations."""
        t, agent = self.t, self.agents[mode]
        P = agent.params()
        res = t.res
        sig0 = self.lp.sigma0_m if sig0 is None else sig0
        on_site = self.cons.on_site_s if on_site_s is None else on_site_s
        WB, bin_s = self.weather.sampled()
        nb = WB.shape[1]
        rr, cc, sf = self._resample(rows, cols, stopflag, step_m)[:3]
        n = len(rr)
        ri = np.clip(np.rint(rr).astype(int), 0, t.shape[0] - 1)
        ci = np.clip(np.rint(cc).astype(int), 0, t.shape[1] - 1)
        z = ndimage.map_coordinates(t.elev, [rr, cc], order=1, mode="nearest")
        slope_c, rough_c, sand_c = t.slope[ri, ci], t.rough[ri, ci], t.sand[ri, ci]
        S, O, OP = self.loc.sample(rr, cc)
        enu = np.column_stack(self.frame.rc_to_enu(t, rr, cc) + (self.frame.up(z),))
        # sub-cell hazard map (4x finer than the 20 m grid): distance to the nearest no-go
        # cell BOUNDARY, in metres. Independent of the grid the search used.
        nogo, _ = t.mode_layers(mode, agent.max_slope_deg, agent.rough_max, agent.sand_max)
        pad = 12
        r0, r1 = max(0, ri.min() - pad), min(t.shape[0], ri.max() + pad + 1)
        c0, c1 = max(0, ci.min() - pad), min(t.shape[1], ci.max() + pad + 1)
        f = 4
        ng_f = np.kron(nogo[r0:r1, c0:c1].astype(np.uint8), np.ones((f, f), np.uint8)).astype(bool)
        if self._dyn_hazards:
            ng_f |= self._dyn_mask(r0 + (np.arange(ng_f.shape[0]) + 0.5) / f - 0.5,
                                   c0 + (np.arange(ng_f.shape[1]) + 0.5) / f - 0.5)
        edt = ndimage.distance_transform_edt(~ng_f) * (res / f)
        fr_i = np.clip(((rr - r0 + 0.5) * f - 0.5).round().astype(int), 0, ng_f.shape[0] - 1)
        fc_i = np.clip(((cc - c0 + 0.5) * f - 0.5).round().astype(int), 0, ng_f.shape[1] - 1)
        clear = edt[fr_i, fc_i] - 0.5 * res / f
        in_nogo = ng_f[fr_i, fc_i]

        sig = np.empty(n); T = np.empty(n); E = np.empty(n); dist = np.zeros(n)
        sig[0], T[0], E[0] = sig0, t0, e0
        risk_acc, risk_len = 0.0, 0.0
        stops = []
        for i in range(1, n):
            bi = min(int(T[i - 1] / bin_s), nb - 1)
            dm, em, cam, cel = WB[0, bi], WB[1, bi], WB[2, bi], WB[3, bi]
            d = math.hypot(rr[i] - rr[i - 1], cc[i] - cc[i - 1]) * res
            if d < 1e-9:                                   # a stop point
                if sf[i]:
                    sm, meth = best_fix(S[i], O[i], OP[i], cam, cel, self.LP)
                    sig[i], T[i], E[i] = sig[i - 1], T[i - 1], E[i - 1]
                    if meth > 0:
                        sig[i] = fuse(sig[i - 1], sm)
                        T[i] = T[i - 1] + self.lp.fix_stop_s
                        E[i] = E[i - 1] + self.lp.stand_power_w * self.lp.fix_stop_s
                        stops.append({"idx": i, "method": METHODS[meth], "duration_s": self.lp.fix_stop_s})
                else:
                    sig[i], T[i], E[i] = sig[i - 1], T[i - 1], E[i - 1]
                dist[i] = dist[i - 1]
                continue
            dz = z[i] - z[i - 1]
            dt, dE = K.edge_phys(agent.mode_id, d, dz, slope_c[i - 1], slope_c[i], rough_c[i - 1],
                                 rough_c[i], sand_c[i], P)
            T[i], E[i] = T[i - 1] + dt * dm, E[i - 1] + dE * em
            sig[i] = sigma_move(sig[i - 1], d, sand_c[i], S[i], O[i], cam, self.LP)
            dist[i] = dist[i - 1] + d
            hz_eff = max(clear[i] - self.lp.n_sigma * sig[i], 0.0)
            risk_acc += K.risk_per_m(d, dz, slope_c[i - 1], slope_c[i], rough_c[i - 1], rough_c[i],
                                     hz_eff, P) * d
            risk_len += d
        # --- return-to-shelter margin on every consumable (SYS-9)
        CONS = self.cons.coeffs(mode)
        retT = np.full(n, 1e12); retE = np.full(n, 1e12)
        if ctx is not None:
            wr0, wc0 = ctx["origin"]
            wi = np.clip(ri - wr0, 0, ctx["retT"].shape[0] - 1)
            wj = np.clip(ci - wc0, 0, ctx["retT"].shape[1] - 1)
            retT, retE = ctx["retT"][wi, wj], ctx["retE"][wi, wj]
        deadline = self._deadline_s()
        marg = np.full(n, np.inf)
        for r in range(CONS.shape[0]):
            a, b, cap = CONS[r]
            extra = np.zeros(n); extra[-1] = on_site
            used = a * (T + extra) + b * E
            need = used + (1 + self.cons.return_margin) * (a * retT + b * retE)
            marg = np.minimum(marg, ((1 - self.cons.reserve_frac) * cap - need) / cap)
        if math.isfinite(deadline):
            extra = np.zeros(n); extra[-1] = on_site
            marg = np.minimum(marg, (deadline - (T + extra) - (1 + self.cons.return_margin) * retT) / max(deadline, 1.0))
        marg = np.where(retT >= 1e11, -1.0, marg)

        viol = []
        nsg = self.lp.n_sigma
        k_nogo = int(in_nogo.sum())
        if k_nogo:
            viol.append(f"route enters no-go terrain at {k_nogo} sample(s)")
        buf_idx = np.nonzero(clear < nsg * sig - 1e-6)[0]
        k_buf = int(len(buf_idx))
        if k_buf:
            viol.append(f"hazard buffer (n_sigma x predicted sigma) violated at {k_buf} sample(s); "
                        f"worst shortfall {float((nsg * sig - clear).max()):.1f} m")
        # an isolated single steep cell is treated as DEM noise by the no-go mask (binary opening), so only
        # count a slope violation where the whole 3x3 neighbourhood is steep
        slope_min = ndimage.minimum_filter(t.slope[r0:r1, c0:c1], size=3)[ri - r0, ci - c0]
        k_sl = int((slope_min > agent.max_slope_deg + 1.0).sum())
        if k_sl:
            viol.append(f"terrain slope above the {agent.max_slope_deg:g} deg limit at {k_sl} sample(s)")
        k_lost = int((sig > self.lp.sigma_lost_m).sum())
        if k_lost:
            viol.append(f"position confidence 'lost' (sigma > {self.lp.sigma_lost_m:g} m) at {k_lost} sample(s)")
        if enforce_return and marg.min() < -1e-6:
            viol.append(f"return-to-shelter margin negative (min {marg.min():.3f} of capacity)")
        return {"rr": rr, "cc": cc, "sf": sf, "z": z, "enu": enu, "sig": sig, "T": T, "E": E,
                "dist": dist, "marg": marg, "clear": clear, "slope": slope_c, "rough": rough_c,
                "sand": sand_c, "S": S, "O": O, "viol": viol, "stops": stops,
                "risk": risk_acc / max(risk_len, 1.0), "retT": retT, "retE": retE, "buf_idx": buf_idx}

    @staticmethod
    def _dp(xy, eps):
        """Douglas-Peucker simplification; returns kept indices."""
        n = len(xy)
        keep = np.zeros(n, bool)
        keep[[0, n - 1]] = True
        stack = [(0, n - 1)]
        while stack:
            a, b = stack.pop()
            if b <= a + 1:
                continue
            p, q = xy[a], xy[b]
            seg = q - p
            L = np.hypot(*seg)
            pts = xy[a + 1:b] - p
            dd = np.abs(pts[:, 0] * seg[1] - pts[:, 1] * seg[0]) / L if L > 1e-9 else np.hypot(pts[:, 0], pts[:, 1])
            k = int(np.argmax(dd))
            if dd[k] > eps:
                m = a + 1 + k
                keep[m] = True
                stack += [(a, m), (m, b)]
        return np.nonzero(keep)[0]

    def _finalise(self, route, ctx, origin, mode, agent, P, profile, WB, bin_s, CONS, on_site,
                  deadline, sig0, t0, e0, enforce_return):
        rows, cols, meth = route["rows"], route["cols"], route["meth"]
        v = self.verify_route(rows, cols, meth > 0, mode, ctx, sig0, t0, e0, enforce_return, on_site)
        n = len(v["rr"])
        stops = v["stops"]
        stop_idx = [s["idx"] for s in stops]
        wi = set(self._dp(v["enu"][:, :2], 5.0).tolist()) | set(stop_idx)
        wl = sorted(wi)
        # keep waypoints at most ~150 m apart so the HUD ribbon stays registered
        full = [wl[0]]
        for i in wl[1:]:
            while v["dist"][i] - v["dist"][full[-1]] > 150.0:
                j = int(np.searchsorted(v["dist"], v["dist"][full[-1]] + 150.0))
                j = min(max(j, full[-1] + 1), i - 1) if i - 1 > full[-1] else None
                if j is None or j <= full[-1]:
                    break
                full.append(j)
            full.append(i)
        wl = sorted(set(full))
        diffs = []
        soft = agent.soft_slope_deg
        for a, b in zip(wl[:-1], wl[1:]):
            sl = slice(a, b + 1)
            hard = (v["slope"][sl].max() > soft or v["rough"][sl].max() > 2.5 or
                    v["clear"][sl].min() < 60 or v["sand"][sl].max() > 0.6)
            mod = (v["slope"][sl].max() > 0.6 * soft or v["rough"][sl].max() > 1.2 or
                   v["clear"][sl].min() < 120 or v["sand"][sl].max() > 0.3)
            diffs.append("hard" if hard else "moderate" if mod else "easy")
        marg = v["marg"]
        tb = int(len(marg) - 1 - np.argmin(marg[::-1])) if enforce_return else len(marg) - 1
        # consumables left at the goal
        c_goal = {}
        for r, nm in enumerate(self.cons.NAMES):
            a, b, cap = CONS[r]
            used = a * (v["T"][-1] + on_site) + b * v["E"][-1]
            c_goal[nm] = round(float(max(0.0, 1.0 - used / cap)), 3)
        # confidence timeline (lander screen, spec 11.4)
        lv = [confidence_level(s) for s in v["sig"]]
        tl, a = [], 0
        for i in range(1, n + 1):
            if i == n or lv[i] != lv[a]:
                tl.append({"from_m": round(float(v["dist"][a]), 1), "to_m": round(float(v["dist"][i - 1]), 1),
                           "level": lv[a]})
                a = i
        t_end = v["T"][-1]
        ret_total = float(v["retT"][-1]) if v["retT"][-1] < 1e11 else 0.0
        dose = dose_msv(t_end + on_site + ret_total, self.sep, 0.0)
        ok_w, w_alerts = self.weather.hard_limits(t0 / 60.0, (t_end + on_site + ret_total - t0) / 60.0)
        alerts = list(w_alerts)
        if stops:
            alerts.append({"type": "fix_stop", "msg": f"{len(stops)} stop-and-fix pause(s) added to keep position "
                           f"confidence adequate near hazards ({sum(s['duration_s'] for s in stops):.0f} s in total)"})
        if "low" in lv or "lost" in lv:
            alerts.append({"type": "confidence", "msg": "predicted position confidence drops to LOW on part of the route"})
        if not enforce_return:
            alerts.append({"type": "info", "msg": "Return-to-shelter was NOT checked for this plan (pure start-to-goal path search)."})
        elif marg.min() < 0.05:
            alerts.append({"type": "margin", "msg": "return-to-shelter margin below 5 % of capacity at the turn-back point"})
        if self.sep.active:
            alerts.append({"type": "sep", "msg": f"SEP onset in {self.sep.onset_min:.0f} min; route must finish "
                           f"{self.sep.return_margin_min:.0f} min earlier"})
        hold = (len(v["viol"]) == 1 and v["viol"][0].startswith("hazard buffer") and len(v["buf_idx"])
                and v["buf_idx"].max() <= 2)
        if hold:
            # the ONLY problem is the starting position itself: the crew is already inside the
            # uncertainty buffer of a hazard. The right answer is "fix your position first".
            v["viol"] = [f"At the start, the 3-sigma position uncertainty ({self.lp.n_sigma * v['sig'][0]:.0f} m) "
                         f"exceeds the clearance to the nearest hazard ({max(v['clear'][0], 0):.0f} m). "
                         f"Stop and take a position fix before moving."]
            verdict = "HOLD - take a position fix before moving"
        elif v["viol"]:
            verdict = "NO-GO - verifier found constraint violations"
        elif not ok_w:
            verdict = "NO-GO - weather limits"
        elif not enforce_return:
            verdict = "GO - return to shelter NOT checked"
        elif marg.min() < 0.05:
            verdict = "GO - tight return margin"
        else:
            verdict = "GO"
        plan = MissionPlan(mode=mode, profile=profile, rows=v["rr"], cols=v["cc"], enu=v["enu"],
                           sigma=v["sig"], t_s=v["T"], e_j=v["E"], dist_m=v["dist"], stops=stops,
                           return_margin=marg, eta_min=float((t_end - t0) / 60.0),
                           energy_kj=float((v["E"][-1] - e0) / 1000.0), risk_score=float(v["risk"]),
                           turn_back_idx=tb, waypoint_idx=wl, segment_difficulty=diffs, alerts=alerts,
                           violations=v["viol"], verdict=verdict, dose_msv=dose, consumables=c_goal,
                           confidence_timeline=tl)
        plan.return_checked = bool(enforce_return)
        plan.sep = {"active": self.sep.active, "dose_mSv_baseline_plus_sep": round(dose, 4)}
        plan._ctx = ctx
        plan._verify = v
        return plan

    # ------------------------------------------------------------ SEP: fastest route to shelter (spec 9.5, SYS-9)
    def plan_shelter_route(self, position_enu, mode="walk", loc_state=None, t_elapsed_s=0.0,
                           e_used_j=0.0, sep=None):
        """Switch to the fastest route to shelter and report the countdown. Consumable-margin
        constraints are dropped (the crew is going home regardless); hazard buffers, terrain
        limits and the sigma model still apply."""
        if sep is not None:
            self.sep = sep
        best = None
        for sh in self.shelters_enu:
            try:
                p = self.plan(position_enu, sh, mode=mode, profile="min_eta", loc_state=loc_state,
                              refine=False, t_elapsed_s=t_elapsed_s, e_used_j=e_used_j,
                              enforce_return=False, on_site_s=0.0)
            except RuntimeError:
                continue
            if best is None or p.t_s[-1] < best.t_s[-1]:
                best = p
        if best is None:
            raise RuntimeError("No route to any shelter under the terrain and hazard limits.")
        arrive = float(best.t_s[-1])
        onset = self.sep.onset_min * 60.0
        countdown = onset - arrive if self.sep.active else math.inf
        outdoor = dose_msv(arrive - t_elapsed_s, self.sep, t_elapsed_s)
        best.sep = {"active": self.sep.active, "arrive_s": round(arrive, 1),
                    "onset_s": None if not self.sep.active else onset,
                    "countdown_s": None if not self.sep.active else round(countdown, 1),
                    "arrives_before_onset": bool(countdown > 0) if self.sep.active else True,
                    "dose_to_shelter_mSv": round(outdoor, 4)}
        best.alerts = [a for a in best.alerts if a["type"] != "sep"]
        best.alerts.append({"type": "sep", "msg": (
            f"SOLAR PARTICLE EVENT: fastest route to shelter, ETA {best.eta_min:.0f} min"
            + (f", countdown {countdown / 60:.0f} min to onset" if self.sep.active else ""))})
        best.verdict = "SHELTER ROUTE" if best.sep["arrives_before_onset"] else "SHELTER ROUTE - ONSET BEFORE ARRIVAL"
        return best

    # ------------------------------------------------------------ replan triggers (spec 9.5)
    def replan(self, trigger, current_enu, goal_enu, mode="walk", profile="min_eta", loc_state=None,
               t_elapsed_s=0.0, e_used_j=0.0, hazards=(), replanner=None, sep=None, enforce_return=True):
        """One entry point for the replan-trigger table in spec 9.5. Returns (plan, info)."""
        t0 = _time.time()
        info = {"trigger": trigger}
        if trigger == "sep_alert":
            plan = self.plan_shelter_route(current_enu, mode, loc_state, t_elapsed_s, e_used_j, sep)
            info["response"] = "fastest route to shelter + countdown"
        elif trigger == "new_hazard":
            from replan import DStarReplanner
            rp = replanner or DStarReplanner(self, current_enu, goal_enu, mode, profile,
                                             sigma_assumed_m=(loc_state.sigma_m() * 1.5 + 5.0) if loc_state else None,
                                             enforce_return=enforce_return)
            for (xy, rad) in hazards:
                rp.block_hazard(xy, rad)
            try:
                plan = rp.plan(current_enu=current_enu, loc_state=loc_state, t_elapsed_s=t_elapsed_s,
                               e_used_j=e_used_j, enforce_return=enforce_return)
                info["response"] = "local replan with D* Lite"
                if any("hazard buffer" in v or "no-go" in v or "confidence" in v for v in plan.violations):
                    raise RuntimeError("sigma-dependent constraint failed on the D* Lite route")
            except RuntimeError as e:
                info["dstar_note"] = str(e)
                plan = self.plan(current_enu, goal_enu, mode, profile, loc_state, refine=False,
                                 t_elapsed_s=t_elapsed_s, e_used_j=e_used_j, enforce_return=enforce_return)
                info["response"] = "fell back to full uncertainty-aware A*"
            info["replanner"] = rp
        elif trigger in ("confidence_drop", "dust"):
            try:
                plan = self.plan(current_enu, goal_enu, mode, profile, loc_state, refine=False,
                                 t_elapsed_s=t_elapsed_s, e_used_j=e_used_j, enforce_return=enforce_return)
                info["response"] = ("re-evaluated fix plan with current weather: fix stop / reroute toward "
                                    "high-localizability terrain")
            except RuntimeError as e:
                # spec 14 (dust storm): when fixes cannot be taken, position confidence cannot be kept near the
                # hazards: recommend wait or return, and have the way back ready
                info["why"] = str(e)
                plan = self.plan_shelter_route(current_enu, mode, loc_state, t_elapsed_s, e_used_j)
                info["response"] = ("no safe route to the goal in these conditions (position fixes unavailable): "
                                    "RECOMMEND WAIT OR RETURN; fastest route to shelter planned")
        elif trigger == "consumables":
            try:
                plan = self.plan(current_enu, goal_enu, mode, profile, loc_state, refine=False,
                                 t_elapsed_s=t_elapsed_s, e_used_j=e_used_j)
                info["response"] = "route re-checked against remaining consumables (turn-back point moved)"
            except RuntimeError:
                plan = self.plan_shelter_route(current_enu, mode, loc_state, t_elapsed_s, e_used_j)
                info["response"] = "goal no longer reachable with margin: route to shelter"
        elif trigger == "integrity_alert":
            infl = LocalizationState(covariance_enu_m2=[[(2 * (loc_state.sigma_m() if loc_state else 30.0)) ** 2, 0, 0],
                                                        [0, (2 * (loc_state.sigma_m() if loc_state else 30.0)) ** 2, 0],
                                                        [0, 0, 1]])
            plan = self.plan(current_enu, goal_enu, mode, profile, infl, refine=False,
                             t_elapsed_s=t_elapsed_s, e_used_j=e_used_j)
            plan.alerts.insert(0, {"type": "integrity", "msg": "sources disagree: STOP AND VERIFY before continuing near hazards"})
            info["response"] = "sigma inflated x2, stop-and-verify prompt"
        else:
            raise ValueError(f"unknown trigger '{trigger}'")
        info["latency_s"] = round(_time.time() - t0, 3)
        info["meets_SYS3_5s"] = info["latency_s"] <= 5.0
        return plan, info

    # ------------------------------------------------------------ AI layer tool: rank candidate sites (spec 6)
    def rank_sites(self, query, start=(0.0, 0.0), loc_state=None, top_n=3):
        """The 'coarse planner run' the target resolver scores sites with. Every number the
        resolver quotes comes from here (spec 6 guardrails: the language model sets no limits).
        `query` follows the AI layer -> planner contract in spec 13."""
        mode = query.get("mode", "walk")
        budget = float(query.get("time_budget_min", 120.0))
        out = []
        for site in query["candidate_sites"]:
            row = {"id": site["id"], "enu_m": site["enu_m"], "science_score": float(site.get("science_score", 0.5))}
            try:
                p = self.plan(start, tuple(site["enu_m"][:2]), mode=mode, profile="balanced",
                              loc_state=loc_state, refine=False)
            except (RuntimeError, ValueError) as e:
                row.update(feasible=False, reasoning=f"{site['id']}: no safe route ({str(e)[:90]})", score=0.0)
                out.append(row)
                continue
            ret_min = (float(p._verify["retT"][-1]) / 60.0) if p._verify["retT"][-1] < 1e11 else 0.0
            round_trip = p.eta_min + self.cons.on_site_s / 60.0 + ret_min
            loc = float(np.mean(np.maximum(p._verify["S"], p._verify["O"])))
            n_hard = sum(1 for d in p.segment_difficulty if d == "hard")
            fits = round_trip <= budget and not p.violations
            time_fit = max(0.0, 1.0 - round_trip / budget)
            score = 0.45 * row["science_score"] + 0.25 * time_fit + 0.15 / (1.0 + p.risk_score) + 0.15 * loc
            conf = "good" if loc > 0.5 else "fair" if loc > 0.25 else "poor"
            why = (f"{site.get('label', site['id'])}; {p.dist_m[-1] / 1000:.1f} km; ETA {p.eta_min:.0f} min "
                   f"(round trip {round_trip:.0f} min of {budget:.0f}); {conf} localization; "
                   + (f"{n_hard} hard section(s)" if n_hard else "no hard sections")
                   + (f"; {len(p.stops)} fix stop(s)" if p.stops else ""))
            row.update(feasible=bool(fits), score=round(score if fits else 0.0, 3), eta_min=round(p.eta_min, 1),
                       round_trip_min=round(round_trip, 1), distance_km=round(p.dist_m[-1] / 1000, 2),
                       risk_score=round(p.risk_score, 3), localizability=round(loc, 2),
                       hard_sections=n_hard, fix_stops=len(p.stops), verdict=p.verdict,
                       reasoning=why if fits else why + "; exceeds time budget" if round_trip > budget else why)
            out.append(row)
        out.sort(key=lambda r: -r["score"])
        return out[:top_n]
