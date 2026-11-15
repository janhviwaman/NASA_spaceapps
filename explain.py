"""
explain.py - 'how was this route calculated?' for the lander terminal.

Everything here is RE-COMPUTED from the finished route with the same physics kernels the search used, so the
numbers shown to the crew are the planner's own, not a story written afterwards:

  * cost ingredients along the route (time, energy, risk, localization penalty, fix stops) with the profile weights
  * position error vs the safety buffer it must stay inside (the uncertainty-aware part, spec 9.2)
  * what a straight line would have run into (no-go terrain, steep ground, hazard clearance)
  * the search itself: which (cell, position-error) states A* expanded, in order, for the replay view
  * a numbered recipe with this run's own counts
"""
import math

import numpy as np
from scipy import ndimage

import kernels as K


def _f(x, nd=1):
    return round(float(x), nd)


def _terms(mp, plan):
    """Per-sample cost ingredients (seconds-equivalent) recomputed along the final route."""
    v = plan._verify
    agent = mp.agents[plan.mode]
    P = agent.params()
    mid = agent.mode_id
    wt, we, wr, wl = (float(x) for x in mp._weights(plan.profile))
    res = mp.t.res
    nsig, base = mp.lp.n_sigma, mp.lp.base_buffer_m
    WB, bin_s = mp.weather.sampled()
    n = len(v["sig"])
    c = {k: np.zeros(n) for k in ("time", "energy", "risk", "loc", "stop")}
    for i in range(1, n):
        d = float(v["dist"][i] - v["dist"][i - 1])
        if d < 1e-9:                                      # a stop-and-fix pause
            dT = float(v["T"][i] - v["T"][i - 1])
            dE = float(v["E"][i] - v["E"][i - 1])
            c["stop"][i] = wt * dT + we * dE / 1000.0
            continue
        bi = min(int(v["T"][i - 1] / bin_s), WB.shape[1] - 1)
        dm, em = WB[0, bi], WB[1, bi]
        dz = float(v["z"][i] - v["z"][i - 1])
        dt, dE = K.edge_phys(mid, d, dz, v["slope"][i - 1], v["slope"][i], v["rough"][i - 1], v["rough"][i], v["sand"][i], P)
        hz_eff = max(float(v["clear"][i]) + 0.5 * res - nsig * float(v["sig"][i]), 0.0)
        risk = K.risk_per_m(d, dz, v["slope"][i - 1], v["slope"][i], v["rough"][i - 1], v["rough"][i], hz_eff, P)
        L = max(float(v["S"][i]), float(v["O"][i]))
        c["time"][i] = wt * dt * dm
        c["energy"][i] = we * dE * em / 1000.0
        c["risk"][i] = wr * risk * d
        c["loc"][i] = wl * (1.0 - L) * d
    return c, (wt, we, wr, wl)


def _straight_line(mp, plan):
    """What would walking (driving) the straight line from start to goal run into?"""
    t = mp.t
    agent = mp.agents[plan.mode]
    nogo, hazd = t.mode_layers(plan.mode, agent.max_slope_deg, agent.rough_max, agent.sand_max)
    r0, c0, r1, c1 = float(plan.rows[0]), float(plan.cols[0]), float(plan.rows[-1]), float(plan.cols[-1])
    L = math.hypot(r1 - r0, c1 - c0) * t.res
    n = max(2, int(math.ceil(L / 10.0)) + 1)
    rr, cc = np.linspace(r0, r1, n), np.linspace(c0, c1, n)
    ri = np.clip(np.rint(rr).astype(int), 0, t.shape[0] - 1)
    ci = np.clip(np.rint(cc).astype(int), 0, t.shape[1] - 1)
    seg = L / (n - 1)
    pad = 6
    a0, a1 = max(0, ri.min() - pad), min(t.shape[0], ri.max() + pad + 1)
    b0, b1 = max(0, ci.min() - pad), min(t.shape[1], ci.max() + pad + 1)
    slope_min = ndimage.minimum_filter(t.slope[a0:a1, b0:b1], size=3)[ri - a0, ci - b0]
    z = ndimage.map_coordinates(t.elev, [rr, cc], order=1, mode="nearest")
    P = agent.params()
    eta = 0.0
    for i in range(1, n):
        dt, _ = K.edge_phys(agent.mode_id, seg, float(z[i] - z[i - 1]), t.slope[ri[i - 1], ci[i - 1]], t.slope[ri[i], ci[i]],
                            t.rough[ri[i - 1], ci[i - 1]], t.rough[ri[i], ci[i]], t.sand[ri[i], ci[i]], P)
        eta += dt
    blocked = nogo[ri, ci]
    steep = slope_min > agent.max_slope_deg + 1.0
    return {"distance_km": _f(L / 1000, 2), "nogo_m": _f(blocked.sum() * seg, 0), "over_slope_m": _f(steep.sum() * seg, 0),
            "max_slope_deg": _f(slope_min.max(), 1), "min_clearance_m": _f(max(float(hazd[ri, ci].min()) - 0.5 * t.res, 0.0), 0),
            "eta_min": _f(eta / 60.0, 0), "blocked": bool(blocked.any() or steep.any()),
            "route_longer_pct": _f(100.0 * (float(plan.dist_m[-1]) / L - 1.0), 0) if L > 1 else 0.0}


def _search(mp, plan, max_explored):
    s = getattr(plan, "_search", None)
    if not s or len(s["rec"]) == 0:
        return None
    rec = np.asarray(s["rec"], dtype=np.int64)
    if len(rec) > max_explored:
        rec = rec[np.linspace(0, len(rec) - 1, max_explored).astype(int)]
    K_ = s["K"]
    r0, c0 = s["origin"]
    e0, n0 = mp.frame.rc_to_enu(mp.t, float(r0), float(c0))
    return {"cells": (rec // K_).astype(int).tolist(), "levels": (rec % K_).astype(int).tolist(), "w": int(s["wshape"][1]),
            "h": int(s["wshape"][0]), "res": float(mp.t.res), "e0": _f(e0, 1), "n0": _f(n0, 1),
            "sigma_levels": [float(x) for x in mp.lp.sigma_levels], "expanded": int(s["expanded"]), "step": int(s["step"]),
            "shown": int(len(rec))}


def _steps(mp, plan, W, straight, search, idx_stats):
    st = plan.stats
    agent = mp.agents[plan.mode]
    wt, we, wr, wl = W
    v = plan._verify
    rover = plan.mode == "rover"
    steps = []
    steps.append({"title": "Read the terrain",
                  "text": f"The {mp.t.res:.0f} m elevation model gives the slope, roughness and cliff edges of every cell. "
                          f"{st.get('nogo_pct_in_window', 0)} % of the {st.get('window_cells', 0) / 1e6:.2f} million cells in the search window are no-go "
                          f"{'for the rover' if rover else 'on foot'} (slope over {agent.max_slope_deg:g} deg, cliffs"
                          f"{', blocky ground or soft sand' if rover else ''}). Each cell also knows how far it is from the nearest hazard."})
    dm = mp.weather.speed_cost_mult((plan.t_s[0]) / 60.0)
    physics = ("a rolling-resistance and climb model, slowed by slope, rocks and sand" if rover
               else "Tobler's hiking speed and the Minetti metabolic cost, scaled to Mars gravity and suit mass")
    steps.append({"title": "Price every possible step",
                  "text": f"Each step costs {wt:g} x time + {we:g} x energy (kJ) + {wr:g} x risk x distance + {wl:g} x (1 - localizability) x distance, "
                          f"in seconds-equivalent (profile '{plan.profile}'). Time and energy come from {physics}"
                          + (f", and dust multiplies the time by {dm:.2f}." if dm > 1.001 else ".")})
    if search:
        n_stops = len(plan.stops)
        steps.append({"title": "Search with the position error built into the state",
                      "text": f"A* does not search plain cells but (cell, position-error) states: {st.get('sigma_levels', '?')} levels from "
                              f"{mp.lp.sigma_levels[0]:g} to {mp.lp.sigma_levels[-1]:g} m. Position error grows with distance (faster on sand) and shrinks where the "
                              f"ground gives good camera matches. It expanded {search['expanded']:,} states. {st.get('pruned_hazard_buffer', 0):,} moves were rejected "
                              f"because +/-{mp.lp.n_sigma:g} sigma of position error would reach a hazard, {st.get('pruned_return_margin', 0):,} because the crew "
                              f"could no longer get back to the shelter with margin. It weighed {st.get('fix_stops_considered', 0):,} possible fix stops and chose {n_stops}."})
    else:
        steps.append({"title": "Incremental repair (D* Lite)",
                      "text": "This route came from D* Lite, which repairs the previous search around the change instead of starting over "
                              f"({st.get('dstar_iterations', '?')} iterations). Its result is re-checked, and the full uncertainty-aware search is used if the check fails."})
    if st.get("refined"):
        steps.append({"title": "Refine on a finer grid",
                      "text": f"The coarse route ({st.get('coarse_route_points', '?')} points) was re-planned on a {mp.t.res / mp.refine_factor:g} m grid inside a "
                              f"{mp.corridor_m:g} m corridor, in {st.get('fine_chunks', '?')} chunks. (The finer grid is the same elevation data resampled: it straightens "
                              f"the route but adds no new terrain detail.)"})
    else:
        steps.append({"title": "Refinement", "text": "The coarse route was kept (refinement was off, failed, or did not pass the verifier)."})
    marg = float(np.min(v["marg"]))
    steps.append({"title": "Check it independently",
                  "text": f"A separate verifier re-computed the position error, time, energy, hazard clearance and return margin on {len(v['sig'])} samples "
                          f"about every 10 m: {len(plan.violations)} violation(s). Tightest hazard clearance {float(v['clear'].min()):.0f} m; "
                          + (f"smallest return margin {marg * 100:.0f} % of the tightest resource." if getattr(plan, 'return_checked', True) else "return to shelter was not checked.")})
    if straight:
        if straight["blocked"]:
            why = (f"A straight line ({straight['distance_km']} km) would cross {straight['nogo_m']:.0f} m of no-go terrain"
                   + (f" and {straight['over_slope_m']:.0f} m steeper than the limit (up to {straight['max_slope_deg']} deg)" if straight["over_slope_m"] > 0 else "")
                   + f". The chosen route is {straight['route_longer_pct']:.0f} % longer to avoid that.")
        else:
            why = (f"A straight line would be {straight['distance_km']} km with {straight['min_clearance_m']:.0f} m minimum clearance; "
                   f"the chosen route is {straight['route_longer_pct']:.0f} % longer because of slope, risk and position-error costs.")
        steps.append({"title": "Why not a straight line?", "text": why})
    return steps


def build(mp, plan, idx, max_explored=120000):
    """Explanation payload aligned with the route samples `idx` used by the map (see MissionWeb._payload)."""
    v = plan._verify
    terms, W = _terms(mp, plan)
    idx = np.asarray(idx, dtype=int)
    dist = np.asarray(v["dist"])
    cs = {k: np.cumsum(a) for k, a in terms.items()}
    per_m = {k: [] for k in ("time", "energy", "risk", "loc")}
    for j in range(len(idx)):
        a, b = (idx[j - 1], idx[j]) if j > 0 else (0, idx[min(1, len(idx) - 1)])
        length = max(float(dist[b] - dist[a]), 1e-6)
        for k in per_m:
            per_m[k].append(round(float((cs[k][b] - cs[k][a]) / length), 4))
    totals = {k: float(a.sum()) for k, a in terms.items()}
    total = sum(totals.values()) or 1.0
    nsig = mp.lp.n_sigma
    need = nsig * np.asarray(v["sig"])
    slack = np.asarray(v["clear"]) - need
    grade = np.zeros(len(dist))
    dz = np.diff(v["z"])
    dd = np.maximum(np.diff(dist), 1e-6)
    grade[1:] = np.degrees(np.arctan(dz / dd)) * (np.diff(dist) > 1e-9)
    # tightest places: lowest slack, spread apart
    order = np.argsort(slack)
    tight, used = [], []
    for i in order:
        if all(abs(dist[i] - dist[j]) > 150.0 for j in used):
            used.append(int(i))
            tight.append({"dist_m": _f(dist[i], 0), "enu": [_f(v["enu"][i][0], 1), _f(v["enu"][i][1], 1)], "slack_m": _f(slack[i], 0),
                          "clearance_m": _f(v["clear"][i], 0), "needed_m": _f(need[i], 0), "sigma_m": _f(v["sig"][i], 1)})
        if len(tight) >= 3:
            break
    straight = _straight_line(mp, plan)
    search = _search(mp, plan, max_explored)
    out = {
        "weights": {"time": W[0], "energy": W[1], "risk": W[2], "loc": W[3]},
        "per_m": per_m,
        "stop_cost": [{"dist_m": _f(dist[s["idx"]], 0), "cost": _f(terms["stop"][s["idx"]], 1)} for s in plan.stops],
        "totals": {k: _f(val, 0) for k, val in totals.items()}, "total": _f(total, 0),
        "pct": {k: _f(100.0 * val / total, 0) for k, val in totals.items()},
        "series": {"grade_deg": [_f(grade[i], 1) for i in idx], "slope_deg": [_f(v["slope"][i], 1) for i in idx],
                   "sigma_m": [_f(v["sig"][i], 2) for i in idx], "clear_m": [_f(max(v["clear"][i], 0), 0) for i in idx],
                   "need_m": [_f(need[i], 1) for i in idx]},
        "tight": tight, "n_sigma": nsig, "straight": straight, "search": search,
    }
    out["steps"] = _steps(mp, plan, W, straight, search, None)
    return out
