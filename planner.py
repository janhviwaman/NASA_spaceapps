"""
planner.py - safest-path search for an astronaut on foot in Jezero crater.

Method (no reinforcement learning, no training):
  * The terrain is a graph: every 20 m DEM cell is a node, joined to its 16 neighbours
    (8 adjacent cells plus the 8 "knight's move" cells, which gives smoother headings).
  * Every edge gets a physically motivated, direction-dependent cost made of
      - travel time   : Tobler hiking function, slowed by a suit factor
      - metabolic work: Minetti et al. (2002) cost-of-walking polynomial vs. gradient,
                        scaled to Mars gravity
      - risk          : slope, steep downhill (fall risk), side-slope (balance),
                        surface roughness, and closeness to cliffs / no-go terrain
  * Hard EVA limits remove edges entirely (slope over the limit, cliffs, missing data).
  * A* search with an admissible straight-line heuristic finds the minimum-cost route.

Profiles change the weights between time, energy and risk.
"""
import math
from dataclasses import dataclass, asdict

import numpy as np
from numba import njit

G_MARS = 3.721
G_EARTH = 9.81

PROFILES = {
    #             time  energy  risk
    "safest":   (0.5,   0.5,    4.0),
    "balanced": (1.0,   1.0,    1.0),
    "fastest":  (2.0,   0.5,    0.15),
}

# 16-connected move set
MOVES = np.array([(-1, 0), (1, 0), (0, -1), (0, 1),
                  (-1, -1), (-1, 1), (1, -1), (1, 1),
                  (-2, -1), (-2, 1), (2, -1), (2, 1),
                  (-1, -2), (1, -2), (-1, 2), (1, 2)], dtype=np.int64)


@dataclass
class Astronaut:
    body_mass_kg: float = 80.0
    suit_mass_kg: float = 60.0          # a future Mars surface suit + pack
    suit_speed_factor: float = 0.55     # pressurised-suit walking vs. shirt-sleeve hiking
    max_slope_deg: float = 25.0         # hard limit on any grade walked
    soft_slope_deg: float = 15.0        # slopes above this are penalised hard
    eva_hours: float = 8.0              # suit life-support budget
    reserve_frac: float = 0.3           # keep 30 % of the EVA in reserve


# ------------------------------------------------------------------ physics
@njit(cache=True)
def minetti(i):
    """Metabolic cost of walking (J / kg / m) on gradient i (rise/run), Minetti 2002."""
    if i > 0.45:
        i = 0.45
    if i < -0.45:
        i = -0.45
    return (280.5 * i**5 - 58.7 * i**4 - 76.8 * i**3 + 51.9 * i**2 + 19.6 * i + 2.5)


@njit(cache=True)
def tobler_kmh(i):
    return 6.0 * math.exp(-3.5 * abs(i + 0.05))


@njit(cache=True)
def edge_terms(d, dz, s0, s1, r0, r1, h0, h1, soft):
    """The individual ingredients of one step's cost (all per metre, unweighted).
    Returns (t_norm, e_norm, r_slope, r_down, r_side, r_rough, r_prox, grade_deg)."""
    gi = dz / d
    s = 0.5 * (s0 + s1)
    rgh = 0.5 * (r0 + r1)
    hz = min(h0, h1)
    t_norm = tobler_kmh(0.0) / tobler_kmh(gi)      # 1.0 = flat-ground walking time
    e_norm = minetti(gi) / 2.5                     # 1.0 = flat-ground energy
    st = math.tan(math.radians(s))
    cross = math.degrees(math.atan(math.sqrt(max(st * st - gi * gi, 0.0))))
    r_slope = (s / soft) ** 3                      # steep ground (1.0 at the soft limit)
    down = -math.degrees(math.atan(gi)) - 8.0
    r_down = (down / 7.0) ** 2 if down > 0 else 0.0  # descending steeper than 8 deg
    r_side = (cross / 12.0) ** 2                   # walking across a slope
    r_rough = (rgh / 1.5) ** 2                     # bumpy / blocky ground
    r_prox = 2.0 * math.exp(-hz / 80.0)            # close to a cliff / no-go cell
    return (t_norm, e_norm, r_slope, r_down, r_side, r_rough, r_prox,
            math.degrees(math.atan(gi)))


@njit(cache=True)
def edge_cost(d, dz, s0, s1, r0, r1, h0, h1, wt, we, wr, soft, maxs):
    """Cost of one move. Returns -1 when the move is not allowed."""
    if math.degrees(math.atan(abs(dz / d))) > maxs:
        return -1.0
    tn, en, a, b, c, e, f, g = edge_terms(d, dz, s0, s1, r0, r1, h0, h1, soft)
    return d * (wt * tn + we * en + wr * (a + b + c + e + f))


# ------------------------------------------------------------------ A* search
@njit(cache=True)
def _heap_push(hk, hn, pos, size, key, node):
    i = size
    hk[i] = key
    hn[i] = node
    pos[node] = i
    while i > 0:
        p = (i - 1) >> 1
        if hk[p] <= hk[i]:
            break
        hk[p], hk[i] = hk[i], hk[p]
        hn[p], hn[i] = hn[i], hn[p]
        pos[hn[p]] = p
        pos[hn[i]] = i
        i = p
    return size + 1


@njit(cache=True)
def _heap_decrease(hk, hn, pos, key, node):
    i = pos[node]
    hk[i] = key
    while i > 0:
        p = (i - 1) >> 1
        if hk[p] <= hk[i]:
            break
        hk[p], hk[i] = hk[i], hk[p]
        hn[p], hn[i] = hn[i], hn[p]
        pos[hn[p]] = p
        pos[hn[i]] = i
        i = p


@njit(cache=True)
def _heap_pop(hk, hn, pos, size):
    node = hn[0]
    pos[node] = -2  # closed
    size -= 1
    if size > 0:
        hk[0] = hk[size]
        hn[0] = hn[size]
        pos[hn[0]] = 0
        i = 0
        while True:
            l = 2 * i + 1
            r = l + 1
            m = i
            if l < size and hk[l] < hk[m]:
                m = l
            if r < size and hk[r] < hk[m]:
                m = r
            if m == i:
                break
            hk[m], hk[i] = hk[i], hk[m]
            hn[m], hn[i] = hn[i], hn[m]
            pos[hn[m]] = m
            pos[hn[i]] = i
            i = m
    return node, size


@njit(cache=True)
def astar(elev, slope, rough, hazd, nogo, res, sr, sc, gr, gc,
          wt, we, wr, soft, maxs, hmin, moves):
    H, W = elev.shape
    N = H * W
    g = np.full(N, np.inf)
    parent = np.full(N, -1, dtype=np.int64)
    pos = np.full(N, -1, dtype=np.int64)          # -1 unseen, -2 closed, >=0 heap index
    hk = np.empty(N, dtype=np.float64)
    hn = np.empty(N, dtype=np.int64)
    s = sr * W + sc
    goal = gr * W + gc
    order = np.empty(N, dtype=np.int64)  # cells in the order A* expanded them
    g[s] = 0.0
    size = _heap_push(hk, hn, pos, 0, 0.0, s)
    expanded = 0
    while size > 0:
        u, size = _heap_pop(hk, hn, pos, size)
        order[expanded] = u
        expanded += 1
        if u == goal:
            break
        ur = u // W
        uc = u - ur * W
        for k in range(moves.shape[0]):
            dr = moves[k, 0]
            dc = moves[k, 1]
            vr = ur + dr
            vc = uc + dc
            if vr < 0 or vr >= H or vc < 0 or vc >= W:
                continue
            if nogo[vr, vc]:
                continue
            # knight moves sweep across two cells: both must be passable
            if abs(dr) + abs(dc) == 3:
                if abs(dr) == 2:
                    if nogo[ur + dr // 2, uc] or nogo[ur + dr // 2, vc]:
                        continue
                else:
                    if nogo[ur, uc + dc // 2] or nogo[vr, uc + dc // 2]:
                        continue
            v = vr * W + vc
            if pos[v] == -2:
                continue
            d = res * math.sqrt(dr * dr + dc * dc)
            c = edge_cost(d, elev[vr, vc] - elev[ur, uc],
                          slope[ur, uc], slope[vr, vc],
                          rough[ur, uc], rough[vr, vc],
                          hazd[ur, uc], hazd[vr, vc],
                          wt, we, wr, soft, maxs)
            if c < 0:
                continue
            ng = g[u] + c
            if ng < g[v]:
                g[v] = ng
                parent[v] = u
                hh = hmin * res * math.sqrt((vr - gr) ** 2 + (vc - gc) ** 2)
                if pos[v] >= 0:
                    _heap_decrease(hk, hn, pos, ng + hh, v)
                else:
                    size = _heap_push(hk, hn, pos, size, ng + hh, v)
    if parent[goal] == -1 and goal != s:
        return np.empty(0, dtype=np.int64), order[:expanded]
    path = [goal]
    while path[-1] != s:
        path.append(parent[path[-1]])
    out = np.empty(len(path), dtype=np.int64)
    for i in range(len(path)):
        out[i] = path[len(path) - 1 - i]
    return out, order[:expanded]


def heuristic_factor(wt, we):
    """Smallest possible cost per metre (risk >= 0), so A* stays optimal."""
    gs = np.linspace(-0.47, 0.47, 2001)
    t = np.array([tobler_kmh(0.0) / tobler_kmh(x) for x in gs])
    e = np.array([minetti(x) / 2.5 for x in gs])
    return float(np.min(wt * t + we * e)) * 0.999


@njit(cache=True)
def bilinear(a, r, c):
    H, W = a.shape
    r = min(max(r, 0.0), H - 1.001)
    c = min(max(c, 0.0), W - 1.001)
    i, j = int(r), int(c)
    fr, fc = r - i, c - j
    return (a[i, j] * (1 - fr) * (1 - fc) + a[i + 1, j] * fr * (1 - fc) +
            a[i, j + 1] * (1 - fr) * fc + a[i + 1, j + 1] * fr * fc)


@njit(cache=True)
def poly_cost(rows, cols, elev, slope, rough, hazd, nogo, res, wt, we, wr, soft, maxs):
    """Cost of a polyline given in fractional grid coordinates."""
    tot = 0.0
    for k in range(len(rows) - 1):
        r0, c0, r1, c1 = rows[k], cols[k], rows[k + 1], cols[k + 1]
        d = res * math.sqrt((r1 - r0) ** 2 + (c1 - c0) ** 2)
        if d == 0:
            continue
        i0, j0 = int(round(r0)), int(round(c0))
        i1, j1 = int(round(r1)), int(round(c1))
        if nogo[i0, j0] or nogo[i1, j1]:
            return np.inf
        c = edge_cost(d, bilinear(elev, r1, c1) - bilinear(elev, r0, c0),
                      slope[i0, j0], slope[i1, j1], rough[i0, j0], rough[i1, j1],
                      hazd[i0, j0], hazd[i1, j1], wt, we, wr, soft, maxs)
        if c < 0:
            return np.inf
        tot += c
    return tot


# ------------------------------------------------------------------ public API
class RoutePlanner:
    def __init__(self, terrain, astronaut=None):
        self.t = terrain
        self.a = astronaut or Astronaut()

    def snap_to_safe(self, r, c, radius=25):
        """Move a requested point to the nearest passable cell (start/goal on a cliff)."""
        if not self.t.nogo[r, c]:
            return r, c, 0.0
        r0, r1 = max(0, r - radius), min(self.t.shape[0], r + radius + 1)
        c0, c1 = max(0, c - radius), min(self.t.shape[1], c + radius + 1)
        sub = ~self.t.nogo[r0:r1, c0:c1]
        if not sub.any():
            raise ValueError("Point is inside a large no-go area (cliff / bad data).")
        rr, cc = np.nonzero(sub)
        k = np.argmin((rr + r0 - r) ** 2 + (cc + c0 - c) ** 2)
        nr, nc = int(rr[k] + r0), int(cc[k] + c0)
        return nr, nc, self.t.res * math.hypot(nr - r, nc - c)

    # -------------------------------------------------------------- smoothing
    @staticmethod
    def _line_pts(r0, c0, r1, c1):
        n = max(1, int(math.ceil(math.hypot(r1 - r0, c1 - c0))))
        f = np.linspace(0.0, 1.0, n + 1)
        return r0 + f * (r1 - r0), c0 + f * (c1 - c0)

    def _poly_cost(self, rows, cols, w):
        t, a = self.t, self.a
        return poly_cost(np.asarray(rows, np.float64), np.asarray(cols, np.float64),
                         t.elev, t.slope, t.rough, t.hazdist, t.nogo, float(t.res),
                         w[0], w[1], w[2], a.soft_slope_deg, a.max_slope_deg)

    def smooth(self, rows, cols, profile, max_skip=80):
        """String-pulling: replace zig-zags with straight legs, but only where the
        straight leg crosses no hazard and costs no more than the grid path it replaces.
        Returns the route as fractional grid coordinates sampled every ~1 cell."""
        w = PROFILES[profile]
        rows = rows.astype(np.float64)
        cols = cols.astype(np.float64)
        out_r, out_c = [rows[0]], [cols[0]]
        i, n = 0, len(rows)
        while i < n - 1:
            best = i + 1
            for j in range(min(n - 1, i + max_skip), i + 1, -1):
                lr, lc = self._line_pts(rows[i], cols[i], rows[j], cols[j])
                if self._poly_cost(lr, lc, w) <= self._poly_cost(rows[i:j + 1], cols[i:j + 1], w):
                    best = j
                    break
            lr, lc = self._line_pts(rows[i], cols[i], rows[best], cols[best])
            out_r.extend(lr[1:].tolist())
            out_c.extend(lc[1:].tolist())
            i = best
        return np.array(out_r), np.array(out_c)

    def plan(self, start_rc, goal_rc, profile="safest", margin_m=4000, smooth=True):
        if profile not in PROFILES:
            raise ValueError(f"profile must be one of {list(PROFILES)}")
        wt, we, wr = PROFILES[profile]
        t = self.t
        (sr, sc, sm) = self.snap_to_safe(*start_rc)
        (gr, gc, gm) = self.snap_to_safe(*goal_rc)
        span = t.res * math.hypot(gr - sr, gc - sc)
        # search inside a window around A and B; grow it if no route is found
        for grow in (1.0, 2.5, None):
            if grow is None:
                r0, r1, c0, c1 = 0, t.shape[0], 0, t.shape[1]
            else:
                m = int((margin_m + 0.5 * span * grow) / t.res)
                r0, r1 = max(0, min(sr, gr) - m), min(t.shape[0], max(sr, gr) + m + 1)
                c0, c1 = max(0, min(sc, gc) - m), min(t.shape[1], max(sc, gc) + m + 1)
            win = (slice(r0, r1), slice(c0, c1))
            path, order = astar(
                t.elev[win], t.slope[win], t.rough[win], t.hazdist[win],
                t.nogo[win], float(t.res), sr - r0, sc - c0, gr - r0, gc - c0,
                wt, we, wr, self.a.soft_slope_deg, self.a.max_slope_deg,
                heuristic_factor(wt, we), MOVES)
            if len(path):
                W = c1 - c0
                rows = path // W + r0
                cols = path % W + c0
                if smooth:
                    rows, cols = self.smooth(rows, cols, profile)
                else:
                    rows, cols = rows.astype(float), cols.astype(float)
                rep = self.report(rows, cols, profile)
                rep["snapped_start_m"] = round(sm, 1)
                rep["snapped_goal_m"] = round(gm, 1)
                rep["nodes_expanded"] = int(len(order))
                # where the search looked, as full-grid row/col (for the "replay search" view)
                rep["_explored"] = (order // W + r0, order % W + c0)
                return rows, cols, rep
            if grow is None:
                break
        raise RuntimeError("No safe route exists between these points under the EVA limits.")

    # -------------------------------------------------------------- explanation
    TERM_NAMES = ("time", "energy", "steep ground", "steep descent",
                  "side-slope", "rough ground", "near cliff")

    def breakdown(self, rows, cols, profile):
        """Per-step cost ingredients along a polyline (fractional grid coords).
        Returns a dict of arrays, each already multiplied by its profile weight and the
        step length, so they add up to the route's total cost."""
        t, a = self.t, self.a
        wt, we, wr = PROFILES[profile]
        n = len(rows) - 1
        out = {k: np.zeros(n) for k in self.TERM_NAMES}
        grade = np.zeros(n)
        dist = np.zeros(n)
        blocked = np.zeros(n, dtype=bool)
        for k in range(n):
            r0, c0, r1, c1 = rows[k], cols[k], rows[k + 1], cols[k + 1]
            d = t.res * math.hypot(r1 - r0, c1 - c0)
            dist[k] = d
            if d == 0:
                continue
            i0, j0 = int(round(r0)), int(round(c0))
            i1, j1 = int(round(r1)), int(round(c1))
            dz = bilinear(t.elev, r1, c1) - bilinear(t.elev, r0, c0)
            tn, en, rs, rd, rsd, rr, rp, g = edge_terms(
                d, dz, t.slope[i0, j0], t.slope[i1, j1], t.rough[i0, j0], t.rough[i1, j1],
                t.hazdist[i0, j0], t.hazdist[i1, j1], a.soft_slope_deg)
            grade[k] = g
            blocked[k] = (t.nogo[i0, j0] or t.nogo[i1, j1] or abs(g) > a.max_slope_deg)
            for name, v, w in zip(self.TERM_NAMES, (tn, en, rs, rd, rsd, rr, rp),
                                  (wt, we, wr, wr, wr, wr, wr)):
                out[name][k] = d * w * v
        return {"terms": out, "grade": grade, "dist": dist, "blocked": blocked}

    def straight_line(self, a_rc, b_rc, profile):
        """Cost and hazards of simply walking straight from A to B, for comparison."""
        r0, c0 = a_rc
        r1, c1 = b_rc
        n = max(1, int(math.ceil(math.hypot(r1 - r0, c1 - c0))))
        f = np.linspace(0, 1, n + 1)
        rows, cols = r0 + f * (r1 - r0), c0 + f * (c1 - c0)
        bd = self.breakdown(rows, cols, profile)
        ri, ci = np.rint(rows).astype(int), np.rint(cols).astype(int)
        return {
            "rows": rows, "cols": cols,
            "distance_km": float(bd["dist"].sum() / 1000),
            "cost": float(sum(v.sum() for v in bd["terms"].values())),
            "terms": {k: float(v.sum()) for k, v in bd["terms"].items()},
            "nogo_m": float(bd["dist"][bd["blocked"]].sum()),
            "max_slope_deg": float(self.t.slope[ri, ci].max()),
            "max_grade_deg": float(np.abs(bd["grade"]).max()),
        }

    # -------------------------------------------------------------- metrics
    def report(self, rows, cols, profile):
        t, a = self.t, self.a
        z = np.array([bilinear(t.elev, r, c) for r, c in zip(rows, cols)])
        xy = np.array([t.rc_to_xy(r, c) for r, c in zip(rows, cols)])
        fr, fc = rows, cols
        rows = np.rint(fr).astype(int)
        cols = np.rint(fc).astype(int)
        dh = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1]))
        dz = np.diff(z)
        grade = dz / np.maximum(dh, 1e-6)
        grade_deg = np.degrees(np.arctan(grade))
        speed_ms = np.array([tobler_kmh(g) for g in grade]) * a.suit_speed_factor / 3.6
        seg_t = dh / speed_ms
        mass = a.body_mass_kg + a.suit_mass_kg
        seg_e = np.array([minetti(g) for g in grade]) * mass * dh * (G_MARS / G_EARTH)
        cum_d = np.concatenate([[0], np.cumsum(dh)])
        cum_t = np.concatenate([[0], np.cumsum(seg_t)])
        slope = t.slope[rows, cols]
        hz = t.hazdist[rows, cols]
        rough = t.rough[rows, cols]
        total_h = cum_t[-1] / 3600
        budget = a.eva_hours * (1 - a.reserve_frac)

        # rest stops: flattest, safest cell near every ~45 min of walking
        stops = []
        target = 45 * 60
        while target < cum_t[-1] - 600:
            k = int(np.searchsorted(cum_t, target))
            lo, hi = max(0, k - 15), min(len(rows), k + 15)
            score = slope[lo:hi] - 0.02 * np.minimum(hz[lo:hi], 300)
            j = lo + int(np.argmin(score))
            lon, lat = t.rc_to_lonlat(fr[j], fc[j])
            stops.append({"lon": round(lon, 5), "lat": round(lat, 5),
                          "after_km": round(cum_d[j] / 1000, 2),
                          "after_min": round(cum_t[j] / 60),
                          "slope_deg": round(float(slope[j]), 1)})
            target += 45 * 60

        # flag the tricky stretches
        caution = []
        flag = (np.abs(grade_deg) > a.soft_slope_deg) | (hz[1:] < 60) | (rough[1:] > 2.5)
        k = 0
        while k < len(flag):
            if flag[k]:
                j = k
                while j < len(flag) and flag[j]:
                    j += 1
                reasons = []
                if np.any(np.abs(grade_deg[k:j]) > a.soft_slope_deg):
                    reasons.append(f"grade up to {np.max(np.abs(grade_deg[k:j])):.0f} deg")
                if np.any(hz[k + 1:j + 1] < 60):
                    reasons.append(f"within {np.min(hz[k + 1:j + 1]):.0f} m of cliff/no-go terrain")
                if np.any(rough[k + 1:j + 1] > 2.5):
                    reasons.append("rough/blocky surface")
                caution.append({"from_km": round(cum_d[k] / 1000, 2),
                                "to_km": round(cum_d[j] / 1000, 2),
                                "why": "; ".join(reasons)})
                k = j
            else:
                k += 1

        if total_h <= budget * 0.5:
            verdict = "GO - fits one EVA with time to walk back"
        elif total_h <= budget:
            verdict = "GO (one-way) - fits the EVA budget but not a return trip on foot"
        else:
            verdict = "NO-GO on foot - exceeds EVA budget; needs a rover or staging point"

        return {
            "profile": profile,
            "verdict": verdict,
            "distance_km": round(cum_d[-1] / 1000, 3),
            "straight_line_km": round(float(np.hypot(*(xy[-1] - xy[0]))) / 1000, 3),
            "walking_time_h": round(total_h, 2),
            "eva_budget_h": budget,
            "energy_kJ": round(float(seg_e.sum()) / 1000, 0),
            "ascent_m": round(float(dz[dz > 0].sum()), 1),
            "descent_m": round(float(-dz[dz < 0].sum()), 1),
            "start_elev_m": round(z[0], 1), "goal_elev_m": round(z[-1], 1),
            "max_uphill_grade_deg": round(float(grade_deg.max()), 1),
            "max_downhill_grade_deg": round(float(-grade_deg.min()), 1),
            "max_terrain_slope_deg": round(float(slope.max()), 1),
            "mean_terrain_slope_deg": round(float(slope.mean()), 1),
            "closest_approach_to_hazard_m": round(float(hz.min()), 0),
            "rest_stops": stops,
            "caution_segments": caution,
            "astronaut": asdict(a),
            "_cum_d": cum_d, "_z": z, "_xy": xy,
        }
