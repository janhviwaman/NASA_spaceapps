"""
Timing benchmark for the system requirements that are about speed:

    SYS-2  initial path planning over a ~10 km radius      <= 30 s
    SYS-3  replanning on a new hazard / state change        <= 5 s
    SYS-1  (target resolution is the AI layer's job; the planner side of it, the coarse
           'rank candidate sites' run, is timed here too)

    python tests/bench_spec.py [grid_size]       # default 1100 cells = 22 km at 20 m

Numba kernels are compiled up front (MissionPlanner.warmup) and cached on disk, so the numbers
are steady-state, which is what a lander computer would see after its first boot.
"""
import os
import tempfile
import resource
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import numpy as np                                  # noqa: E402
import synth_dem                                    # noqa: E402
from terrain import Terrain                         # noqa: E402
from mission_planner import MissionPlanner          # noqa: E402
from replan import DStarReplanner                   # noqa: E402


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def main(n=1100):
    path = os.environ.get("JEZERO_DEM") or os.path.join(tempfile.gettempdir(), "jezero_synth", f"synth_{n}.tif")
    if not os.path.exists(path):
        synth_dem.make(path, n=n)
    t0 = time.time()
    t = Terrain(path)
    t_terrain = time.time() - t0
    t0 = time.time()
    mp = MissionPlanner(t)
    t_layers = time.time() - t0
    t0 = time.time()
    mp.warmup()
    t_warm = time.time() - t0
    print(f"grid {t.shape[0]}x{t.shape[1]} cells ({t.shape[0] * t.res / 1000:.0f} km); "
          f"terrain layers {t_terrain:.1f} s, localizability layer {t_layers:.1f} s, kernel warm-up {t_warm:.1f} s")

    rows = []
    # ~10 km radius from the lander, three headings. A 10 km one-way WALK cannot satisfy SYS-9 (return
    # on foot with margin inside one EVA), so walking is timed as pure path search (return constraint
    # off); the rover runs with every constraint on.
    for name, goal in (("NE 10 km", (7000, 7000)), ("W 10 km (through the scarp)", (-9500, 2000)),
                       ("S 10 km", (500, -9500))):
        for mode, enforce in (("walk", False), ("rover", True)):
            t0 = time.time()
            tag = f"{mode}{'' if enforce else ', path search only'}"
            try:
                p = mp.plan((0, 0), goal, mode, refine=True, enforce_return=enforce)
                dt = time.time() - t0
                rows.append((f"SYS-2 plan {name} [{tag}]", dt, 30.0,
                             f"{p.dist_m[-1] / 1000:.1f} km, ETA {p.eta_min:.0f} min, {p.verdict}, "
                             f"window {p.stats['window_cells'] / 1e6:.2f} M cells"))
            except RuntimeError as e:
                rows.append((f"SYS-2 plan {name} [{tag}]", time.time() - t0, 30.0, "no feasible route: " + str(e)[:60]))
    t0 = time.time()
    try:
        mp.plan((0, 0), (7000, 7000), "walk", refine=False)
        note = "planned"
    except RuntimeError:
        note = "correctly rejected: no return-to-shelter margin on foot (SYS-9)"
    rows.append(("SYS-9 10 km WALK with return constraint", time.time() - t0, 30.0, note))
    # replanning on the rover's 10 km route
    base = mp.plan((0, 0), (7000, 7000), "rover", refine=False)
    mid = tuple(base.enu[len(base.enu) // 2][:2])
    t0 = time.time()
    rp = DStarReplanner(mp, (0, 0), (7000, 7000), "rover")
    rp.compute()
    t_build = time.time() - t0
    t0 = time.time()
    rp.block_hazard(mid, 150.0)
    rp.plan()
    mp.clear_dynamic_hazards()
    rows.append(("SYS-3 D* Lite repair after a new hazard (10 km rover route)", time.time() - t0, 5.0,
                 f"initial D* build {t_build:.1f} s"))
    t0 = time.time()
    mp.replan("dust", (1000, 1000), (7000, 7000), "rover")
    rows.append(("SYS-3 replan on a state change (dust / confidence)", time.time() - t0, 5.0, "full uncertainty-aware A*"))
    t0 = time.time()
    mp.replan("sep_alert", (3000, 3000), (7000, 7000), "rover")
    rows.append(("SYS-3 SEP alert -> shelter route", time.time() - t0, 5.0, ""))
    q = {"mode": "walk", "time_budget_min": 600, "candidate_sites": [
        {"id": f"S{i}", "enu_m": [float(x), float(y)], "science_score": 0.5 + 0.05 * i}
        for i, (x, y) in enumerate([(2500, 1500), (-3200, 2000), (4000, 3500), (1000, -4000), (6000, 2000)])]}
    t0 = time.time()
    mp.rank_sites(q, top_n=3)
    rows.append(("SYS-1 planner side: rank 5 candidate sites", time.time() - t0, 10.0, "coarse planner runs"))

    print()
    ok_all = True
    for name, dt, lim, note in rows:
        ok = dt <= lim
        ok_all &= ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:58s} {dt:6.2f} s (limit {lim:g} s)  {note}")
    print(f"\npeak memory {rss_mb():.0f} MB")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 1100))
