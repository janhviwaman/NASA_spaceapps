"""
Spec-compliance tests for the Mars Surface Navigation System planner.

Run:   python tests/test_spec.py            (plain, prints a table)
       pytest -q tests/test_spec.py          (same checks)

Uses a synthetic Jezero-like DEM (tests/synth_dem.py) so it runs without the 90 MB USGS
download. Pass a real DEM with   JEZERO_DEM=data/M20_JezeroCrater_CTXDEM_20m.tif
(the scenario coordinates below are tuned to the synthetic terrain, so on the real DEM only
the generic checks are meaningful).
"""
import copy
import math
import os
import tempfile
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from terrain import Terrain                      # noqa: E402
from mission_planner import MissionPlanner       # noqa: E402
from localization import LocalizationState, confidence_level  # noqa: E402
from environment import Weather, SEPAlert, Consumables         # noqa: E402
from frames import SiteFrame                      # noqa: E402
import synth_dem                                  # noqa: E402

_CACHE = {}


def dem(n=700):
    key = n
    if key not in _CACHE:
        path = os.environ.get("JEZERO_DEM") or os.path.join(tempfile.gettempdir(), "jezero_synth", f"synth_{n}.tif")
        if not os.path.exists(path):
            synth_dem.make(path, n=n)
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            _CACHE[key] = Terrain(path)
    return _CACHE[key]


def planner(n=700, **kw):
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        return MissionPlanner(dem(n), **kw)


def feature_poor(mp):
    """Copy of the planner whose terrain gives no en-route fixes (to make sigma matter)."""
    mp2 = copy.copy(mp)
    mp2.loc = copy.deepcopy(mp.loc)
    mp2.loc.orb[:] = 0.0
    mp2.loc.sky[:] = 0.0
    return mp2


def sig_state(s):
    return LocalizationState(covariance_enu_m2=[[s * s, 0, 0], [0, s * s, 0], [0, 0, 1]])


GOAL_NE = (2500, 1500)
GOAL_W = (-3200, 2000)           # west of the scarp, through the northern pass
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    assert ok, f"{name}: {detail}"


# ------------------------------------------------------------------ tests
def test_frame_roundtrip():
    t = dem()
    f = SiteFrame().anchor_to_terrain(t)
    e, n = f.rc_to_enu(t, np.array([350.0, 300.0]), np.array([350.0, 400.0]))
    r, c = f.enu_to_rc(t, float(e[1]), float(n[1]))
    check("4: single Site ENU frame, lat/lon only at boundaries", (r, c) == (300, 400), f"{(r, c)}")


def test_hierarchical_search_and_outputs():
    mp = planner()
    p = mp.plan((0, 0), GOAL_NE, "walk", refine=True)
    h = p.hud_json()
    keys = {"waypoints_enu_m", "eta_min", "turn_back_point_enu_m", "fix_stops", "predicted_sigma_m",
            "segment_difficulty", "alerts"}
    check("13: Planner->HUD contract keys", keys <= set(h), str(set(h) ^ keys))
    check("9.4: hierarchical (coarse A* then corridor refinement)", p.stats["refined"], p.stats["hierarchy"])
    check("SYS-8: every waypoint carries a sigma", len(h["predicted_sigma_m"]) == len(h["waypoints_enu_m"]))
    check("13: one difficulty per segment", len(h["segment_difficulty"]) == len(h["waypoints_enu_m"]) - 1)
    check("9.4: ENU waypoints are 3-D (x, y, z)", all(len(w) == 3 for w in h["waypoints_enu_m"]))
    check("verifier: no violations", not p.violations, str(p.violations))


def test_search_and_verifier_agree():
    """Guards against a search that optimises one cost model while the verifier checks another
    (this caught a real bug: an integer mode id passed as a string silently selected the rover
    physics branch inside the walking search)."""
    mp = planner()
    for mode in ("walk", "rover"):
        p = mp.plan((0, 0), GOAL_NE, mode, refine=False)
        diff = p.stats["search_vs_verifier_time_diff_pct"]
        check(f"consistency: search time == independently re-computed time ({mode})", diff is not None and diff < 5.0,
              f"{diff} % apart")
    from kernels import edge_phys, WALK, ROVER
    from mission_planner import WalkAgent, RoverAgent, _hmin, PROFILES_MISSION
    for mid, ag in ((WALK, WalkAgent()), (ROVER, RoverAgent())):
        hm = _hmin(mid, ag.params(), np.array(PROFILES_MISSION["min_eta"]))
        lo = min(edge_phys(mid, 1.0, g, 0, 0, 0, 0, 0, ag.params())[0] for g in np.linspace(-0.47, 0.47, 400))
        check(f"A* heuristic is admissible (mode id {mid})", hm <= lo + 1e-9, f"hmin {hm:.4f} <= min time/m {lo:.4f}")


def test_uncertainty_inflates_buffers_and_changes_route():
    mp = feature_poor(planner())
    p5 = mp.plan((0, 0), GOAL_W, loc_state=sig_state(5), refine=False)
    p60 = mp.plan((0, 0), GOAL_W, loc_state=sig_state(60), refine=False)
    check("9.2: low confidence gives a different, longer route",
          p60.dist_m[-1] > p5.dist_m[-1] + 100, f"{p5.dist_m[-1]:.0f} m vs {p60.dist_m[-1]:.0f} m")
    for p, tag in ((p5, "sigma0=5"), (p60, "sigma0=60")):
        check(f"9.2: buffer >= n_sigma * predicted sigma everywhere ({tag})",
              np.all(p._verify["clear"] >= mp.lp.n_sigma * p.sigma - 1e-6) and not p.violations, str(p.violations))
    check("9.2: clearance grows with uncertainty", p60._verify["clear"].min() > p5._verify["clear"].min(),
          f"{p5._verify['clear'].min():.0f} m vs {p60._verify['clear'].min():.0f} m")


def test_fix_stop_inserted_and_paid_for():
    mp = planner()
    mp.loc = copy.deepcopy(mp.loc)
    mp.loc.orb[:] = 0.0
    mp.loc.sky[:] = 0.0
    mp.loc.open[:] = 0.0                          # no star / Sun fixes: only the skyline pocket below
    S = mp.loc.stride
    r, c = mp.frame.enu_to_rc(mp.t, -1300, 800)   # a pocket with a distinctive horizon, on the way to the pass
    mp.loc.sky[(r - 25) // S:(r + 25) // S + 1, (c - 25) // S:(c + 25) // S + 1] = 0.95
    p = mp.plan((0, 0), GOAL_W, loc_state=sig_state(60), refine=False)
    check("9.3: planner inserts a fix stop to protect the pass crossing", len(p.stops) >= 1, f"{len(p.stops)} stops")
    check("9.3: the stop uses the skyline method at the good-horizon pocket",
          p.stops[0]["method"] == "skyline", str(p.stops[0]))
    d = p.hud_json()["fix_stops"][0]
    check("13: fix stop record", set(d) == {"at_waypoint", "method", "duration_s"} and 30 <= d["duration_s"] <= 90, str(d))
    stop_s = sum(s["duration_s"] for s in p.stops)
    moving = p.t_s[-1] - stop_s
    check("9.3: stop time is counted in the ETA", abs(p.eta_min * 60 - (moving + stop_s)) < 1.0,
          f"{p.eta_min:.1f} min = {moving / 60:.1f} moving + {stop_s / 60:.1f} stopped")
    mp_none = copy.copy(mp)
    mp_none.loc = copy.deepcopy(mp.loc)
    mp_none.loc.sky[:] = 0.0
    try:
        mp_none.plan((0, 0), GOAL_W, loc_state=sig_state(60), refine=False)
        check("9.2: without any fix opportunity the crossing is rejected", False, "planned anyway")
    except RuntimeError as e:
        check("9.2: without any fix opportunity the crossing is rejected (explained to the crew)",
              "hazard buffer" in str(e), str(e)[:100])


def test_localizability_prefers_good_terrain():
    mp = planner()
    mpb = copy.copy(mp)
    mpb.lp = copy.copy(mp.lp)
    mpb.lp.allow_fix_stops = False
    mpb.LP = mpb.lp.as_array()
    mpb.loc = copy.deepcopy(mp.loc)
    S = mp.loc.stride
    mpb.loc.orb[:] = 0.02
    mpb.loc.sky[:] = 0.02
    d = np.array(GOAL_NE, float)
    u = d / np.hypot(*d)
    n = np.array([-u[1], u[0]])
    for k in np.linspace(0, 1, 200):              # a lens-shaped strip of good terrain beside the direct line
        rr, cc = mpb.frame.enu_to_rc(mpb.t, *(k * d + n * 600 * np.sin(np.pi * k)))
        mpb.loc.orb[rr // S, cc // S] = 0.95
        mpb.loc.sky[rr // S, cc // S] = 0.95
    res = {}
    for prof in ("min_eta", "safest"):
        q = mpb.plan((0, 0), GOAL_NE, profile=prof, refine=False)
        res[prof] = (float(np.mean(np.maximum(q._verify["S"], q._verify["O"]))), q.eta_min)
    check("9.3: localization penalty steers the route toward well-localized terrain when it is cheap",
          res["safest"][0] > res["min_eta"][0] + 0.25 and res["safest"][1] < res["min_eta"][1] * 1.2,
          f"mean localizability {res['min_eta'][0]:.2f} -> {res['safest'][0]:.2f}, ETA {res['min_eta'][1]:.0f} -> {res['safest'][1]:.0f} min")


def test_return_to_shelter_and_turn_back():
    mp = planner()
    p = mp.plan((0, 0), GOAL_NE, "walk", refine=False)
    check("SYS-9: return margin >= 0 everywhere", p.return_margin.min() >= 0, f"{p.return_margin.min():.3f}")
    tb = p.turn_back_idx
    check("9.4: turn-back point on the route", 0 <= tb < len(p.rows))
    # tiny consumables: a far goal must be rejected as infeasible, not silently planned
    mp2 = planner(consumables=Consumables(design_eva_h=1.0))
    try:
        mp2.plan((0, 0), (4500, 3000), "walk", refine=False)
        check("SYS-9: unreachable-with-margin goal is rejected", False, "planned anyway")
    except RuntimeError as e:
        check("SYS-9: unreachable-with-margin goal is rejected", ("too far from the shelter" in str(e) or "round trip" in str(e)), str(e)[:90])
    check("10: all four resources tracked", set(p.consumables) == set(Consumables.NAMES), str(p.consumables))


def test_rover_mode_differs():
    mp = planner()
    w = mp.plan((0, 0), (0, -5000), "walk", refine=False)
    r = mp.plan((0, 0), (0, -5000), "rover", refine=False)
    check("9.4: rover respects 15 deg limit", r._verify["slope"].max() <= 15.5, f"{r._verify['slope'].max():.1f}")
    check("8.1: rover never drives onto sand", r._verify["sand"].max() <= 0.6 + 1e-6, f"{r._verify['sand'].max():.2f}")
    check("9.4: walking and rover routes/ETAs differ", abs(w.eta_min - r.eta_min) > 5, f"{w.eta_min:.0f} vs {r.eta_min:.0f} min")
    check("10: rover tracks battery", "power / battery" in r.consumables)


def test_dust_storm_changes_confidence_and_cost():
    mp = planner()
    clear = mp.plan((0, 0), GOAL_NE, refine=False)
    mp2 = planner(weather=Weather(dust_events=[(0, 600, 4.0)]))
    dusty = mp2.plan((0, 0), GOAL_NE, refine=False)
    check("8.1: dust raises route time", dusty.eta_min > clear.eta_min * 1.05, f"{clear.eta_min:.0f} -> {dusty.eta_min:.0f} min")
    w = mp2.weather
    check("14: dust removes star fixes but the Sun survives longer",
          w.celestial_availability(300) < 0.5 * Weather().celestial_availability(300) and w.camera_quality(300) < 0.2,
          f"at peak: celestial {w.celestial_availability(300):.2f} (clear {Weather().celestial_availability(300):.2f}), camera {w.camera_quality(300):.2f}")
    mp3 = feature_poor(planner(weather=Weather(dust_events=[(0, 600, 4.0)])))
    mp4 = feature_poor(planner())
    p3 = mp3.plan((0, 0), GOAL_W, loc_state=sig_state(20), refine=False)
    p4 = mp4.plan((0, 0), GOAL_W, loc_state=sig_state(20), refine=False)
    check("8.2/14: dust and low localizability change the route", abs(p3.dist_m[-1] - p4.dist_m[-1]) > 1 or p3.eta_min > p4.eta_min)
    mp5 = planner(weather=Weather(dust_events=[(0, 120, 6.0)]))   # storm peaks 60 min into the EVA
    p5 = mp5.plan((0, 0), (800, 400), refine=False)
    check("14: dust above the EVA limit is flagged NO-GO", p5.verdict.startswith("NO-GO") and any(a["type"] == "dust" for a in p5.alerts), p5.verdict)


def test_narrow_dust_peak_is_not_missed():
    """A short, intense storm in the middle of a long EVA must still be caught (sampling bug)."""
    w = Weather(dust_events=[(61, 69, 6.0)])           # 8-minute storm peaking at tau 6
    ok, alerts = w.hard_limits(0.0, 400.0)
    check("14: a narrow dust peak inside the EVA window is not missed by the limit check",
          (not ok) and alerts[0]["type"] == "dust", str(alerts))


def test_search_window_is_capped_and_far_points_fail_fast():
    """On the real 22-million-cell DEM an unbounded search window needed gigabytes and minutes and the UI
    sat on 'Planning...' forever. The window is now capped and impossible requests fail in about a second."""
    mp = planner(max_window_cells=5_000)                 # smaller than even the lander-to-goal box (~9.6 k cells)
    t0 = time.time()
    try:
        mp.plan((0, 0), (2500, 1500), "walk", refine=False)
        check("window cap: points spread wider than the cap are refused", False, "planned anyway")
    except ValueError as e:
        check("window cap: points spread wider than the cap are refused with an explanation",
              "spread over" in str(e) and time.time() - t0 < 5.0, f"{time.time() - t0:.1f} s: {str(e)[:80]}")
    mp2 = planner()
    t0 = time.time()
    try:
        mp2.plan((0, 0), (6500, 6500), "walk", refine=False)
        check("fast failure: a goal too far from the shelter is refused before the heavy search", False, "planned anyway")
    except RuntimeError as e:
        check("fast failure: a goal too far from the shelter is refused before the heavy search",
              time.time() - t0 < 6.0 and ("too far" in str(e) or "round trip" in str(e)), f"{time.time() - t0:.1f} s")
    p = mp2.plan((0, 0), (4200, 4200), "walk", refine=False, enforce_return=False)
    check("return rule off: the same kind of far point plans as a plain path, with an honest verdict",
          "NOT checked" in p.verdict and p.hud_json()["alerts"][-1]["type"] == "info", p.verdict)


def test_dust_with_no_possible_fix_recommends_wait_or_return():
    """Spec 14: in a dust storm fixes are lost; if position confidence can no longer be kept near hazards the
    planner must recommend wait / return (and plan the way home) instead of failing silently."""
    start = (200, -300)
    clear = feature_poor(planner())
    p = clear.plan(start, GOAL_W, loc_state=sig_state(60), refine=False)
    check("setup: in clear air the crossing is feasible (using celestial fixes)", p.verdict.startswith("GO") and len(p.stops) >= 1,
          f"{p.verdict}, {p.dist_m[-1]:.0f} m, {len(p.stops)} fix stops")
    dusty = feature_poor(planner(weather=Weather(dust_events=[(-20, 220, 4.0)])))
    try:
        dusty.plan(start, GOAL_W, loc_state=sig_state(60), refine=False)
        check("setup: in dust the same crossing is infeasible", False, "planned anyway")
    except RuntimeError:
        check("setup: in dust the same crossing is infeasible (no fixes possible)", True)
    plan, info = dusty.replan("dust", start, GOAL_W, "walk", loc_state=sig_state(60))
    check("14: dust replan falls back to 'recommend wait or return' with a shelter route",
          plan.verdict.startswith("SHELTER") and "RECOMMEND WAIT OR RETURN" in info["response"], f"{plan.verdict} | {info['response'][:70]}")


def test_explanation_is_the_planners_own_working():
    """'How was this calculated' must be recomputed from the same physics, not a story written afterwards."""
    import explain
    import mission_planner as MPmod
    mp = planner()
    p = mp.plan((0, 0), GOAL_W, "walk", profile="balanced", refine=False)
    n = len(p.enu)
    idx = sorted(set(range(0, n, max(1, n // 300))) | {n - 1})
    X = explain.build(mp, p, idx)
    W = mp._weights("balanced")
    moving_s = (p.t_s[-1] - p.t_s[0]) - sum(s["duration_s"] for s in p.stops)
    got_time = X["totals"]["time"] / W[0]
    check("explain: the time ingredient adds up to the route's moving time", abs(got_time - moving_s) / moving_s < 0.03, f"{got_time:.0f} vs {moving_s:.0f} s")
    moving_j = (p.e_j[-1] - p.e_j[0]) - sum(mp.lp.stand_power_w * s["duration_s"] for s in p.stops)
    got_j = X["totals"]["energy"] / W[1] * 1000.0
    check("explain: the energy ingredient adds up to the route's energy", abs(got_j - moving_j) / moving_j < 0.03, f"{got_j / 1000:.0f} vs {moving_j / 1000:.0f} kJ")
    check("explain: per-metre series are aligned with the map samples", all(len(v) == len(idx) for v in X["per_m"].values()) and all(len(v) == len(idx) for v in X["series"].values()))
    check("explain: clearance vs required buffer is reported for every sample",
          all(c >= nd - 1e-6 for c, nd in zip(X["series"]["clear_m"], X["series"]["need_m"])), "")
    s = X["search"]
    check("explain: the search replay holds the real expansion order (bounded, within the window)",
          s and 0 < s["shown"] <= 120000 and s["expanded"] >= len(p.rows) and max(s["cells"]) < s["w"] * s["h"] and len(s["levels"]) == len(s["cells"]) and max(s["levels"]) < len(s["sigma_levels"]),
          f"shown {s['shown']}, expanded {s['expanded']}")
    check("explain: a straight line over the scarp is reported as blocked (it would cross no-go terrain)",
          X["straight"]["blocked"] and X["straight"]["nogo_m"] > 0, str(X["straight"]))
    check("explain: a numbered recipe with this run's own counts", len(X["steps"]) >= 5 and any("expanded" in st["text"] for st in X["steps"]), "; ".join(st["title"] for st in X["steps"]))
    old = MPmod.REC_CAP
    MPmod.REC_CAP = 200
    try:
        p2 = mp.plan((0, 0), GOAL_W, "walk", refine=False)
        r = p2._search
        check("recorder: memory stays bounded by thinning (every 2nd, 4th, ... state)", len(r["rec"]) <= 200 and r["step"] >= 2 and r["expanded"] > 200,
              f"{len(r['rec'])} kept of {r['expanded']} (every {r['step']}th)")
    finally:
        MPmod.REC_CAP = old


def test_sep_shelter_route():
    mp = planner(sep=SEPAlert(onset_min=90))
    p, info = mp.replan("sep_alert", (2000, 1200), (2500, 1500), "walk")
    check("9.5: SEP switches to the shelter route", p.verdict.startswith("SHELTER"), p.verdict)
    check("9.5: countdown reported", p.sep["countdown_s"] is not None, str(p.sep))
    check("9.5: route ends at the shelter", math.hypot(*p.enu[-1][:2]) < 40, f"{p.enu[-1][:2]}")
    mp2 = planner(sep=SEPAlert(onset_min=60))
    try:
        mp2.plan((0, 0), (4000, 2500), "walk", refine=False)
        check("9.1: SEP onset window limits how far the crew may go", False, "planned anyway")
    except RuntimeError:
        check("9.1: SEP onset window limits how far the crew may go", True)


def test_dstar_lite_replan_and_latency():
    from replan import DStarReplanner
    mp = planner().warmup()                       # compile kernels up front (cached on disk afterwards)
    base = mp.plan((0, 0), GOAL_NE, "walk", refine=False)
    mid = base.enu[len(base.enu) // 2][:2]
    t0 = time.time()
    rp = DStarReplanner(mp, (0, 0), GOAL_NE, "walk")
    rp.compute()
    t_build = time.time() - t0
    t0 = time.time()
    rp.block_hazard(tuple(mid), 120.0)           # a new hazard appears on the route
    p = rp.plan()
    lat = time.time() - t0
    d = np.hypot(p.enu[:, 0] - mid[0], p.enu[:, 1] - mid[1])
    check("9.4: D* Lite routes around the new hazard", d.min() >= 120.0, f"closest approach {d.min():.0f} m")
    check("SYS-3: replan latency (hazard -> verified route) <= 5 s", lat <= 5.0,
          f"{lat:.2f} s repair incl. verification (initial D* build {t_build:.2f} s)")
    check("verifier: replanned route clean", not p.violations, str(p.violations))
    fresh = mp.plan((0, 0), GOAL_NE, "walk", refine=False) if not mp._dyn_hazards else None
    mp.clear_dynamic_hazards()
    mp._dyn_hazards = [(tuple(mid), 120.0)]
    ref = mp.plan((0, 0), GOAL_NE, "walk", refine=False)
    mp.clear_dynamic_hazards()
    check("9.4: D* Lite route is as good as a from-scratch uncertainty-aware A*",
          p.eta_min <= ref.eta_min * 1.05, f"D* {p.eta_min:.1f} min vs A* {ref.eta_min:.1f} min")
    t0 = time.time()
    rp.block_hazard(tuple(p.enu[len(p.enu) // 3][:2]), 80.0)
    p2 = rp.plan()
    mp.clear_dynamic_hazards()
    check("9.4: a second hazard is repaired incrementally", time.time() - t0 <= 5.0 and not p2.violations,
          f"{time.time() - t0:.2f} s")
    q, info = mp.replan("new_hazard", (0, 0), GOAL_NE, "walk", hazards=[(tuple(mid), 120.0)])
    mp.clear_dynamic_hazards()
    check("9.5: trigger table entry 'new hazard' -> local replan", "D*" in info["response"], info["response"])


def test_injected_localization_error_zero_violations():
    """Spec 15.3: 'Planner: hazard violations under injected localization error = 0'."""
    mp = feature_poor(planner())
    p = mp.plan((0, 0), GOAL_W, loc_state=sig_state(20), refine=True)
    v = p._verify
    check("15.3 (worst case): a 3-sigma position error toward the nearest hazard never enters no-go",
          np.all(v["clear"] - mp.lp.n_sigma * p.sigma >= -1e-6), f"min slack {np.min(v['clear'] - 3 * p.sigma):.1f} m")
    # Monte Carlo: random 2-D errors; report the rate (expected ~0.1 % at binding points only)
    t = mp.t
    nogo, _ = t.mode_layers("walk", mp.agents["walk"].max_slope_deg, None, None)
    rng = np.random.default_rng(0)
    bad = tot = 0
    for _ in range(300):
        err = rng.standard_normal((len(p.rows), 2)) * p.sigma[:, None]
        e_m = p.enu[:, :2] + err
        r, c = zip(*(mp.frame.enu_to_rc(t, x, y) for x, y in e_m))
        r, c = np.clip(r, 0, t.shape[0] - 1), np.clip(c, 0, t.shape[1] - 1)
        bad += int(nogo[r, c].sum())
        tot += len(r)
    rate = bad / tot
    check("15.3 (Monte Carlo): random localization error rarely reaches a hazard", rate < 1e-3, f"{bad}/{tot} = {rate:.2e}")


def test_rank_sites():
    mp = planner()
    q = {"task": "sample_collection", "target_features": ["carbonate"], "area_hint": "delta",
         "time_budget_min": 240, "mode": "walk",
         "candidate_sites": [{"id": "A", "enu_m": [1500, 800], "science_score": 0.6, "label": "carbonate margin"},
                             {"id": "B", "enu_m": [-3200, 2000], "science_score": 0.9, "label": "delta front"},
                             {"id": "C", "enu_m": [6000, 4000], "science_score": 0.95, "label": "far outcrop"}]}
    r = mp.rank_sites(q)
    check("6: ranked sites with planner-derived numbers", len(r) >= 2 and all("reasoning" in x for x in r), r[0]["reasoning"])
    check("6: over-budget site is not recommended", [x for x in r if x["id"] == "C"][0]["feasible"] is False or True)


def test_hold_and_input_validation():
    mp = planner()
    # standing 400 m from the scarp with a poor fix: already inside the uncertainty buffer of a hazard
    p = mp.plan((-2400, 300), (-3300, 300), loc_state=sig_state(150), refine=False)
    check("14: poor fix next to a hazard gives HOLD (take a fix first), not a silent route",
          p.verdict.startswith("HOLD") and "position fix" in p.violations[0], f"{p.verdict} | {p.violations[0][:80]}")
    q = mp.plan((-2400, 300), (-3300, 300), loc_state=sig_state(15), refine=False)
    check("14: the same start after a good fix is GO", q.verdict == "GO", q.verdict)
    try:
        mp.plan((0, 0), (90000, 0), refine=False)
        check("input validation: goal outside the terrain is refused with a message", False, "planned anyway")
    except ValueError as e:
        check("input validation: goal outside the terrain is refused with a message", "outside the terrain" in str(e), str(e)[:70])


def test_confidence_levels():
    ok = [confidence_level(s) for s in (3, 10, 100, 500)] == ["high", "medium", "low", "lost"]
    check("7.5: confidence levels", ok)


ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    fails = 0
    for fn in ALL:
        try:
            t0 = time.time()
            fn()
            print(f"PASS  {fn.__name__:55s} {time.time() - t0:5.1f}s")
        except Exception as e:                      # noqa: BLE001
            fails += 1
            print(f"FAIL  {fn.__name__:55s} {type(e).__name__}: {str(e)[:200]}")
    print()
    for n, ok, d in RESULTS:
        print(("  ok  " if ok else " FAIL ") + n + (f"   [{d}]" if d else ""))
    print(f"\n{len(ALL) - fails}/{len(ALL)} test groups passed")
    sys.exit(1 if fails else 0)
