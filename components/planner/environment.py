"""
environment.py - time-varying conditions and life-support budgets (spec 8.1, 9.1, 9.5, 10).

  Weather       dust opacity / temperature / wind over time -> per-time-bin cost multipliers
                and camera / star availability (dust affects BOTH route cost and sensors)
  SEPAlert      solar particle event: onset time and dose rate (spec 9.5)
  Consumables   O2, power, cooling water, CO2 scrubber (or rover battery) and the
                'return to shelter must always be feasible' margin (SYS-9)
  Dose          RAD baseline plus SEP dose

All numbers marked (est.) are engineering estimates, as in the spec. Sun position uses an
analytic Mars ephemeris good to ~0.2 deg; swap in SpiceyPy + NAIF kernels when online.
"""
import math
from dataclasses import dataclass, field

import numpy as np

MARS_AXIAL_TILT = 25.19
BASELINE_DOSE_MSV_PER_DAY = 0.65      # RAD baseline, spec Section 5 (~0.6-0.7)


def sun_elevation_deg(lat_deg, hour, ls_deg):
    """Solar elevation at local mean solar time `hour` (0-24) for solar longitude Ls."""
    dec = math.asin(math.sin(math.radians(MARS_AXIAL_TILT)) * math.sin(math.radians(ls_deg)))
    ha = math.radians(15.0 * (hour - 12.0))
    lat = math.radians(lat_deg)
    s = math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


@dataclass
class Weather:
    start_hour: float = 9.0            # local mean solar time when the plan starts
    lat_deg: float = 18.4
    ls_deg: float = 150.0              # season
    tau_background: float = 0.5        # dust opacity, quiet conditions
    dust_events: list = field(default_factory=list)   # (start_min, end_min, peak_tau), triangular
    temp_mean_c: float = -55.0         # (est.) Jezero diurnal swing
    temp_amp_c: float = 30.0
    wind_ms: float = 6.0
    wind_limit_ms: float = 25.0        # (est.)
    temp_min_eva_c: float = -95.0      # (est.) suit thermal limit: no EVA colder than this
    tau_max_eva: float = 4.5           # (est.) beyond this recommend wait / return
    window_end_min: float = math.inf   # weather time window for the EVA (spec 9.1)

    # ---- instantaneous quantities
    def tau_at(self, t_min):
        tau = self.tau_background
        for (a, b, peak) in self.dust_events:
            if a <= t_min <= b:
                mid = 0.5 * (a + b)
                f = 1 - abs(t_min - mid) / max(0.5 * (b - a), 1e-9)
                tau = max(tau, self.tau_background + (peak - self.tau_background) * f)
        return tau

    def hour_at(self, t_min):
        return (self.start_hour + t_min / 60.0 / 1.0275) % 24.0   # sol = 24.66 h

    def temp_c(self, t_min):
        return self.temp_mean_c + self.temp_amp_c * math.cos(2 * math.pi * (self.hour_at(t_min) - 13.0) / 24.0)

    def sun_el(self, t_min):
        return sun_elevation_deg(self.lat_deg, self.hour_at(t_min), self.ls_deg)

    def celestial_availability(self, t_min):
        """0..1: can a star / Sun fix be taken now? Stars need darkness AND clear air; the
        Sun disc survives much more dust than faint stars (spec 7.1 / 14)."""
        tau = self.tau_at(t_min)
        el = self.sun_el(t_min)
        dark = min(max((-6.0 - el) / 6.0, 0.0), 1.0)                 # stars: Sun < -6 deg
        star = dark * math.exp(-max(tau - 0.5, 0.0) / 1.0)
        sun = (1.0 if el > 5.0 else max(el / 5.0, 0.0)) * math.exp(-max(tau - 0.5, 0.0) / 3.0)
        return max(star, 0.6 * sun)                                  # Sun fix is a bit coarser

    def camera_quality(self, t_min):
        return math.exp(-max(self.tau_at(t_min) - 0.5, 0.0) / 1.5)

    def speed_cost_mult(self, t_min):
        """>= 1. Dust slows the crew (visibility) and raises risk margins (est.)."""
        return 1.0 + 0.15 * max(self.tau_at(t_min) - 0.5, 0.0)

    def energy_cost_mult(self, t_min):
        """>= 1. Cold suits and dust-clogged joints cost more energy (est.)."""
        cold = max(-60.0 - self.temp_c(t_min), 0.0)
        return 1.0 + 0.05 * max(self.tau_at(t_min) - 0.5, 0.0) + 0.004 * cold

    def sampled(self, bin_s=300.0, horizon_s=16 * 3600.0):
        """Per-time-bin arrays for the numba search: rows = (dust/speed mult, energy mult,
        camera quality, celestial availability)."""
        n = int(horizon_s / bin_s) + 1
        out = np.empty((4, n))
        for i in range(n):
            tm = (i + 0.5) * bin_s / 60.0
            out[:, i] = (self.speed_cost_mult(tm), self.energy_cost_mult(tm),
                         self.camera_quality(tm), self.celestial_availability(tm))
        return out, bin_s

    def hard_limits(self, t_start_min=0.0, duration_min=0.0):
        """Weather alerts / constraints over [t_start, t_start+duration] (spec 8.1, 14).
        Sampled every 5 minutes (a storm peak must not slip between coarse samples)."""
        alerts, ok = [], True
        t1 = t_start_min + max(duration_min, 1.0)
        ts = np.append(np.arange(t_start_min, t1, 5.0), t1)
        tau = np.array([self.tau_at(float(t)) for t in ts])
        if tau.max() > self.tau_max_eva:
            alerts.append({"type": "dust", "msg": f"dust opacity reaches {tau.max():.1f} (EVA limit {self.tau_max_eva:g}): "
                           "wait or return", "t_min": round(float(ts[int(tau.argmax())]), 1)}); ok = False
        temp = np.array([self.temp_c(float(t)) for t in ts])
        if temp.min() < self.temp_min_eva_c:
            alerts.append({"type": "temperature", "msg": "suit thermal limit",
                           "t_min": round(float(ts[int(temp.argmin())]), 1)}); ok = False
        if self.wind_ms > self.wind_limit_ms:
            alerts.append({"type": "wind", "msg": "wind above limit"}); ok = False
        return ok, alerts


@dataclass
class SEPAlert:
    """Solar particle event. `onset_min` is minutes from plan start (0 = already under way)."""
    onset_min: float = math.inf
    dose_rate_msv_h: float = 2.0       # (est.) unshielded surface dose rate during the event
    shelter_factor: float = 0.1        # dose reduction inside the shelter / lander (est.)
    return_margin_min: float = 10.0    # arrive at shelter this long before onset

    @property
    def active(self):
        return math.isfinite(self.onset_min)


def dose_msv(duration_s, sep=None, t_start_s=0.0):
    """Baseline GCR dose over a duration plus SEP dose accrued outdoors after onset."""
    dose = BASELINE_DOSE_MSV_PER_DAY * duration_s / 86400.0
    if sep is not None and sep.active:
        exposed = max(0.0, (t_start_s + duration_s) - max(sep.onset_min * 60.0, t_start_s))
        dose += sep.dose_rate_msv_h * exposed / 3600.0
    return dose


@dataclass
class Consumables:
    """Life-support budgets (spec Section 10). Capacities are defined by a design EVA so the
    numbers are traceable: `design_eva_h` hours at `design_metabolic_w` of metabolic load.

      O2, CO2 scrubber, cooling water  scale with metabolic energy (walking) or with time at
                                       rest (rover: the crew sits in a cabin)
      suit power                       scales with time
      rover battery                    scales with drive energy (rover only)
    """
    design_eva_h: float = 8.0
    design_metabolic_w: float = 250.0
    rest_metabolic_w: float = 100.0
    suit_power_w: float = 300.0          # (est.)
    rover_battery_kwh: float = 15.0      # (est.)
    rover_cabin_power_w: float = 150.0   # life support in the pressurised cabin (est.)
    reserve_frac: float = 0.10           # untouchable fraction of every resource
    return_margin: float = 0.25          # return requirement is multiplied by 1 + this (SYS-9)
    on_site_s: float = 1800.0            # time spent working at the destination

    # kg per joule of metabolic energy (O2: 20.1 kJ per litre, 1.429 g per litre)
    K_O2 = 1.429e-3 / 20100.0
    K_CO2 = 1.0e-3 / 20100.0 * 1.2
    K_H2O = 3.5e-7

    NAMES = ("O2", "CO2 scrubber", "cooling water", "power / battery")

    def coeffs(self, mode):
        """(R, 3) rows of [per-second, per-joule, capacity] for each resource. Usage of a
        resource over a leg is  a*time + b*energy_J  where energy_J is the planner's
        per-edge energy (metabolic for walking, battery for the rover)."""
        t = self.design_eva_h * 3600.0
        rows = []
        if mode == "walk":
            for k in (self.K_O2, self.K_CO2, self.K_H2O):
                a, b = self.rest_metabolic_w * k, k
                rows.append([a, b, t * (a + b * self.design_metabolic_w)])
            rows.append([self.suit_power_w, 0.0, t * self.suit_power_w])
        else:
            for k in (self.K_O2, self.K_CO2, self.K_H2O):
                a = self.rest_metabolic_w * k
                rows.append([a, 0.0, t * a])
            rows.append([0.0, 1.0, self.rover_battery_kwh * 3.6e6])
        return np.array(rows, dtype=np.float64)
