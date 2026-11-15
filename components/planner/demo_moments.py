#!/usr/bin/env python3
"""
Renders the four demo moments of spec 15.2 from live planner output:

  1. the same destination planned with high vs low localization confidence
  2. a fix stop appearing automatically, with its time tradeoff
  3. a dust storm removing star / camera fixes (confidence and HUD mode change)
  4. an SEP alert flipping the plan to the shelter route

    python demo_moments.py [--synthetic] [--out docs/demo_moments.png]
"""
import argparse
import copy
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true", default=True)
    ap.add_argument("--dem")
    ap.add_argument("--out", default=os.path.join(HERE, "docs", "demo_moments.png"))
    a = ap.parse_args()
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import test_spec as T
    from environment import Weather, SEPAlert
    from mission_planner import MissionPlanner

    t = T.dem()
    mp0 = T.planner()
    f = mp0.frame
    H, W = t.shape
    (e0, e1), (n1, n0) = (f.rc_to_enu(t, [0, H], [0, W])[0], f.rc_to_enu(t, [0, H], [0, W])[1])
    hs = t.hillshade(step=2)
    ext = (min(e0, e1), max(e0, e1), min(n0, n1), max(n0, n1))
    ng = t.nogo[::2, ::2]

    def base(ax, title):
        ax.imshow(hs, cmap="gray", extent=ext, vmin=0, vmax=1)
        ov = np.zeros(ng.shape + (4,)); ov[ng] = (0.85, 0.1, 0.1, 0.55)
        ax.imshow(ov, extent=ext)
        ax.set_title(title, fontsize=10, loc="left")
        ax.set_xlim(-4200, 3200); ax.set_ylim(-1200, 3300)
        ax.set_xlabel("East (m, Site ENU)"); ax.set_ylabel("North (m)")
        ax.plot(0, 0, "w^", mec="k", ms=9)

    def route(ax, p, color, label):
        ax.plot(p.enu[:, 0], p.enu[:, 1], color=color, lw=2.2, label=label)
        for s in p.stops:
            ax.plot(*p.enu[s["idx"]][:2], "o", color="gold", mec="k", ms=9, zorder=5)

    fig, axs = plt.subplots(2, 2, figsize=(15, 10.5))
    GW = T.GOAL_W

    # 1. high vs low confidence (feature-poor terrain so sigma matters)
    ax = axs[0, 0]; base(ax, "1  Same destination, high vs low localization confidence")
    mp = T.feature_poor(mp0)
    for s0, c in ((5, "#1f9d55"), (60, "#e8590c")):
        p = mp.plan((0, 0), GW, loc_state=T.sig_state(s0), refine=False)
        route(ax, p, c, f"start sigma {s0} m: {p.dist_m[-1] / 1000:.1f} km, ETA {p.eta_min:.0f} min, "
                        f"min clearance {p._verify['clear'].min():.0f} m")
    ax.plot(*GW, "w*", mec="k", ms=14); ax.legend(loc="lower left", fontsize=8)

    # 2. fix stop appears
    ax = axs[0, 1]; base(ax, "2  A fix stop appears where it is cheapest")
    mp = copy.copy(mp0); mp.loc = copy.deepcopy(mp0.loc)
    mp.loc.orb[:] = 0; mp.loc.sky[:] = 0; mp.loc.open[:] = 0
    S = mp.loc.stride; r, c = f.enu_to_rc(t, -1300, 800)
    mp.loc.sky[(r - 25) // S:(r + 25) // S + 1, (c - 25) // S:(c + 25) // S + 1] = 0.95
    p = mp.plan((0, 0), GW, loc_state=T.sig_state(60), refine=False)
    route(ax, p, "#1c7ed6", f"{p.dist_m[-1] / 1000:.1f} km, ETA {p.eta_min:.1f} min")
    from matplotlib.patches import Rectangle
    ax.add_patch(Rectangle((-1300 - 500, 800 - 500), 1000, 1000, fill=False, ec="gold", lw=1.5, ls="--"))
    ax.text(-1750, 120, "good-horizon area", color="gold", fontsize=8)
    if p.stops:
        s = p.stops[0]
        ax.annotate(f"{s['method']} fix, {s['duration_s']:.0f} s\nsigma {p.sigma[s['idx'] - 1]:.0f} -> {p.sigma[s['idx']]:.0f} m",
                    p.enu[s["idx"]][:2], xytext=(-3900, -700), color="w", fontsize=9, fontweight="bold",
                    arrowprops=dict(arrowstyle="->", color="gold"), bbox=dict(boxstyle="round", fc="#222", ec="gold"))
    ax.text(-4100, 2900, "without any fix opportunity this crossing is REJECTED", color="w", fontsize=8,
            bbox=dict(fc="#c92a2a", ec="none"))
    ax.plot(*GW, "w*", mec="k", ms=14); ax.legend(loc="lower left", fontsize=8)

    # 3. dust storm: terrain where orbital image matching works in clear air, so dust has something to degrade
    ax = axs[1, 0]
    ax.set_title("3  Dust storm: camera fixes degrade, sigma rises, the route slows", fontsize=10, loc="left")
    storm = Weather(dust_events=[(0, 70, 5.0)])          # opacity ramps to 5 at minute 35, mid-route
    mpc = T.planner(); mpd = T.planner(weather=storm)
    for nm, m, c in (("clear air", mpc, "#1f9d55"), ("dust storm (tau -> 5)", mpd, "#e8590c")):
        p = m.plan((0, 0), (1500, 900), loc_state=T.sig_state(20), refine=False)
        ax.plot(p.dist_m, p.sigma, color=c, lw=2, label=f"{nm}: ETA {p.eta_min:.0f} min, ends {p.sigma[-1]:.0f} m")
    for y, lab in ((5, "HIGH  < 5 m: full ribbon"), (30, "MEDIUM < 30 m: ribbon + uncertainty halo"),
                   (300, "LOW < 300 m: bearing arrow + waypoints only")):
        ax.axhline(y, color="gray", ls=":", lw=1); ax.text(10, y * 1.08, lab, fontsize=8, color="gray")
    ax.set_yscale("log"); ax.set_xlabel("distance along route (m)"); ax.set_ylabel("predicted 1-sigma (m)")
    ax.legend(fontsize=8, loc="center right")
    w = storm
    ax.text(0.30, 0.72, f"at storm peak: camera quality {w.camera_quality(35):.2f}, celestial availability "
            f"{w.celestial_availability(35):.2f} (clear {Weather().celestial_availability(35):.2f})",
            transform=ax.transAxes, fontsize=8, va="top")

    # 4. SEP
    ax = axs[1, 1]; base(ax, "4  SEP alert: fastest route to shelter + countdown")
    mps = T.planner(sep=SEPAlert(onset_min=90))
    p = mps.plan_shelter_route((2000, 1200), "walk")
    ax.plot(p.enu[:, 0], p.enu[:, 1], color="#ae3ec9", lw=3)
    ax.plot(2000, 1200, "o", color="#ae3ec9", mec="w", ms=10)
    ax.text(2000 - 1800, 1200 + 250, f"{p.verdict}\nETA {p.eta_min:.0f} min   countdown "
            f"{p.sep['countdown_s'] / 60:.0f} min to onset\ndose to shelter {p.sep['dose_to_shelter_mSv']:.3f} mSv",
            color="w", fontsize=9, fontweight="bold", bbox=dict(boxstyle="round", fc="#5f3dc4", ec="none"))
    fig.suptitle("Spec 15.2 demo moments, rendered from live planner output (synthetic Jezero-like terrain; "
                 "red = no-go, white triangle = lander, gold = fix stop)", fontsize=11)
    fig.tight_layout()
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    fig.savefig(a.out, dpi=110)
    print("wrote", a.out)


if __name__ == "__main__":
    main()
