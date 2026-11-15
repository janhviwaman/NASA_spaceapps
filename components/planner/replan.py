"""
replan.py - D* Lite incremental replanning (spec 9.4 'Replanning: D* Lite for new hazards
or state changes', 9.5 replan triggers, SYS-3 <= 5 s).

D* Lite searches backwards from the goal and keeps its g / rhs values between calls, so a
new hazard only repairs the part of the search tree it touches instead of starting over.
The edge cost is the same physics + risk + localization model as the main planner, with the
hazard buffer evaluated at an assumed sigma (D* Lite's graph is static, so it cannot carry
sigma as part of the state). The route it returns is therefore re-checked by the planner's
independent verifier, and if sigma-dependent constraints fail, the planner falls back to the
full uncertainty-aware A* (engine is reported in plan.stats["engine"]).
"""
import math
import time as _time

import numpy as np
from numba import njit
from scipy import ndimage

import kernels as K
from kernels import MOVES, h_push, h_update, h_remove, h_pop, edge_phys, risk_per_m, sweep_ok, P_MAXS

INF = 1e18


@njit(cache=True)
def _dcost(ur, uc, vr, vc, elev, slope, rough, sand, hazd, nogo, locS, locO, res, P, W_, mode,
           nsig_sig, base, dm, em):
    if nogo[vr, vc] or nogo[ur, uc]:
        return INF
    dr = vr - ur
    dc = vc - uc
    if not sweep_ok(nogo, ur, uc, dr, dc):
        return INF
    d = res * math.sqrt(float(dr * dr + dc * dc))
    dz = elev[vr, vc] - elev[ur, uc]
    if math.degrees(math.atan(abs(dz / d))) > P[P_MAXS]:
        return INF
    hv = hazd[vr, vc]
    if hv < nsig_sig + base + 0.1 * d:
        return INF
    dt, dE = edge_phys(mode, d, dz, slope[ur, uc], slope[vr, vc], rough[ur, uc], rough[vr, vc],
                       sand[vr, vc], P)
    hz = hv - nsig_sig
    if hz < 0.0:
        hz = 0.0
    risk = risk_per_m(d, dz, slope[ur, uc], slope[vr, vc], rough[ur, uc], rough[vr, vc], hz, P)
    Lc = max(locS[vr, vc], locO[vr, vc])
    return W_[0] * dt * dm + W_[1] * dE * em / 1000.0 + W_[2] * risk * d + W_[3] * (1.0 - Lc) * d


@njit(cache=True)
def _key(s, g, rhs, sr, sc, Wd, hmin, res, km):
    m = min(g[s], rhs[s])
    r = s // Wd
    c = s - r * Wd
    h = hmin * res * math.sqrt(float((r - sr) ** 2 + (c - sc) ** 2))
    return m + h + km + 1e-9 * m


@njit(cache=True)
def ds_build_rows(tab, dirty, elev, slope, rough, sand, hazd, nogo, locS, locO, res, P, W_, mode,
                  nsig_sig, base, dm, em, moves):
    """(Re)compute the edge-cost table rows of every `dirty` cell: tab[u, k] = cost of the move
    u -> u + moves[k] (>= 1e17 means blocked / out of bounds). The D* inner loop then only does
    table look-ups instead of re-evaluating the physics ~250 times per queue pop."""
    H, Wd = elev.shape
    for r in range(H):
        for c in range(Wd):
            if not dirty[r, c]:
                continue
            u = r * Wd + c
            for k in range(moves.shape[0]):
                vr = r + moves[k, 0]
                vc = c + moves[k, 1]
                if vr < 0 or vr >= H or vc < 0 or vc >= Wd:
                    tab[u, k] = 1e18
                else:
                    tab[u, k] = _dcost(r, c, vr, vc, elev, slope, rough, sand, hazd, nogo, locS, locO,
                                       res, P, W_, mode, nsig_sig, base, dm, em)


@njit(cache=True)
def _update_vertex(u, goal, g, rhs, hk, hn, pos, size, sr, sc, hmin, km, tab, Wd, res, moves):
    if u != goal:
        ur = u // Wd
        uc = u - ur * Wd
        best = INF
        for k in range(moves.shape[0]):
            c = tab[u, k]
            if c < 1e17:
                t = c + g[(ur + moves[k, 0]) * Wd + uc + moves[k, 1]]
                if t < best:
                    best = t
        rhs[u] = best
    if pos[u] >= 0:
        size = h_remove(hk, hn, pos, size, u)
    if abs(g[u] - rhs[u]) > 1e-9 and (g[u] < INF or rhs[u] < INF):
        size = h_push(hk, hn, pos, size, _key(u, g, rhs, sr, sc, Wd, hmin, res, km), u)
    return size


@njit(cache=True)
def ds_compute(g, rhs, hk, hn, pos, size, goal, sr, sc, hmin, km, tab, H, Wd, res, moves, max_iter):
    """Standard D* Lite ComputeShortestPath over the precomputed cost table.
    Returns (heap size, iterations)."""
    start = sr * Wd + sc
    it = 0
    while size > 0 and it < max_iter:
        kstart = _key(start, g, rhs, sr, sc, Wd, hmin, res, km)
        if hk[0] >= kstart and abs(g[start] - rhs[start]) <= 1e-9:
            break
        it += 1
        k_old = hk[0]
        u, size = h_pop(hk, hn, pos, size, -1)
        k_new = _key(u, g, rhs, sr, sc, Wd, hmin, res, km)
        if k_old < k_new:
            size = h_push(hk, hn, pos, size, k_new, u)
            continue
        ur = u // Wd
        uc = u - ur * Wd
        if g[u] > rhs[u]:
            g[u] = rhs[u]
        else:
            g[u] = INF
            size = _update_vertex(u, goal, g, rhs, hk, hn, pos, size, sr, sc, hmin, km, tab, Wd, res, moves)
        for k in range(moves.shape[0]):
            pr = ur + moves[k, 0]
            pc = uc + moves[k, 1]
            if pr < 0 or pr >= H or pc < 0 or pc >= Wd:
                continue
            size = _update_vertex(pr * Wd + pc, goal, g, rhs, hk, hn, pos, size, sr, sc, hmin, km,
                                  tab, Wd, res, moves)
    return size, it


@njit(cache=True)
def ds_touch(changed, goal, g, rhs, hk, hn, pos, size, sr, sc, hmin, km, tab, H, Wd, res, moves):
    """After the cost table changed in `changed` cells: repair those cells and their predecessors."""
    for r in range(H):
        for c in range(Wd):
            if not changed[r, c]:
                continue
            size = _update_vertex(r * Wd + c, goal, g, rhs, hk, hn, pos, size, sr, sc, hmin, km,
                                  tab, Wd, res, moves)
            for k in range(moves.shape[0]):
                pr = r + moves[k, 0]
                pc = c + moves[k, 1]
                if pr < 0 or pr >= H or pc < 0 or pc >= Wd:
                    continue
                size = _update_vertex(pr * Wd + pc, goal, g, rhs, hk, hn, pos, size, sr, sc, hmin, km,
                                      tab, Wd, res, moves)
    return size


@njit(cache=True)
def ds_path(g, goal, sr, sc, elev, slope, rough, sand, hazd, nogo, locS, locO, res, P, W_, mode,
            nsig_sig, base, dm, em, moves, max_len):
    H, Wd = elev.shape
    out = np.empty(max_len, dtype=np.int64)
    out[0] = sr * Wd + sc
    n = 1
    cur = out[0]
    while cur != goal and n < max_len:
        cr = cur // Wd
        cc = cur - cr * Wd
        best = INF
        bn = -1
        for k in range(moves.shape[0]):
            vr = cr + moves[k, 0]
            vc = cc + moves[k, 1]
            if vr < 0 or vr >= H or vc < 0 or vc >= Wd:
                continue
            c = _dcost(cr, cc, vr, vc, elev, slope, rough, sand, hazd, nogo, locS, locO, res, P, W_,
                       mode, nsig_sig, base, dm, em)
            if c >= INF:
                continue
            t = c + g[vr * Wd + vc]
            if t < best:
                best, bn = t, vr * Wd + vc
        if bn < 0 or best >= INF:
            return out[:0]
        cur = bn
        out[n] = cur
        n += 1
    if cur != goal:
        return out[:0]
    return out[:n]


class DStarReplanner:
    """Incremental planner bound to one (start, goal, mode). Typical use:

        r = DStarReplanner(planner, start_enu, goal_enu, mode="walk")
        plan = r.plan()                              # initial route
        r.block_hazard((x, y), radius_m=60)          # new hazard seen by the sensors
        plan = r.plan(current_enu=(x2, y2))          # repaired route from where the crew is now
    """

    def __init__(self, planner, start, goal, mode="walk", profile="min_eta", sigma_assumed_m=None,
                 margin_m=None, enforce_return=True):
        self.p = planner
        t = planner.t
        self.mode, self.profile = mode, profile
        agent = planner.agents[mode]
        self.mid = agent.mode_id        # integer id for the kernels (a string silently picks the rover branch)
        self.agent, self.P = agent, agent.params()
        self.W = planner._weights(profile)
        nogo, hazd = t.mode_layers(mode, agent.max_slope_deg, agent.rough_max, agent.sand_max)
        sr, sc = planner._snap(nogo, *planner.to_rc(start))
        gr, gc = planner._snap(nogo, *planner.to_rc(goal))
        shel = [planner._snap(nogo, *planner.to_rc(s)) for s in planner.shelters_enu] if enforce_return else []
        m = int(((margin_m or planner.margin_m) + 0.5 * t.res * math.hypot(gr - sr, gc - sc)) / t.res)
        pts = [(sr, sc), (gr, gc)] + shel
        self.r0, self.r1, self.c0, self.c1 = planner.fit_window(pts, m)
        self.res = float(t.res)
        dm, em = planner._worst_mult()
        self.ctx = planner._window_ctx(self.r0, self.r1, self.c0, self.c1, nogo, hazd, shel, self.mid,
                                       self.P, dm, em)
        self.nogo = self.ctx["nogo"].copy()
        self.hazd = self.ctx["hazd"].copy()
        self.shel = shel
        H, Wd = self.nogo.shape
        self.N = H * Wd
        lp = planner.lp
        self.sigma_assumed = sigma_assumed_m if sigma_assumed_m is not None else lp.sigma0_m * 1.5 + 5.0
        self.base = lp.base_buffer_m
        WB, _ = planner.weather.sampled()
        self.dm, self.em = float(WB[0, 0]), float(WB[1, 0])
        self.hmin = K_hmin(self)
        self.goal_rc = (gr - self.r0, gc - self.c0)
        self.start_rc = (sr - self.r0, sc - self.c0)
        self.last_start = self.start_rc
        self.km = 0.0
        self.g = np.full(self.N, INF)
        self.rhs = np.full(self.N, INF)
        self.hk = np.empty(self.N)
        self.hn = np.empty(self.N, dtype=np.int32)
        self.pos = np.full(self.N, -1, dtype=np.int32)
        goal = self.goal_rc[0] * Wd + self.goal_rc[1]
        self.goal = goal
        self.tab = np.empty((self.N, len(MOVES)), dtype=np.float32)
        self._rows(np.ones(self.nogo.shape, bool))
        self.rhs[goal] = 0.0
        self.size = h_push(self.hk, self.hn, self.pos, 0, self._h_goal(), goal)
        self.iters = 0
        self.stats = {}

    def _args(self):
        c = self.ctx
        return (c["elev"], c["slope"], c["rough"], c["sand"], self.hazd, self.nogo, c["locS"], c["locO"],
                self.res, self.P, self.W, self.mid, self.lp_nsig * self.sigma_assumed, self.base,
                self.dm, self.em)

    @property
    def lp_nsig(self):
        return self.p.lp.n_sigma

    def _rows(self, dirty):
        a = self._args()
        ds_build_rows(self.tab, dirty, *a, MOVES)

    def _h_goal(self):
        sr, sc = self.start_rc
        gr, gc = self.goal_rc
        return self.hmin * self.res * math.hypot(gr - sr, gc - sc)

    def compute(self, max_iter=200_000_000):
        t0 = _time.time()
        a = self._args()
        sr, sc = self.start_rc
        H, Wd = self.nogo.shape
        self.size, it = ds_compute(self.g, self.rhs, self.hk, self.hn, self.pos, self.size, self.goal,
                                   sr, sc, self.hmin, self.km, self.tab, H, Wd, self.res, MOVES, max_iter)
        self.iters += it
        self.stats = {"dstar_iterations": int(it), "dstar_s": round(_time.time() - t0, 3)}
        return self.g[sr * self.nogo.shape[1] + sc] < INF

    def block_hazard(self, enu, radius_m=40.0):
        """A new hazard (rock field, fresh crevasse, rover track ...) seen at ENU (x, y)."""
        p = self.p
        r, c = p.to_rc(tuple(enu))
        lr, lc = r - self.r0, c - self.c0
        H, Wd = self.nogo.shape
        rr, cc = np.ogrid[:H, :Wd]
        disc = (rr - lr) ** 2 + (cc - lc) ** 2 <= (radius_m / self.res) ** 2
        # never block the shelter or the crew's own cell
        for s in self.shel + [(self.start_rc[0] + self.r0, self.start_rc[1] + self.c0)]:
            disc[s[0] - self.r0, s[1] - self.c0] = False
        old_h = self.hazd
        self.nogo = self.nogo | disc
        self.hazd = (ndimage.distance_transform_edt(~self.nogo) * self.res).astype(np.float32)
        # Only cells within the hazard-proximity horizon need repair: feasibility depends on hazard
        # distance within n_sigma*sigma + base, and the proximity cost 2*exp(-hz/80) is < 0.05 % of an
        # edge's cost beyond 640 m. Re-evaluating every cell whose nearest hazard changed (tens of
        # thousands, many km away) cost ~3 s for no practical change in the route.
        horizon = max(640.0, self.lp_nsig * self.sigma_assumed + self.base + 200.0)
        changed = disc | ((self.hazd < old_h - 1e-6) & (self.hazd < horizon))
        t0 = _time.time()
        # a changed cell alters its own outgoing edges, every edge INTO it, and (knight moves) every
        # edge that sweeps across it: all of those start within 2 cells, so refresh that neighbourhood
        dirty = ndimage.binary_dilation(changed, structure=np.ones((5, 5), bool))
        self._rows(dirty)
        sr, sc = self.start_rc
        H, Wd = self.nogo.shape
        self.size = ds_touch(changed, self.goal, self.g, self.rhs, self.hk, self.hn, self.pos, self.size,
                             sr, sc, self.hmin, self.km, self.tab, H, Wd, self.res, MOVES)
        self.stats["touch_s"] = round(_time.time() - t0, 3)
        self.p._dyn_hazards.append((tuple(enu), radius_m))

    def move_start(self, enu):
        r, c = self.p.to_rc(tuple(enu))
        nr, nc = r - self.r0, c - self.c0
        self.km += self.hmin * self.res * math.hypot(nr - self.last_start[0], nc - self.last_start[1])
        self.last_start = self.start_rc = (nr, nc)

    def cells(self):
        a = self._args()
        sr, sc = self.start_rc
        ids = ds_path(self.g, self.goal, sr, sc, a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7], a[8],
                      a[9], a[10], a[11], a[12], a[13], a[14], a[15], MOVES, 200000)
        Wd = self.nogo.shape[1]
        return (ids // Wd + self.r0).astype(float), (ids % Wd + self.c0).astype(float)

    def plan(self, current_enu=None, loc_state=None, **kw):
        """Repair (if needed) and return the route as a verified MissionPlan."""
        t0 = _time.time()
        if current_enu is not None:
            self.move_start(current_enu)
        if loc_state is not None:
            self.sigma_assumed = max(loc_state.sigma_m(), self.sigma_assumed)
        ok = self.compute()
        if not ok:
            raise RuntimeError("D* Lite: no route to the goal around the new hazards.")
        rows, cols = self.cells()
        if len(rows) == 0:
            raise RuntimeError("D* Lite: no route to the goal around the new hazards.")
        p = self.p
        route = dict(rows=rows, cols=cols, meth=np.zeros(len(rows), np.int8),
                     sig=np.zeros(len(rows)), T=np.zeros(len(rows)), E=np.zeros(len(rows)))
        # refresh the return-to-shelter field for the changed terrain
        c = self.ctx
        c["nogo"], c["hazd"] = self.nogo, self.hazd
        shel_idx = np.array([(r - self.r0) * (self.c1 - self.c0) + (cc - self.c0) for r, cc in self.shel],
                            dtype=np.int64)
        dm_w, em_w = p._worst_mult()
        req = p.lp.n_sigma * p.lp.sigma_levels[0] + p.lp.base_buffer_m
        T_, E_ = K.return_field(c["elev"], c["slope"], c["rough"], c["sand"], self.hazd, self.nogo, self.res,
                                shel_idx, self.mid, self.P, dm_w, em_w, req, MOVES)
        c["retT"], c["retE"] = T_.reshape(self.nogo.shape), E_.reshape(self.nogo.shape)
        sig0 = loc_state.sigma_m() if loc_state is not None else p.lp.sigma0_m
        WB, bin_s = p.weather.sampled()
        plan = p._finalise(route, c, (self.r0, self.c0), self.mode, self.agent, self.P, self.profile, WB, bin_s,
                           p.cons.coeffs(self.mode), p.cons.on_site_s, p._deadline_s(), sig0,
                           kw.get("t_elapsed_s", 0.0), kw.get("e_used_j", 0.0), kw.get("enforce_return", True))
        plan.stats = dict(self.stats, engine="D* Lite", replan_s=round(_time.time() - t0, 3),
                          window_cells=int(self.N), nogo_pct_in_window=round(100.0 * float(self.nogo.mean()), 1))
        return plan


def K_hmin(r):
    from mission_planner import _hmin
    return _hmin(r.mid, r.P, r.W)
