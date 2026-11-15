"""Synthetic Jezero-like DEM for testing without the 90 MB USGS download.

Georeferenced exactly like the real CTX mosaic (Mars 2000 sphere, equirectangular, standard
parallel 18.4663 N, 20 m/px) around the landing site, with: a gently rolling crater floor,
a rocky field, a sand-like smooth plain, a steep delta scarp (cliff) with a few passes, and
a crater rim. The goal is a terrain with the SAME failure modes (cliffs, narrow passes,
featureless plains) so the planner's behaviours can be exercised and checked.
"""
import math
import os

import numpy as np
import rasterio
from affine import Affine
from scipy import ndimage

MARS_R = 3396190.0
LAT_TS = math.radians(18.4663)


def make(path, n=700, res=20.0, seed=3, lat0=18.44463, lon0=77.45089):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32)
    # fractal-ish rolling terrain
    z = np.zeros((n, n), np.float32)
    for sc, amp in ((60, 18.0), (25, 6.0), (10, 2.0), (4, 0.6)):
        z += amp * ndimage.gaussian_filter(rng.standard_normal((n, n)).astype(np.float32), sc) * math.sqrt(sc)
    z -= z.mean()
    z += 0.12 * rng.standard_normal((n, n)).astype(np.float32)   # centimetre-scale texture
    cx, cy = n * 0.5, n * 0.5
    # crater rim to the east and north-east
    rr = np.hypot(xx - n * 0.95, yy - n * 0.2)
    z += 260 * np.exp(-((rr - n * 0.3) / (n * 0.04)) ** 2)
    # delta scarp: a steep step (cliff) running N-S at x ~ 0.3 n, with two passes
    step = 70 * (1 / (1 + np.exp(-np.clip((xx - n * 0.3) / 0.8, -50, 50))))
    for yc in (n * 0.35, n * 0.72):
        gap = np.exp(-((yy - yc) / (n * 0.025)) ** 2)
        step = step * (1 - 0.92 * gap)
    z += step
    # smooth plain in the south (sand-like: flat and smooth)
    plain = np.exp(-((yy - n * 0.88) / (n * 0.07)) ** 4)
    z = z * (1 - 0.95 * plain) + 0.004 * rng.standard_normal((n, n)).astype(np.float32) * plain
    # rocky field in the north-west
    rock = np.exp(-(((xx - n * 0.15) ** 2 + (yy - n * 0.15) ** 2) / (n * 0.1) ** 2))
    z += 2.5 * rock * rng.standard_normal((n, n)).astype(np.float32)
    z += -2600.0
    # georeference so that (lat0, lon0) is the grid centre
    x0c = MARS_R * math.radians(lon0) * math.cos(LAT_TS)
    y0c = MARS_R * math.radians(lat0)
    tr = Affine(res, 0, x0c - n * res / 2, 0, -res, y0c + n * res / 2)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", height=n, width=n, count=1, dtype="float32",
                       transform=tr, nodata=-3.4e38) as ds:
        ds.write(z.astype(np.float32), 1)
    return path


if __name__ == "__main__":
    import sys
    print(make(sys.argv[1] if len(sys.argv) > 1 else "synth_dem.tif"))
