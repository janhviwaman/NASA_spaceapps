"""
frames.py - the single Site ENU frame the spec requires (Section 4).

Every component shares one East-North-Up frame anchored at the surveyed lander
position. Geodetic (lat/lon) conversion happens only at the boundaries (loading data,
talking to a map UI). Local tangent plane about the anchor, Mars 2000 sphere:

    east  = R cos(lat0) * dlon        north = R * dlat        up = elev - elev0
"""
import math

import numpy as np

MARS_R = 3396190.0
# Perseverance landing site (Octavia E. Butler), used as the default surveyed lander pose
LANDER_LAT, LANDER_LON = 18.44463, 77.45089


class SiteFrame:
    def __init__(self, lat0=LANDER_LAT, lon0=LANDER_LON, elev0=0.0):
        self.lat0, self.lon0, self.elev0 = lat0, lon0, elev0
        self._ke = MARS_R * math.cos(math.radians(lat0)) * math.pi / 180.0
        self._kn = MARS_R * math.pi / 180.0

    # ---- geodetic <-> ENU (boundary conversions)
    def lonlat_to_enu(self, lon, lat):
        return ((np.asarray(lon) - self.lon0) * self._ke, (np.asarray(lat) - self.lat0) * self._kn)

    def enu_to_lonlat(self, e, n):
        return (self.lon0 + np.asarray(e) / self._ke, self.lat0 + np.asarray(n) / self._kn)

    # ---- grid <-> ENU, vectorised (rows/cols may be fractional)
    def rc_to_enu(self, terrain, rows, cols):
        rows = np.asarray(rows, dtype=np.float64)
        cols = np.asarray(cols, dtype=np.float64)
        t = terrain.transform
        x = t.a * (cols + 0.5) + t.b * (rows + 0.5) + t.c
        y = t.d * (cols + 0.5) + t.e * (rows + 0.5) + t.f
        from terrain import MARS_R as R, LAT_TS
        lon = np.degrees(x / (R * math.cos(LAT_TS)))
        lat = np.degrees(y / R)
        return self.lonlat_to_enu(lon, lat)

    def enu_to_rc(self, terrain, e, n):
        lon, lat = self.enu_to_lonlat(e, n)
        return terrain.lonlat_to_rc(float(lon), float(lat))

    def up(self, z):
        return np.asarray(z) - self.elev0

    def anchor_to_terrain(self, terrain):
        r, c = terrain.lonlat_to_rc(self.lon0, self.lat0)
        if terrain.in_bounds(r, c):
            self.elev0 = float(terrain.elev[r, c])
        return self
