"""
mission_web.py - web backend for the spec-compliant mission planner UI (mission_ui.html).

    GET  /mission/meta             map extent in Site ENU, presets, landmarks, defaults
    GET  /mission/layer/<name>     base | nogo_walk | nogo_rover | localizability | slope  (PNG / JPEG)
    GET  /mission/probe?x=&y=      ground readout under the cursor
    POST /mission/plan             plan a route            (spec 13 'Planner -> HUD' + drawing data)
    POST /mission/replan           replan on a trigger     (spec 9.5)
    POST /mission/rank             rank candidate sites    (spec 6 / 13)

Everything is in Site ENU metres east / north of the lander (the single frame the spec requires).
"""
import io
import math
import os
import threading

import numpy as np
from PIL import Image

ELEV_STOPS = [(70, 52, 38), (120, 86, 58), (176, 132, 84), (214, 184, 132), (240, 226, 190)]


def _ramp(v, stops):
    stops = np.array(stops, dtype=np.float32)
    x = np.clip(v, 0, 1) * (len(stops) - 1)
    i = np.minimum(x.astype(int), len(stops) - 2)
    f = (x - i)[..., None]
    return stops[i] * (1 - f) + stops[i + 1] * f


def _down_max(a, step):
    """Block-maximum down-sampling, so one-cell-wide cliffs survive."""
    H, W = a.shape
    h, w = -(-H // step), -(-W // step)
    pad = np.zeros((h * step, w * step), a.dtype)
    pad[:H, :W] = a
    return pad.reshape(h, step, w, step).max(axis=(1, 3))


def _png(rgba):
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", compress_level=4)
    return buf.getvalue()


class MissionWeb:
    def __init__(self, terrain_fn, synthetic=False, max_px=5200):
        self.terrain_fn = terrain_fn
        self.synthetic = synthetic
        self.max_px = max_px
        self._mp = None
        self._layers = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------ planner (lazy)
    @property
    def mp(self):
        with self._lock:
            if self._mp is None:
                from mission_planner import MissionPlanner
                print("[mission] building localizability layer + warming up kernels (first run only is slow) ...")
                self._mp = MissionPlanner(self.terrain_fn(25.0)).warmup()
                print("[mission] ready")
            return self._mp

    # ------------------------------------------------------------ meta / presets
    def _presets(self):
        mp = self.mp
        if self.synthetic:
            return [
                {"name": "Walk 2.9 km NE", "start": [0, 0], "goal": [2500, 1500], "mode": "walk", "sigma": 5},
                {"name": "Cross the scarp (walk)", "start": [0, 0], "goal": [-3200, 2000], "mode": "walk", "sigma": 5},
                {"name": "Start 400 m from the scarp with a poor fix (sigma 150 m): expect HOLD", "start": [-2400, 300],
                 "goal": [-3300, 300], "mode": "walk", "sigma": 150},
                {"name": "Same start after a good fix (sigma 15 m)", "start": [-2400, 300], "goal": [-3300, 300],
                 "mode": "walk", "sigma": 15},
                {"name": "Rover 10 km", "start": [0, 0], "goal": [7000, 7000], "mode": "rover", "sigma": 5} if mp.t.shape[0] >= 1000
                else {"name": "Rover 5 km south", "start": [0, 0], "goal": [0, -5000], "mode": "rover", "sigma": 5},
                {"name": "Walk 2.9 km in a dust storm", "start": [0, 0], "goal": [2500, 1500], "mode": "walk", "sigma": 5,
                 "dust_tau": 4.0, "dust_peak_min": 35},
                {"name": "SEP alert, 2.3 km from the lander", "start": [2000, 1200], "goal": [2500, 1500], "mode": "walk",
                 "sigma": 5, "sep_alert": 90},
            ]
        from plan import LANDMARKS
        lm = {}
        for k, (lat, lon, desc) in LANDMARKS.items():
            e, n = mp.frame.lonlat_to_enu(lon, lat)
            lm[k] = [round(float(e), 1), round(float(n), 1)]
        return [
            {"name": "Delta front (walk)", "start": [0, 0], "goal": lm["delta_front"], "mode": "walk", "sigma": 5},
            {"name": "Delta top, poor position fix (sigma 60 m)", "start": [0, 0], "goal": lm["delta_top"], "mode": "walk", "sigma": 60},
            {"name": "Upper fan (rover)", "start": [0, 0], "goal": lm["upper_fan"], "mode": "rover", "sigma": 5},
            {"name": "Delta front in a dust storm", "start": [0, 0], "goal": lm["delta_front"], "mode": "walk", "sigma": 5,
             "dust_tau": 4.0, "dust_peak_min": 35},
            {"name": "SEP alert at the delta front", "start": lm["delta_front"], "goal": lm["delta_front"], "mode": "walk",
             "sigma": 5, "sep_alert": 90},
        ]

    def _landmarks(self):
        if self.synthetic:
            return [{"key": "scarp_pass_n", "name": "Northern scarp pass", "enu": [-2800, 2100]},
                    {"key": "west", "name": "Beyond the scarp", "enu": [-3200, 2000]},
                    {"key": "rim", "name": "Crater rim (E)", "enu": [5500, 4200]},
                    {"key": "plain", "name": "Smooth plain (S)", "enu": [0, -5200]}]
        from plan import LANDMARKS
        out = []
        for k, (lat, lon, desc) in LANDMARKS.items():
            e, n = self.mp.frame.lonlat_to_enu(lon, lat)
            out.append({"key": k, "name": desc, "enu": [round(float(e), 1), round(float(n), 1)]})
        return out

    def extent(self):
        t, f = self.mp.t, self.mp.frame
        H, W = t.shape
        e, n = f.rc_to_enu(t, [-0.5, H - 0.5], [-0.5, W - 0.5])
        return [float(e[0]), float(e[1]), float(n[1]), float(n[0])]        # e_min, e_max, n_min, n_max

    def meta(self):
        mp = self.mp
        t = mp.t
        return {
            "extent": [round(v, 1) for v in self.extent()], "res_m": float(t.res), "size": list(t.shape),
            "synthetic": self.synthetic, "shelters": [list(s) for s in mp.shelters_enu],
            "presets": self._presets(), "landmarks": self._landmarks(),
            "profiles": {"min_eta": "Minimum ETA (spec 9.1); risk and localization are tie-breakers",
                         "balanced": "Balance time, energy, risk and localization",
                         "safest": "Weight risk and localization strongly; accepts a longer route"},
            "agents": {k: {"max_slope_deg": a.max_slope_deg} for k, a in mp.agents.items()},
            "loc": {"sigma0_m": mp.lp.sigma0_m, "n_sigma": mp.lp.n_sigma, "fix_stop_s": mp.lp.fix_stop_s,
                    "bounds": [5, 30, 300]},
            "layers": ["nogo_walk", "nogo_rover", "localizability", "slope"],
        }

    # ------------------------------------------------------------ map layers
    def layer(self, name):
        with self._lock:
            if name not in self._layers:
                self._layers[name] = self._render(name)
            return self._layers[name]

    def prewarm(self):
        """Render the (big) base map in the background so the first page load does not wait for it."""
        threading.Thread(target=lambda: self.layer("base"), daemon=True).start()

    def _step(self, shape, minimum=1):
        return max(minimum, math.ceil(max(shape) / self.max_px))

    def _render_base(self, step):
        """Hillshaded, elevation-tinted base map at full resolution, computed in strips so the temporary memory stays small
        (a 22-million-cell map would otherwise need several gigabytes of float arrays)."""
        t = self.mp.t
        zs = t.elev[::step, ::step]
        h, wd = zs.shape
        lo, hi = np.percentile(zs[::4, ::4], [1, 99])
        cell = float(t.res) * step
        azr, altr = math.radians(315.0), math.radians(40.0)
        out = np.empty((h, wd, 3), np.uint8)
        B = 512
        for r0 in range(0, h, B):
            r1 = min(h, r0 + B)
            a, b = max(0, r0 - 1), min(h, r1 + 1)             # one extra row each side so strip edges are seamless
            gy, gx = np.gradient(zs[a:b].astype(np.float32), cell)
            sl = np.pi / 2 - np.arctan(np.hypot(gx, gy))
            asp = np.arctan2(-gx, gy)
            hs = np.clip(np.sin(altr) * np.sin(sl) + np.cos(altr) * np.cos(sl) * np.cos(azr - np.pi / 2 - asp), 0, 1)[r0 - a:r0 - a + (r1 - r0)]
            rgb = _ramp((zs[r0:r1] - lo) / max(hi - lo, 1e-6), ELEV_STOPS) * (0.35 + 0.65 * hs[..., None])
            out[r0:r1] = np.clip(rgb, 0, 255).astype(np.uint8)
        return out

    def _cache_path(self, name, step):
        t = self.mp.t
        if not t.dem_path:
            return None
        stem = os.path.splitext(os.path.basename(t.dem_path))[0]
        return os.path.join(os.path.dirname(os.path.abspath(t.dem_path)), f"mission_layer_{stem}_{name}_s{step}_{t.shape[0]}x{t.shape[1]}.jpg")

    def _render(self, name):
        mp = self.mp
        t = mp.t
        H, W = t.shape
        step = self._step(t.shape)
        if name == "base":
            path = self._cache_path(name, step)
            try:
                if path and os.path.exists(path) and os.path.getmtime(path) >= os.path.getmtime(t.dem_path):
                    with open(path, "rb") as f:
                        return f.read(), "image/jpeg"
            except OSError:
                pass
            buf = io.BytesIO()
            Image.fromarray(self._render_base(step), "RGB").save(buf, "JPEG", quality=88)
            data = buf.getvalue()
            try:
                if path:
                    with open(path, "wb") as f:
                        f.write(data)
            except OSError:
                pass
            return data, "image/jpeg"
        out = np.zeros(_down_max(t.nogo, step).shape + (4,), np.uint8)
        if name in ("nogo_walk", "nogo_rover"):
            mode = name.split("_")[1]
            ag = mp.agents[mode]
            nogo, _ = t.mode_layers(mode, ag.max_slope_deg, ag.rough_max, ag.sand_max)
            m = _down_max(nogo, step)
            out[m] = (230, 60, 60, 165) if mode == "walk" else (240, 150, 40, 165)
        elif name == "slope":
            s = _down_max(t.slope, step) if step > 1 else t.slope
            B = 512                                              # strips: a full-size float colour array needs gigabytes
            for r0 in range(0, s.shape[0], B):
                v = np.clip((s[r0:r0 + B] - 3.0) / 27.0, 0, 1)
                out[r0:r0 + B, :, :3] = _ramp(v, [(250, 230, 80), (240, 130, 40), (200, 30, 40)]).astype(np.uint8)
                out[r0:r0 + B, :, 3] = (v > 0) * (60 + 150 * v)
        elif name == "localizability":
            # derived from data sampled every ~200 m, so a picture finer than ~80 m adds nothing but file size
            L = np.maximum(mp.loc.sky, mp.loc.orb)
            lstep = max(step, 4)
            out = np.zeros(_down_max(t.nogo, lstep).shape + (4,), np.uint8)
            img = Image.fromarray((np.clip(L, 0, 1) * 255).astype(np.uint8), "L").resize(
                (out.shape[1], out.shape[0]), Image.BILINEAR)
            v = np.asarray(img, np.float32) / 255.0
            out[..., :3] = _ramp(v, [(220, 50, 50), (240, 200, 60), (60, 200, 110)]).astype(np.uint8)
            out[..., 3] = 120
        else:
            raise KeyError(name)
        return _png(out), "image/png"

    # ------------------------------------------------------------ probe
    def probe(self, x, y):
        mp = self.mp
        t = mp.t
        r, c = mp.frame.enu_to_rc(t, x, y)
        if not t.in_bounds(r, c):
            return {"out": True}
        wk, rv = mp.agents["walk"], mp.agents["rover"]
        ng_w, hz_w = t.mode_layers("walk", wk.max_slope_deg, wk.rough_max, wk.sand_max)
        ng_r, _ = t.mode_layers("rover", rv.max_slope_deg, rv.rough_max, rv.sand_max)
        S, O, OP = mp.loc.sample([r], [c])
        return {"elev": round(float(t.elev[r, c]), 1), "up": round(float(mp.frame.up(t.elev[r, c])), 1),
                "slope": round(float(t.slope[r, c]), 1), "rough": round(float(t.rough[r, c]), 2),
                "sand": round(float(t.sand[r, c]), 2), "hazd": round(min(float(hz_w[r, c]), 9999.0)),
                "nogo_walk": bool(ng_w[r, c]), "nogo_rover": bool(ng_r[r, c]),
                "loc_skyline": round(float(S[0]), 2), "loc_orbital": round(float(O[0]), 2)}

    # ------------------------------------------------------------ planning
    def _configure(self, req):
        from environment import Weather, SEPAlert
        mp = self.mp
        w = dict(req.get("weather") or {})
        w["dust_events"] = [tuple(e) for e in w.get("dust_events", [])]
        mp.weather = Weather(**w)
        mp.sep = SEPAlert(**(req.get("sep") or {}))
        mp._dyn_hazards = [(tuple(h["enu"]), float(h.get("radius_m", 40.0))) for h in req.get("hazards", [])]

    @staticmethod
    def _loc(req):
        from localization import LocalizationState
        return LocalizationState.from_dict(req["localization"]) if req.get("localization") else None

    def _payload(self, p, max_pts=2200):
        """Planner -> HUD message plus the dense per-point arrays the map needs to draw the route."""
        d = p.to_json()
        n = len(p.enu)
        stride = max(1, math.ceil(n / max_pts))
        idx = set(range(0, n, stride)) | {n - 1, p.turn_back_idx} | {s["idx"] for s in p.stops}
        idx = sorted(idx)
        diff = ["easy"] * n
        wi = p.waypoint_idx
        for k, (a, b) in enumerate(zip(wi[:-1], wi[1:])):
            for i in range(a, b + 1):
                diff[i] = p.segment_difficulty[k]
        d["route"] = {"enu": [[round(float(p.enu[i][0]), 1), round(float(p.enu[i][1]), 1)] for i in idx],
                      "sigma": [round(float(p.sigma[i]), 2) for i in idx],
                      "dist_m": [round(float(p.dist_m[i]), 1) for i in idx],
                      "margin": [round(float(p.return_margin[i]), 3) for i in idx],
                      "t_s": [round(float(p.t_s[i]), 1) for i in idx],
                      "e_j": [round(float(p.e_j[i]), 0) for i in idx],
                      "difficulty": [diff[i] for i in idx]}
        d["fix_stop_detail"] = [{"enu": [round(float(p.enu[s["idx"]][0]), 1), round(float(p.enu[s["idx"]][1]), 1)],
                                 "method": s["method"], "duration_s": s["duration_s"],
                                 "dist_m": round(float(p.dist_m[s["idx"]]), 1),
                                 "sigma_before": round(float(p.sigma[s["idx"] - 1]), 1),
                                 "sigma_after": round(float(p.sigma[s["idx"]]), 1)} for s in p.stops]
        d["start_enu"] = [round(float(p.enu[0][0]), 1), round(float(p.enu[0][1]), 1)]
        d["goal_enu"] = [round(float(p.enu[-1][0]), 1), round(float(p.enu[-1][1]), 1)]
        rt = p._verify["retT"][-1] if "_verify" in p.__dict__ else 1e12
        d["return_min"] = round(float(rt) / 60.0, 1) if (rt < 1e11 and d.get("return_checked", True)) else None
        d["on_site_min"] = round(self.mp.cons.on_site_s / 60.0, 1)
        d["shelters"] = [list(s) for s in self.mp.shelters_enu]
        try:                                                  # 'how was this calculated?' (explain.py); never allowed to break a plan
            import explain
            d["explain"] = explain.build(self.mp, p, idx)
        except Exception as e:                                # noqa: BLE001
            print(f"[explain] failed: {type(e).__name__}: {e}")
            d["explain"] = None
        return d

    def plan(self, req):
        with self._lock:
            mp = self.mp
            self._configure(req)
            try:
                p = mp.plan(tuple(req.get("start_enu", (0.0, 0.0))), tuple(req["goal_enu"]), req.get("mode", "walk"),
                            req.get("profile", "min_eta"), self._loc(req), refine=bool(req.get("refine", True)),
                            t_elapsed_s=float(req.get("t_elapsed_s", 0.0)), e_used_j=float(req.get("e_used_j", 0.0)),
                            enforce_return=bool(req.get("enforce_return", True)))
                return self._payload(p)
            finally:
                mp._dyn_hazards = []

    def replan(self, req):
        with self._lock:
            mp = self.mp
            self._configure(req)
            haz = [(tuple(h["enu"]), float(h.get("radius_m", 40.0))) for h in req.get("hazards", [])]
            mp._dyn_hazards = []                     # the replanner registers the hazards itself
            try:
                p, info = mp.replan(req["trigger"], tuple(req["current_enu"]),
                                    tuple(req.get("goal_enu", req["current_enu"])), req.get("mode", "walk"),
                                    req.get("profile", "min_eta"), self._loc(req),
                                    float(req.get("t_elapsed_s", 0.0)), float(req.get("e_used_j", 0.0)), haz,
                                    enforce_return=bool(req.get("enforce_return", True)))
                info.pop("replanner", None)
                return {"plan": self._payload(p), "info": info}
            finally:
                mp._dyn_hazards = []

    def rank(self, req):
        with self._lock:
            self._configure(req)
            try:
                return {"sites": self.mp.rank_sites(req, tuple(req.get("start_enu", (0.0, 0.0))), self._loc(req))}
            finally:
                self.mp._dyn_hazards = []

    # ------------------------------------------------------------ AI layer (spec Section 6)
    @property
    def ai(self):
        with self._lock:
            if getattr(self, "_ai", None) is None:
                import ai_layer
                cat = ai_layer.Catalogue.synthetic() if self.synthetic else ai_layer.Catalogue.real(self.mp.frame)
                self._ai = ai_layer.AILayer(cat)
            return self._ai

    def ai_status(self):
        return self.ai.status()

    def ai_interpret(self, req):
        return self.ai.interpret(str(req.get("text", "")), req.get("context") or {})

    def ai_sites(self, req):
        """Candidate sites for a structured query, ranked with coarse planner runs (spec 6 steps 2-4).
        Every number in the answer comes from the planner; the AI layer only picked the candidates."""
        import ai_layer
        with self._lock:
            mp = self.mp
            self._configure(req)
            try:
                ai = self.ai
                q, warns = ai_layer.validate_query(req.get("query"), set(ai.cat.features), set(ai.cat.areas))
                budget = q["time_budget_min"] or ai_layer.DEFAULT_BUDGET_MIN
                start = tuple(req.get("start_enu", (0.0, 0.0)))
                loc = self._loc(req)
                notes = list(warns)
                if q["task"] == "return_to_shelter":
                    cands = [{"site": {"id": "lander", "name": "Lander (shelter)", "area": "lander", "unit": "the lander is the shelter",
                                       "features": {}, "enu": [float(mp.shelters_enu[0][0]), float(mp.shelters_enu[0][1])]},
                              "science_score": 1.0, "why": "the lander is the shelter"}]
                else:
                    cands = ai.cat.match(q, limit=6)
                raw_feats = (req.get("query") or {}).get("target_features") or []
                if raw_feats and not q["target_features"]:
                    cands = []                                  # every requested target was unknown: do NOT fall back to "anything"
                if not cands:
                    return {"query": q, "mode_used": q["mode"] or "walk", "budget_min": budget, "sites": [], "unreachable": [],
                            "notes": notes + ["No known science site matches that request."],
                            "summary": "No known science site matches that request. Try another target or area."}
                by_id = {c["site"]["id"]: c for c in cands}
                modes = [q["mode"]] if q["mode"] else ["walk", "rover"]
                rows, used = [], modes[0]
                for m in modes:
                    rq = {"mode": m, "time_budget_min": budget, "candidate_sites": [
                        {"id": c["site"]["id"], "enu_m": c["site"]["enu"], "science_score": c["science_score"],
                         "label": c["site"]["name"]} for c in cands]}
                    rows = mp.rank_sites(rq, start, loc, top_n=len(cands))
                    used = m
                    if any(r["feasible"] for r in rows):
                        break
                    if q["mode"] is None and m == "walk":
                        notes.append("Nothing is reachable on foot within the time budget and safety margins; checked the rover.")
                if q["mode"] is None and used == "rover" and any(r["feasible"] for r in rows):
                    notes.append("These are reachable by ROVER, not on foot.")
                # best-matching sites that are out of reach ON FOOT may still be reachable by rover: tell the crew
                rover_opt = {}
                if q["mode"] is None and used == "walk" and any(not r["feasible"] for r in rows):
                    miss = [c for c in cands if c["site"]["id"] in {r["id"] for r in rows if not r["feasible"]}]
                    rr = mp.rank_sites({"mode": "rover", "time_budget_min": budget, "candidate_sites": [
                        {"id": c["site"]["id"], "enu_m": c["site"]["enu"], "science_score": c["science_score"],
                         "label": c["site"]["name"]} for c in miss]}, start, loc, top_n=len(miss))
                    rover_opt = {r["id"]: r for r in rr if r["feasible"]}
                    if rover_opt:
                        best_id = max(rover_opt, key=lambda i: by_id[i]["science_score"])
                        if by_id[best_id]["science_score"] > max([by_id[r["id"]]["science_score"] for r in rows if r["feasible"]] or [0]):
                            notes.append(f"{len(rover_opt)} better-matching site(s) are out of reach on foot but reachable by ROVER "
                                         f"(for example {by_id[best_id]['site']['name']}). Say 'use the rover' to see them.")
                out, bad = [], []
                for r in rows:
                    c = by_id[r["id"]]
                    s = c["site"]
                    tail = r.get("reasoning", "")
                    tail = tail.split(";", 1)[1].strip() if ";" in tail else tail
                    item = {"id": s["id"], "name": s["name"], "enu": s["enu"], "area": s["area"], "why": c["why"],
                            "science_score": c["science_score"], "features": s["features"],
                            "reasoning": f"{s['name']}: {c['why']}. {tail}" if tail else f"{s['name']}: {c['why']}."}
                    for k in ("score", "eta_min", "round_trip_min", "distance_km", "risk_score", "localizability",
                              "hard_sections", "fix_stops", "verdict"):
                        v = r.get(k)
                        item[k] = float(v) if hasattr(v, "item") else v
                    if r["id"] in rover_opt:
                        ro = rover_opt[r["id"]]
                        item["rover_option"] = {k2: float(ro.get(k2)) for k2 in ("eta_min", "round_trip_min", "distance_km")}
                    (out if r["feasible"] else bad).append(item)
                out.sort(key=lambda x: -x["score"])
                for i, it in enumerate(out[:3], 1):
                    it["rank"] = i
                if out:
                    b = out[0]
                    summary = (f"{min(len(out), 3)} candidate site{'s' if len(out) > 1 else ''} "
                               f"{'by rover' if used == 'rover' else 'on foot'} within {budget} min. "
                               f"Best: #1 {b['name']} ({b['distance_km']} km, ETA {b['eta_min']:.0f} min).")
                else:
                    summary = (f"No candidate is reachable within {budget} min with the safety margins "
                               f"({'by rover' if used == 'rover' else 'on foot'}). Try a longer budget or a different target"
                               f"{'' if used == 'rover' else ', or the rover'}.")
                return {"query": q, "mode_used": used, "budget_min": budget, "sites": out[:3], "unreachable": bad[:4],
                        "notes": notes, "summary": summary}
            finally:
                mp._dyn_hazards = []

    def compare(self, req):
        """The same trip planned with each priority profile (coarse, no refinement), to show how the weights change the route."""
        with self._lock:
            mp = self.mp
            self._configure(req)
            try:
                out = []
                for prof in ("min_eta", "balanced", "safest"):
                    try:
                        p = mp.plan(tuple(req.get("start_enu", (0.0, 0.0))), tuple(req["goal_enu"]), req.get("mode", "walk"), prof,
                                    self._loc(req), refine=False, t_elapsed_s=float(req.get("t_elapsed_s", 0.0)),
                                    e_used_j=float(req.get("e_used_j", 0.0)), enforce_return=bool(req.get("enforce_return", True)))
                        n = len(p.enu)
                        stride = max(1, math.ceil(n / 500))
                        idx = sorted(set(range(0, n, stride)) | {n - 1})
                        out.append({"profile": prof, "ok": True, "distance_km": round(float(p.dist_m[-1]) / 1000, 2),
                                    "eta_min": round(p.eta_min, 1), "energy_kJ": round(p.energy_kj, 0),
                                    "risk_score": round(p.risk_score, 3), "fix_stops": len(p.stops),
                                    "min_clearance_m": round(float(p._verify["clear"].min()), 0), "verdict": p.verdict,
                                    "route": [[round(float(p.enu[i][0]), 1), round(float(p.enu[i][1]), 1)] for i in idx]})
                    except Exception as e:                    # noqa: BLE001
                        out.append({"profile": prof, "ok": False, "error": str(e)[:200]})
                return {"alternatives": out}
            finally:
                mp._dyn_hazards = []
