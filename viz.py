"""Route maps: hillshade + slope tint + no-go terrain + routes + elevation profiles."""
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import ListedColormap

COLORS = {"safest": "#1f9e5a", "balanced": "#2f6fd6", "fastest": "#d9822b"}
HERE = os.path.dirname(os.path.abspath(__file__))


def _hillshade(z, res, az=315.0, alt=35.0, exag=2.0):
    gy, gx = np.gradient(z * exag, res)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(gy, -gx)
    azr = np.radians(360 - az + 90)
    altr = np.radians(alt)
    hs = np.sin(altr) * np.cos(slope) + np.cos(altr) * np.sin(slope) * np.cos(azr - aspect)
    return np.clip(hs, 0, 1)


def plot_routes(t, results, a_ll, b_ll, out_png, landmarks=None):
    allr = np.concatenate([r for r, _, _ in results.values()])
    allc = np.concatenate([c for _, c, _ in results.values()])
    allr = np.rint(allr).astype(int)
    allc = np.rint(allc).astype(int)
    span = max(np.ptp(allr), np.ptp(allc))
    pad = max(100, int(0.12 * span))
    rc, cc = (allr.min() + allr.max()) // 2, (allc.min() + allc.max()) // 2
    half_h = max(np.ptp(allr) // 2 + pad, int(0.38 * span) + pad)
    half_w = max(np.ptp(allc) // 2 + pad, int(0.38 * span) + pad)
    r0, r1 = max(0, rc - half_h), min(t.shape[0], rc + half_h)
    c0, c1 = max(0, cc - half_w), min(t.shape[1], cc + half_w)
    win = (slice(r0, r1), slice(c0, c1))
    km = t.res / 1000
    ext = [(c0 - 0.5) * km, (c1 - 0.5) * km, (r1 - 0.5) * km, (r0 - 0.5) * km]

    aspect = (r1 - r0) / (c1 - c0)
    map_w = 10.0
    map_h = map_w * aspect
    fig = plt.figure(figsize=(map_w + 2.2, map_h + 4.0))
    H = map_h + 4.0
    ax = fig.add_axes([0.07, 3.4 / H, map_w / (map_w + 2.2) * 0.92, map_h / H * 0.93])
    ax.imshow(_hillshade(t.elev[win], t.res), cmap="gray", extent=ext)
    s = np.ma.masked_less(t.slope[win], 10)
    im = ax.imshow(s, cmap="YlOrRd", alpha=0.42, vmin=10, vmax=30, extent=ext)
    ax.imshow(np.ma.masked_equal(t.nogo[win].astype(int), 0),
              cmap=ListedColormap(["#8b0020"]), alpha=0.9, extent=ext)

    wp = os.path.join(HERE, "data", "M20_waypoints.csv")
    if os.path.exists(wp):
        w = pd.read_csv(wp)
        rr = [t.xy_to_rc(x, y) for x, y in zip(w.easting, w.northing)]
        ax.plot([c * km for _, c in rr], [r * km for r, _ in rr], color="#00e5ff", lw=1.2,
                ls="--", alpha=0.9, label="Perseverance actual traverse (sols 13-2022)")

    for p, (rows, cols, rep) in results.items():
        ax.plot(cols * km, rows * km, color="black", lw=4.2, alpha=0.5)
        ax.plot(cols * km, rows * km, color=COLORS[p], lw=2.6,
                label=f"{p}: {rep['distance_km']:.1f} km, {rep['walking_time_h']:.1f} h walk, "
                      f"steepest {max(rep['max_uphill_grade_deg'], rep['max_downhill_grade_deg']):.0f}°")
        for st in rep["rest_stops"]:
            sr, sc = t.lonlat_to_rc(st["lon"], st["lat"])
            ax.plot(sc * km, sr * km, "o", ms=6, mfc="white", mec=COLORS[p], mew=2)
    for (lat, lon), lab, col in ((a_ll, "A", "#00c2ff"), (b_ll, "B", "#ff2bd6")):
        pr, pc = t.lonlat_to_rc(lon, lat)
        ax.plot(pc * km, pr * km, "*", ms=22, color=col, mec="k")
        ax.annotate(lab, (pc * km, pr * km), xytext=(9, 9), textcoords="offset points",
                    fontsize=15, weight="bold", color=col,
                    bbox=dict(boxstyle="round,pad=0.15", fc="black", alpha=0.5, lw=0))
    ax.set_xlim(ext[0], ext[1])
    ax.set_ylim(ext[2], ext[3])
    ax.set_xlabel("km east (DEM grid)")
    ax.set_ylabel("km south (DEM grid)")
    ax.set_title("Jezero crater: astronaut EVA routes  (USGS CTX DEM, 20 m/px; white dots = rest stops)")
    ax.legend(loc="lower left", fontsize=8.5, framealpha=0.92)
    pos = ax.get_position()
    cax = fig.add_axes([pos.x1 + 0.015, pos.y0, 0.015, pos.height])
    fig.colorbar(im, cax=cax, label="terrain slope (deg)   |   dark red = no-go")

    ax2 = fig.add_axes([pos.x0, 0.55 / H, pos.width, 1.9 / H])
    for p, (rows, cols, rep) in results.items():
        ax2.plot(rep["_cum_d"] / 1000, rep["_z"], color=COLORS[p], lw=2, label=p)
        for c in rep["caution_segments"]:
            ax2.axvspan(c["from_km"], c["to_km"], color=COLORS[p], alpha=0.15)
    ax2.set_xlabel("distance along route (km)")
    ax2.set_ylabel("elevation (m)")
    ax2.grid(alpha=0.3)
    ax2.set_title("Elevation profile (shaded = caution segments)", fontsize=10)
    fig.savefig(out_png, dpi=120)
    plt.close(fig)
