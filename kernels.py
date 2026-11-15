"""
kernels.py - numba kernels for the spec-compliant planner (spec Sections 8-10).

Search state is (cell, sigma bucket), not just the cell. That is what makes the planner
uncertainty-aware (spec 9.2): the position covariance is propagated along every candidate
path, hazard buffers are checked against the sigma predicted AT THAT POINT, and a short
stationary fix stop is an explicit action whose time is paid for in the cost (spec 9.3).

Cost units are seconds-equivalent:
    cost = wt*time + we*energy_kJ + wr*risk*distance + wl*(1-localizability)*distance

Approximations (be aware): sigma is carried exactly per label but labels are bucketed into
K levels, and the side resources (time, energy, consumables) are carried along the single
best label per bucket. That is a standard heuristic for resource-constrained shortest path;
it is not a proof of global optimality. The independent verifier in mission_planner.py
re-checks every constraint on the finished route.
"""
import hashlib
import math
import os

import numpy as np
from numba import njit


def _numba_cache_guard():
    """numba's on-disk cache (cache=True) is keyed on a function's OWN source file, so editing a
    callee in another module (e.g. localization.sigma_move) leaves stale compiled search
    kernels behind: the search then silently disagrees with the Python-level verifier.
    Hash every source file the kernels depend on and drop the cache when any of them changes."""
    here = os.path.dirname(os.path.abspath(__file__))
    cdir = os.path.join(here, "__pycache__")
    try:
        h = hashlib.md5()
        for f in ("kernels.py", "localization.py", "planner.py", "replan.py"):
            with open(os.path.join(here, f), "rb") as fh:
                h.update(fh.read())
        stamp = os.path.join(cdir, "numba_deps.md5")
        old = open(stamp).read() if os.path.exists(stamp) else ""
        if old != h.hexdigest():
            if os.path.isdir(cdir):
                for fn in os.listdir(cdir):
                    if fn.endswith((".nbi", ".nbc")):
                        os.remove(os.path.join(cdir, fn))
            os.makedirs(cdir, exist_ok=True)
            with open(stamp, "w") as fh:
                fh.write(h.hexdigest())
    except OSError:
        pass


_numba_cache_guard()

from planner import tobler_kmh, minetti, G_MARS, G_EARTH
from localization import (sigma_move, best_fix, fuse, bucket,
                          L_NSIG, L_BASE, L_STOPS, L_STAND, L_ALLOW, L_GAIN, L_LOST)

# 16-connected move set (same as the legacy planner)
MOVES = np.array([(-1, 0), (1, 0), (0, -1), (0, 1),
                  (-1, -1), (-1, 1), (1, -1), (1, 1),
                  (-2, -1), (-2, 1), (2, -1), (2, 1),
                  (-1, -2), (1, -2), (-1, 2), (1, 2)], dtype=np.int64)

# mode parameter array P layout
P_VFLAT, P_MASS, P_C2, P_ETA, P_SOFT, P_MAXS, P_SIDE, P_HOTEL, P_DOWN0, P_DOWN1, P_ROUGH = range(11)
WALK, ROVER = 0, 1


# ---------------------------------------------------------------- indexed binary heap
@njit(cache=True)
def _sift_up(hk, hn, pos, i):
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
def _sift_down(hk, hn, pos, size, i):
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


@njit(cache=True)
def h_push(hk, hn, pos, size, key, node):
    hk[size] = key
    hn[size] = node
    pos[node] = size
    _sift_up(hk, hn, pos, size)
    return size + 1


@njit(cache=True)
def h_update(hk, hn, pos, size, key, node):
    i = pos[node]
    old = hk[i]
    hk[i] = key
    if key < old:
        _sift_up(hk, hn, pos, i)
    else:
        _sift_down(hk, hn, pos, size, i)
    return size


@njit(cache=True)
def h_pop(hk, hn, pos, size, mark):
    node = hn[0]
    pos[node] = mark
    size -= 1
    if size > 0:
        hk[0] = hk[size]
        hn[0] = hn[size]
        pos[hn[0]] = 0
        _sift_down(hk, hn, pos, size, 0)
    return node, size


@njit(cache=True)
def h_remove(hk, hn, pos, size, node):
    i = pos[node]
    size -= 1
    if i != size:
        moved = hn[size]
        hk[i] = hk[size]
        hn[i] = moved
        pos[moved] = i
        _sift_up(hk, hn, pos, i)
        _sift_down(hk, hn, pos, size, pos[moved])
    pos[node] = -1
    return size


# ---------------------------------------------------------------- physics
@njit(cache=True)
def edge_phys(mode, d, dz, s0, s1, r0, r1, sand, P):
    """Travel time (s) and energy (J) of one move. Walking: Tobler speed x suit factor and
    Minetti metabolic cost scaled to Mars gravity. Rover: speed derated by slope, rock and
    sand; energy = rolling resistance + climb + cabin load."""
    gi = dz / d
    if mode == 0:
        v = tobler_kmh(gi) * P[P_C2] / 3.6
        dt = d / v
        dE = minetti(gi) * P[P_MASS] * d * (G_MARS / G_EARTH)
    else:
        s = 0.5 * (s0 + s1)
        rg = 0.5 * (r0 + r1)
        der = 1.0 - 0.8 * (s / P[P_MAXS]) ** 2
        if der < 0.15:
            der = 0.15
        der *= (1.0 - 0.4 * min(rg / 2.0, 1.0)) * (1.0 - 0.5 * sand)
        v = P[P_VFLAT] * der
        dt = d / v
        up = gi if gi > 0.0 else 0.0
        dE = P[P_MASS] * G_MARS * d * (P[P_C2] * (1.0 + 2.0 * sand) + up) / P[P_ETA] + P[P_HOTEL] * dt
    return dt, dE


@njit(cache=True)
def risk_per_m(d, dz, s0, s1, r0, r1, hz_eff, P):
    """Unweighted risk per metre: steep ground, steep descent, side-slope, roughness, and
    closeness to a hazard (hz_eff already has the uncertainty-inflated buffer removed)."""
    gi = dz / d
    s = 0.5 * (s0 + s1)
    rgh = 0.5 * (r0 + r1)
    st = math.tan(math.radians(s))
    cross = math.degrees(math.atan(math.sqrt(max(st * st - gi * gi, 0.0))))
    r_slope = (s / P[P_SOFT]) ** 3
    down = -math.degrees(math.atan(gi)) - P[P_DOWN0]
    r_down = (down / P[P_DOWN1]) ** 2 if down > 0 else 0.0
    r_side = (cross / P[P_SIDE]) ** 2
    r_rough = (rgh / P[P_ROUGH]) ** 2
    r_prox = 2.0 * math.exp(-hz_eff / 80.0)
    return r_slope + r_down + r_side + r_rough + r_prox


@njit(cache=True)
def sweep_ok(nogo, ur, uc, dr, dc):
    """Knight moves sweep across two extra cells: both must be passable."""
    if abs(dr) + abs(dc) == 3:
        if abs(dr) == 2:
            if nogo[ur + dr // 2, uc] or nogo[ur + dr // 2, uc + dc]:
                return False
        else:
            if nogo[ur, uc + dc // 2] or nogo[ur + dr, uc + dc // 2]:
                return False
    return True


@njit(cache=True)
def res_ok(Tn, En, rT, rE, is_goal, CONS, margin, reserve, on_site, deadline):
    """SYS-9: after this leg, the crew must still be able to walk (drive) back to shelter on
    what is left of EVERY consumable, with `margin` extra on the return and `reserve` of
    each resource untouched, and before the weather / SEP deadline."""
    if not (rT < 1e11):
        return False
    extra = on_site if is_goal else 0.0
    if Tn + extra + (1.0 + margin) * rT > deadline:
        return False
    for r in range(CONS.shape[0]):
        a = CONS[r, 0]
        b = CONS[r, 1]
        used = a * (Tn + extra) + b * En
        need = used + (1.0 + margin) * (a * rT + b * rE)
        if need > (1.0 - reserve) * CONS[r, 2]:
            return False
    return True


# ---------------------------------------------------------------- return-to-shelter field
@njit(cache=True)
def return_field(elev, slope, rough, sand, hazd, nogo, res, shelters, mode, P,
                 dm, em, req_buf, moves):
    """Min-time cost-to-shelter (seconds) and the energy spent along that route, for every
    cell. Conservative: uses the worst weather multipliers over the horizon (dm, em) and a
    hazard buffer `req_buf` for the beacon-guided return."""
    H, W = elev.shape
    N = H * W
    T = np.full(N, 1e12)
    E = np.full(N, 1e12)
    pos = np.full(N, -1, dtype=np.int32)
    hk = np.empty(N, dtype=np.float64)
    hn = np.empty(N, dtype=np.int32)
    size = 0
    for i in range(shelters.shape[0]):
        s = shelters[i]
        T[s] = 0.0
        E[s] = 0.0
        size = h_push(hk, hn, pos, size, 0.0, s)
    while size > 0:
        v, size = h_pop(hk, hn, pos, size, -2)
        vr = v // W
        vc = v - vr * W
        for k in range(moves.shape[0]):
            ur = vr + moves[k, 0]
            uc = vc + moves[k, 1]
            if ur < 0 or ur >= H or uc < 0 or uc >= W:
                continue
            if nogo[ur, uc] or hazd[ur, uc] < req_buf:
                continue
            u = ur * W + uc
            if pos[u] == -2:
                continue
            # the step is u -> v, i.e. displacement (-moves[k])
            if not sweep_ok(nogo, ur, uc, -moves[k, 0], -moves[k, 1]):
                continue
            d = res * math.sqrt(float(moves[k, 0] ** 2 + moves[k, 1] ** 2))
            dz = elev[vr, vc] - elev[ur, uc]
            if math.degrees(math.atan(abs(dz / d))) > P[P_MAXS]:
                continue
            dt, dE = edge_phys(mode, d, dz, slope[ur, uc], slope[vr, vc],
                               rough[ur, uc], rough[vr, vc], sand[vr, vc], P)
            nt = T[v] + dt * dm
            if nt < T[u]:
                T[u] = nt
                E[u] = E[v] + dE * em
                if pos[u] >= 0:
                    size = h_update(hk, hn, pos, size, nt, u)
                else:
                    size = h_push(hk, hn, pos, size, nt, u)
    return T, E


# ---------------------------------------------------------------- uncertainty-aware A*
@njit(cache=True)
def search_aug(elev, slope, rough, sand, hazd, nogo, locS, locO, sopen, retT, retE,
               res, sr, sc, gr, gc, sig0, T0, E0,
               mode, P, W_, LV, LP, WB, bin_s,
               CONS, margin, reserve, on_site, deadline, enforce, hmin, moves, rec_cap):
    """A* over (cell, sigma bucket). Returns (ok, state ids, stop method codes, sigma, T, E,
    stats[expanded, hazard-pruned, resource-pruned, lost-pruned, stops-taken])."""
    H, Wd = elev.shape
    N = H * Wd
    K = LV.shape[0]
    NS = N * K
    nb = WB.shape[1]
    g = np.full(NS, np.inf)
    parent = np.full(NS, -1, dtype=np.int32)
    Tarr = np.zeros(NS, dtype=np.float32)
    Earr = np.zeros(NS, dtype=np.float32)
    Sarr = np.zeros(NS, dtype=np.float32)
    pm = np.zeros(NS, dtype=np.int8)
    pos = np.full(NS, -1, dtype=np.int32)
    hk = np.empty(NS, dtype=np.float64)
    hn = np.empty(NS, dtype=np.int32)
    stats = np.zeros(6, dtype=np.int64)
    # bounded recorder of the expansion ORDER, for the 'replay the search' view: keeps at most rec_cap states and
    # thins itself (every 2nd, then every 4th, ...) as the search grows, so memory stays tiny on any map size
    rec = np.empty(max(rec_cap, 2), dtype=np.int32)
    rec_n = 0
    rec_step = 1

    s = sr * Wd + sc
    goal = gr * Wd + gc
    k0 = bucket(sig0, LV)
    if k0 < 0:
        return False, np.empty(0, np.int64), np.empty(0, np.int8), np.empty(0), np.empty(0), np.empty(0), stats, rec[:0], 1
    st0 = s * K + k0
    g[st0] = 0.0
    Tarr[st0] = T0
    Earr[st0] = E0
    Sarr[st0] = sig0
    size = h_push(hk, hn, pos, 0, 0.0, st0)
    found = -1
    nsig = LP[L_NSIG]
    base = LP[L_BASE]
    stop_s = LP[L_STOPS]

    while size > 0:
        st, size = h_pop(hk, hn, pos, size, -2)
        stats[0] += 1
        if rec_cap > 0 and (stats[0] - 1) % rec_step == 0:
            if rec_n >= rec_cap:
                for q in range(rec_cap // 2):
                    rec[q] = rec[2 * q]
                rec_n = rec_cap // 2
                rec_step *= 2
            if (stats[0] - 1) % rec_step == 0:
                rec[rec_n] = st
                rec_n += 1
        u = st // K
        k = st - u * K
        if u == goal:
            found = st
            break
        ur = u // Wd
        uc = u - ur * Wd
        sig = Sarr[st]
        Tu = Tarr[st]
        Eu = Earr[st]
        gu = g[st]
        bi = int(Tu / bin_s)
        if bi >= nb:
            bi = nb - 1
        dm = WB[0, bi]
        em = WB[1, bi]
        cam = WB[2, bi]
        cel = WB[3, bi]

        # ---- moves
        for m in range(moves.shape[0]):
            dr = moves[m, 0]
            dc = moves[m, 1]
            vr = ur + dr
            vc = uc + dc
            if vr < 0 or vr >= H or vc < 0 or vc >= Wd:
                continue
            if nogo[vr, vc]:
                continue
            if not sweep_ok(nogo, ur, uc, dr, dc):
                continue
            d = res * math.sqrt(float(dr * dr + dc * dc))
            dz = elev[vr, vc] - elev[ur, uc]
            if math.degrees(math.atan(abs(dz / d))) > P[P_MAXS]:
                continue
            v = vr * Wd + vc
            sg2 = sigma_move(sig, d, sand[vr, vc], locS[vr, vc], locO[vr, vc], cam, LP)
            if sg2 > LP[L_LOST]:
                stats[3] += 1
                continue
            kn = bucket(sg2, LV)
            if kn < 0:
                stats[3] += 1
                continue
            # uncertainty-inflated hazard buffer (spec 9.2)
            hv = hazd[vr, vc]
            if hv < nsig * sg2 + base + 0.1 * d:        # +0.1*d: a straight leg can cut a cliff corner between two safe cells
                stats[1] += 1
                continue
            dt, dE = edge_phys(mode, d, dz, slope[ur, uc], slope[vr, vc],
                               rough[ur, uc], rough[vr, vc], sand[vr, vc], P)
            dt *= dm
            dE *= em
            Tn = Tu + dt
            En = Eu + dE
            if enforce:
                if not res_ok(Tn, En, retT[vr, vc], retE[vr, vc], v == goal,
                              CONS, margin, reserve, on_site, deadline):
                    stats[2] += 1
                    continue
            hz_eff = hv - nsig * sg2
            if hz_eff < 0.0:
                hz_eff = 0.0
            risk = risk_per_m(d, dz, slope[ur, uc], slope[vr, vc], rough[ur, uc], rough[vr, vc],
                              hz_eff, P)
            Lc = max(locS[vr, vc], locO[vr, vc])
            c = W_[0] * dt + W_[1] * dE / 1000.0 + W_[2] * risk * d + W_[3] * (1.0 - Lc) * d
            ng = gu + c
            vs = v * K + kn
            if pos[vs] == -2:
                continue
            dom = False
            for k2 in range(kn + 1):
                s2 = v * K + k2
                if g[s2] <= ng and Sarr[s2] <= sg2:
                    dom = True
                    break
            if dom:
                continue
            if ng < g[vs]:
                g[vs] = ng
                parent[vs] = st
                Tarr[vs] = Tn
                Earr[vs] = En
                Sarr[vs] = sg2
                pm[vs] = 0
                hh = hmin * res * math.sqrt(float((vr - gr) ** 2 + (vc - gc) ** 2))
                if pos[vs] >= 0:
                    size = h_update(hk, hn, pos, size, ng + hh, vs)
                else:
                    size = h_push(hk, hn, pos, size, ng + hh, vs)

        # ---- stop-and-fix action (spec 9.3): stationary pause, cost counted in the ETA
        if LP[L_ALLOW] > 0.5 and pm[st] == 0 and k > 0:
            sm, meth = best_fix(locS[ur, uc], locO[ur, uc], sopen[ur, uc], cam, cel, LP)
            if meth > 0:
                sf = fuse(sig, sm)
                if sf <= LP[L_GAIN] * sig:
                    kf = bucket(sf, LV)
                    if kf >= 0 and kf < k:
                        Tn = Tu + stop_s
                        En = Eu + LP[L_STAND] * stop_s
                        ok = True
                        if enforce:
                            ok = res_ok(Tn, En, retT[ur, uc], retE[ur, uc], False,
                                        CONS, margin, reserve, on_site, deadline)
                        if ok:
                            ng = gu + W_[0] * stop_s + W_[1] * LP[L_STAND] * stop_s / 1000.0
                            vs = u * K + kf
                            if pos[vs] != -2:
                                dom = False
                                for k2 in range(kf + 1):
                                    s2 = u * K + k2
                                    if g[s2] <= ng and Sarr[s2] <= sf:
                                        dom = True
                                        break
                                if (not dom) and ng < g[vs]:
                                    g[vs] = ng
                                    parent[vs] = st
                                    Tarr[vs] = Tn
                                    Earr[vs] = En
                                    Sarr[vs] = sf
                                    pm[vs] = meth
                                    stats[4] += 1
                                    hh = hmin * res * math.sqrt(float((ur - gr) ** 2 + (uc - gc) ** 2))
                                    if pos[vs] >= 0:
                                        size = h_update(hk, hn, pos, size, ng + hh, vs)
                                    else:
                                        size = h_push(hk, hn, pos, size, ng + hh, vs)

    if found < 0:
        return False, np.empty(0, np.int64), np.empty(0, np.int8), np.empty(0), np.empty(0), np.empty(0), stats, rec[:0], 1
    n = 1
    cur = found
    while cur != st0:
        cur = parent[cur]
        n += 1
    ids = np.empty(n, dtype=np.int64)
    meths = np.empty(n, dtype=np.int8)
    sigs = np.empty(n)
    Ts = np.empty(n)
    Es = np.empty(n)
    cur = found
    for i in range(n - 1, -1, -1):
        ids[i] = cur
        meths[i] = pm[cur]
        sigs[i] = Sarr[cur]
        Ts[i] = Tarr[cur]
        Es[i] = Earr[cur]
        if i > 0:
            cur = parent[cur]
    return True, ids, meths, sigs, Ts, Es, stats, rec[:rec_n], rec_step
