"""
terrain.py - turns the raw Jezero DEM into the hazard/cost layers the planner uses.

Data: USGS / JPL "Mars 2020 Science Investigation CTX DEM Mosaic" of Jezero crater,
20 m/pixel, Mars 2000 sphere, equirectangular projection (standard parallel 18.4663 N).

Layers produced (all on the native 20 m grid):
  elev       elevation (m, relative to the Mars areoid)
  slope      slope angle in degrees (Horn 3x3 gradient)
  rough      local roughness: std-dev of elevation residual after removing a plane (m)
  relief     max elevation drop to any neighbour within 2 cells (m), used for cliff detection
  nogo       hard no-go mask (bad data, slope above EVA limit, cliffs)
  hazdist    distance in metres from each cell to the nearest no-go cell
"""
import math
import os

import numpy as np
import rasterio
from scipy import ndimage

MARS_R = 3396190.0
LAT_TS = math.radians(18.4663)


# ---------------------------------------------------------------- coordinates
def lonlat_to_xy(lon_e, lat_n):
    """Planetocentric lon (deg E) / lat (deg N) -> projected easting/northing (m)."""
    return (MARS_R * math.radians(lon_e) * math.cos(LAT_TS),
            MARS_R * math.radians(lat_n))


def xy_to_lonlat(x, y):
    return (math.degrees(x / (MARS_R * math.cos(LAT_TS))),
            math.degrees(y / MARS_R))


class Terrain:
    def __init__(self, dem_path, cache_dir=None, max_slope_deg=25.0,
                 cliff_drop_m=12.0):
        self.dem_path = dem_path
        self.max_slope_deg = max_slope_deg
        self.cliff_drop_m = cliff_drop_m
        cache_dir = cache_dir or os.path.dirname(os.path.abspath(dem_path))
        tag = f"s{max_slope_deg:g}_c{cliff_drop_m:g}"
        self.cache = os.path.join(cache_dir, f"jezero_layers_{tag}.npz")
        with rasterio.open(dem_path) as ds:
            self.transform = ds.transform
            self.res = ds.res[0]
            self.shape = (ds.height, ds.width)
            self.crs = ds.crs
        if os.path.exists(self.cache):
            self._load_cache()
        else:
            self._build()
            self._save_cache()

    # ------------------------------------------------------------ grid helpers
    def xy_to_rc(self, x, y):
        c, r = ~self.transform * (x, y)
        return int(round(r - 0.5)), int(round(c - 0.5))

    def rc_to_xy(self, r, c):
        """Fractional rows/cols are allowed (cell centre = integer index)."""
        x, y = self.transform * (c + 0.5, r + 0.5)
        return x, y

    def lonlat_to_rc(self, lon, lat):
        return self.xy_to_rc(*lonlat_to_xy(lon, lat))

    def rc_to_lonlat(self, r, c):
        return xy_to_lonlat(*self.rc_to_xy(r, c))

    def in_bounds(self, r, c):
        return 0 <= r < self.shape[0] and 0 <= c < self.shape[1]

    # ------------------------------------------------------------ layer build
    def _build(self):
        print("[terrain] reading DEM ...")
        with rasterio.open(self.dem_path) as ds:
            z = ds.read(1).astype(np.float32)
            nod = ds.nodata
        bad = ~np.isfinite(z) | (z < -1e30)
        if nod is not None:
            bad |= np.isclose(z, nod)
        # fill holes with nearest valid value so filters do not smear NaNs
        if bad.any():
            idx = ndimage.distance_transform_edt(bad, return_distances=False,
                                                 return_indices=True)
            z = z[tuple(idx)]
        h = self.res

        print("[terrain] slope ...")
        # Horn's method (same as gdaldem slope)
        zp = np.pad(z, 1, mode="edge")
        a, b, c_ = zp[:-2, :-2], zp[:-2, 1:-1], zp[:-2, 2:]
        d, f = zp[1:-1, :-2], zp[1:-1, 2:]
        g, hh, i = zp[2:, :-2], zp[2:, 1:-1], zp[2:, 2:]
        dzdx = ((c_ + 2 * f + i) - (a + 2 * d + g)) / (8 * h)
        dzdy = ((g + 2 * hh + i) - (a + 2 * b + c_)) / (8 * h)
        slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy))).astype(np.float32)

        print("[terrain] roughness ...")
        # residual after removing the local plane = z minus 3x3 mean, then local std
        mean3 = ndimage.uniform_filter(z, 3)
        resid = z - mean3
        rough = np.sqrt(np.maximum(ndimage.uniform_filter(resid ** 2, 5), 0)).astype(np.float32)

        print("[terrain] cliff relief ...")
        # biggest drop between a cell and anything within a 5x5 window (~40 m away)
        zmax = ndimage.maximum_filter(z, 5)
        zmin = ndimage.minimum_filter(z, 5)
        relief = np.maximum(zmax - z, z - zmin).astype(np.float32)

        # relief a steady slope would give across the 5x5 window (2 cells diagonally)
        span = 2 * math.sqrt(2) * h
        steady = span * np.tan(np.radians(slope))
        nogo = bad | (slope > self.max_slope_deg)
        # steeper than the EVA limit when measured over ~57 m
        nogo |= relief > span * math.tan(math.radians(self.max_slope_deg)) * 1.15
        # a cliff/scarp: a sharp step much bigger than the local slope explains
        nogo |= (relief > self.cliff_drop_m) & (relief > 1.6 * steady)
        nogo = ndimage.binary_opening(nogo, iterations=1) | bad  # drop single-pixel noise
        hazdist = (ndimage.distance_transform_edt(~nogo) * h).astype(np.float32)

        self.elev, self.slope, self.rough = z, slope, rough
        self.relief, self.nogo, self.hazdist, self.bad = relief, nogo, hazdist, bad

    def _save_cache(self):
        np.savez_compressed(self.cache, elev=self.elev, slope=self.slope,
                            rough=self.rough, relief=self.relief, nogo=self.nogo,
                            hazdist=self.hazdist, bad=self.bad)
        print(f"[terrain] cached layers -> {self.cache}")

    def _load_cache(self):
        d = np.load(self.cache)
        for k in ("elev", "slope", "rough", "relief", "nogo", "hazdist", "bad"):
            setattr(self, k, d[k])

    # ------------------------------------------------------------ visuals
    def hillshade(self, az=315.0, alt=40.0, step=1):
        z = self.elev[::step, ::step]
        h = self.res * step
        gy, gx = np.gradient(z, h)
        slope = np.pi / 2 - np.arctan(np.hypot(gx, gy))
        aspect = np.arctan2(-gx, gy)
        azr, altr = np.radians(az), np.radians(alt)
        hs = (np.sin(altr) * np.sin(slope) +
              np.cos(altr) * np.cos(slope) * np.cos(azr - np.pi / 2 - aspect))
        return np.clip(hs, 0, 1)
