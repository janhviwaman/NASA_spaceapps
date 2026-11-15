"""
localization.py - the localization model the planner couples to (spec Sections 7, 8.2, 9.2, 13).

The planner does not run the sensors; it needs to know, for any candidate path,
  * how the position uncertainty (sigma) will grow and shrink along it, and
  * where a fix is possible and how good it would be (the per-cell localizability layer).

Contents
  LocParams            tunable estimates from spec 7.1 (all marked est. in the spec)
  confidence_level     sigma -> high / medium / low / lost   (spec 7.5)
  sigma_move / best_fix  the numba sigma kernels shared by the search and the path verifier
  LocalizabilityLayer  skyline score, orbital-match score, sky openness per cell (spec 8.2)
  LocalizationState    the 'Localization -> planner and HUD' contract (spec 13)
"""
import math
import os
from dataclasses import dataclass, field

import numpy as np
from numba import njit, prange
from scipy import ndimage

# method codes used in fix-stop records
METHODS = {0: "none", 1: "orbital_match", 2: "skyline", 3: "celestial"}

CONF_BOUNDS = ((5.0, "high"), (30.0, "medium"), (300.0, "low"))   # spec 7.5


def confidence_level(sigma_m):
    for bound, name in CONF_BOUNDS:
        if sigma_m < bound:
            return name
    return "lost"


@dataclass
class LocParams:
    sigma0_m: float = 5.0            # cold start: surveyed lander pose + fix (SYS-4)
    vio_drift: float = 0.015         # VIO drift, ~1-2 % of distance (spec 7.1)
    vio_drift_sand: float = 0.035    # larger on sand (spec 9.2)
    sigma_orbital_m: float = 3.0     # ground-to-orbital image match, 1-5 m
    sigma_skyline_m: float = 25.0    # skyline match, 10-50 m
    sigma_celestial_m: float = 150.0 # celestial, 100-500 m (SYS-5: <= 300 m)
    fix_ref_dist_m: float = 100.0    # en-route orbital matching: one 'full' fix per this distance
    fix_stop_s: float = 60.0         # stop-and-fix duration, 30-90 s (spec 9.3)
    stand_power_w: float = 120.0     # metabolic power while standing still
    n_sigma: float = 3.0             # hazard buffer = n_sigma * predicted sigma ...
    base_buffer_m: float = 10.0      # ... plus this fixed margin (half a 20 m cell)
    sigma_levels: tuple = (2.5, 4.0, 6.5, 10.0, 16.0, 26.0, 42.0, 70.0, 120.0, 300.0)
    min_stop_gain: float = 0.8       # a fix stop must cut sigma to <= this fraction
    sigma_lost_m: float = 300.0      # beyond this the estimate is 'lost' (spec 7.5)
    min_fix_quality: float = 0.25    # camera methods need this much localizability x visibility
    min_celestial_quality: float = 0.10
    allow_fix_stops: bool = True

    def as_array(self):
        return np.array([self.vio_drift, self.vio_drift_sand, self.sigma_orbital_m,
                         self.sigma_skyline_m, self.sigma_celestial_m, self.fix_ref_dist_m,
                         self.fix_stop_s, self.n_sigma, self.base_buffer_m, self.stand_power_w,
                         1.0 if self.allow_fix_stops else 0.0, self.min_stop_gain,
                         self.sigma_lost_m, self.min_fix_quality, self.min_celestial_quality],
                        dtype=np.float64)

    def levels(self):
        return np.array(self.sigma_levels, dtype=np.float64)


# LP array indices (keep in sync with LocParams.as_array)
L_VIO, L_VIOS, L_SORB, L_SSKY, L_SCEL, L_REF, L_STOPS, L_NSIG, L_BASE, L_STAND, L_ALLOW, \
    L_GAIN, L_LOST, L_QMIN, L_QCEL = range(15)


# ---------------------------------------------------------------- sigma kernels
@njit(cache=True)
def sigma_move(sig, d, sand, loc_sky, loc_orb, cam_q, LP):
    """Predicted sigma after moving d metres onto a cell (spec 9.2).

    1. drift: sigma grows linearly with distance (VIO error is correlated, so we add
       k*d rather than root-sum-square; this is the conservative choice), faster on sand;
    2. continuous ground-to-orbital image matching (spec 7.1 B2) adds information in
       proportion to the cell's orbital-match score, the distance moved and the
       dust-limited camera quality.
    Skyline (panorama) and celestial fixes are NOT available on the move: spec 9.3 takes them
    at stationary pauses, so they only enter through `best_fix` at fix stops.
    """
    k = LP[L_VIO] + (LP[L_VIOS] - LP[L_VIO]) * sand
    s1 = sig + k * d
    w = d / LP[L_REF]
    info = (loc_orb * cam_q) * w / (LP[L_SORB] * LP[L_SORB])
    return 1.0 / math.sqrt(1.0 / (s1 * s1) + info)


@njit(cache=True)
def best_fix(loc_sky, loc_orb, sky_open, cam_q, cel_av, LP):
    """Best stationary fix available here and now. Returns (sigma_meas, method code)."""
    best = 1e18
    meth = 0
    q = loc_orb * cam_q
    if q >= LP[L_QMIN]:
        s = LP[L_SORB] / math.sqrt(q)
        if s < best:
            best, meth = s, 1
    q = loc_sky * cam_q
    if q >= LP[L_QMIN]:
        s = LP[L_SSKY] / math.sqrt(q)
        if s < best:
            best, meth = s, 2
    q = sky_open * cel_av
    if q >= LP[L_QCEL]:
        s = LP[L_SCEL] / math.sqrt(q)
        if s < best:
            best, meth = s, 3
    return best, meth


@njit(cache=True)
def fuse(sig, sig_meas):
    return 1.0 / math.sqrt(1.0 / (sig * sig) + 1.0 / (sig_meas * sig_meas))


@njit(cache=True)
def bucket(sig, LV):
    """Smallest sigma level >= sig (-1 if beyond the last level = 'lost')."""
    for k in range(LV.shape[0]):
        if LV[k] >= sig:
            return k
    return -1


# ---------------------------------------------------------------- horizons
@njit(parallel=True, cache=True)
def _horizons(elev, res, rows, cols, naz, dstep, rmax, eye):
    ni, nj = rows.shape[0], cols.shape[0]
    H, W = elev.shape
    out = np.empty((ni, nj, naz), dtype=np.float32)
    nsteps = int(rmax / dstep)
    for ii in prange(ni):
        r0 = rows[ii]
        for jj in range(nj):
            c0 = cols[jj]
            z0 = elev[r0, c0] + eye
            for a in range(naz):
                az = 2.0 * math.pi * a / naz
                dr = -math.cos(az) / res       # north-up raster: north = decreasing row
                dc = math.sin(az) / res
                best = -1e9
                for s in range(1, nsteps + 1):
                    dist = s * dstep
                    rr = int(r0 + dr * dist + 0.5)
                    cc = int(c0 + dc * dist + 0.5)
                    if rr < 0 or rr >= H or cc < 0 or cc >= W:
                        break
                    t = (elev[rr, cc] - z0) / dist
                    if t > best:
                        best = t
                if best < -1e8:
                    best = 0.0
                out[ii, jj, a] = math.degrees(math.atan(best))
    return out


class LocalizabilityLayer:
    """Per-cell localizability (spec 8.2), precomputed offline from the DEM.

    skyline  : how unique the DEM-rendered horizon is from that cell compared with its
               neighbours (horizon change per 100 m of movement) x how much horizon relief
               there is (a flat horizon is ambiguous).
    orbital  : rock / crater feature density around the cell. ESTIMATE from DEM roughness
               until HiRISE rock abundance is loaded (`set_orbital_score`).
    sky_open : fraction of the horizon below 15 deg; limits star / Sun availability.

    Celestial availability additionally depends on time of day and dust, so it is NOT
    stored per cell: the planner multiplies `sky_open` by the weather model's time-varying
    celestial availability.
    """

    def __init__(self, terrain, stride=10, naz=36, rmax_m=4000.0, orb_rough_ref=0.15,
                 cache_dir=None, verbose=True):
        self.stride, self.naz = int(stride), naz
        self.res = terrain.res
        self.shape = terrain.shape
        stem = os.path.splitext(os.path.basename(terrain.dem_path))[0] if terrain.dem_path else "dem"
        tag = f"loc_{stem}_{terrain.shape[0]}x{terrain.shape[1]}_s{self.stride}_a{naz}_r{rmax_m:g}_o{orb_rough_ref:g}"
        cdir = cache_dir or (os.path.dirname(os.path.abspath(terrain.dem_path))
                             if terrain.dem_path else None)
        path = os.path.join(cdir, tag + ".npz") if cdir else None
        if path and os.path.exists(path):
            d = np.load(path)
            self.sky, self.orb, self.open = d["sky"], d["orb"], d["open"]
            return
        if verbose:
            print("[localizability] horizons ...")
        S = self.stride
        rows = np.arange(0, terrain.shape[0], S, dtype=np.int64)
        cols = np.arange(0, terrain.shape[1], S, dtype=np.int64)
        Hn = _horizons(terrain.elev.astype(np.float32), float(terrain.res), rows, cols, naz,
                       float(2 * terrain.res), float(rmax_m), 1.7)
        # skyline: sensitivity of the horizon to position + horizon relief
        diffs = []
        for ax in (0, 1):
            for sh in (1, -1):
                sh_arr = np.roll(Hn, sh, axis=ax)
                dd = np.sqrt(np.mean((Hn - sh_arr) ** 2, axis=2))
                edge = [slice(None), slice(None)]
                edge[ax] = slice(0, 1) if sh == 1 else slice(-1, None)
                dd[tuple(edge)] = np.nan          # wrapped neighbour is not a neighbour
                diffs.append(dd)
        sens = np.nanmean(np.stack(diffs), axis=0) / (S * terrain.res / 100.0)   # deg / 100 m
        sens = np.nan_to_num(sens, nan=np.nanmedian(sens))
        relief = Hn.std(axis=2)
        self.sky = ((1 - np.exp(-sens / 0.5)) * (1 - np.exp(-relief / 1.5))).astype(np.float32)
        self.open = (Hn < 15.0).mean(axis=2).astype(np.float32)
        # orbital-match proxy: local feature density from roughness + relief
        w = max(3, int(300.0 / terrain.res))
        feat = ndimage.uniform_filter(terrain.rough, w) + 0.02 * ndimage.uniform_filter(terrain.relief, w)
        self.orb = (1 - np.exp(-feat[::S, ::S] / orb_rough_ref)).astype(np.float32)
        if path:
            os.makedirs(cdir, exist_ok=True)
            np.savez_compressed(path, sky=self.sky, orb=self.orb, open=self.open)

    def set_orbital_score(self, full_res_array):
        """Replace the DEM proxy with a real HiRISE-derived score (same grid as the DEM)."""
        self.orb = np.asarray(full_res_array, np.float32)[::self.stride, ::self.stride]

    def sample(self, rows, cols):
        """(sky, orb, open) at arbitrary points given in 20 m-grid index coordinates."""
        S = self.stride
        pts = [np.asarray(rows, float) / S, np.asarray(cols, float) / S]
        return tuple(ndimage.map_coordinates(a, pts, order=1, mode="nearest")
                     for a in (self.sky, self.orb, self.open))

    def window(self, row_coords, col_coords):
        """Resample (sky, orb, open) onto the planning grid.

        row_coords / col_coords are 1-D arrays of 20 m-grid index coordinates (fractional
        allowed), so the same call serves the coarse grid and the finer refinement grids."""
        S = self.stride
        R, C = np.meshgrid(np.asarray(row_coords) / S, np.asarray(col_coords) / S, indexing="ij")
        out = []
        for a in (self.sky, self.orb, self.open):
            out.append(ndimage.map_coordinates(a, [R, C], order=1, mode="nearest").astype(np.float32))
        return tuple(out)


# ---------------------------------------------------------------- contract (spec 13)
@dataclass
class LocalizationState:
    """'Localization -> planner and HUD' message (spec 13)."""
    position_enu_m: tuple = (0.0, 0.0, 0.0)
    heading_deg: float = 0.0
    covariance_enu_m2: list = field(default_factory=lambda: [[25.0, 0, 0], [0, 25.0, 0], [0, 0, 25.0]])
    confidence: str = "high"
    last_absolute_fix: dict = field(default_factory=lambda: {"source": "lander_survey", "age_s": 0})
    distance_since_fix_m: float = 0.0
    timestamp_utc: str = ""
    position_geodetic: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d):
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)

    def sigma_m(self):
        """Horizontal 1-sigma (largest east/north eigenvalue), used as the planner's start sigma."""
        c = np.asarray(self.covariance_enu_m2, dtype=float)[:2, :2]
        return float(math.sqrt(max(np.linalg.eigvalsh(c).max(), 1e-6)))
