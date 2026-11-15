#!/usr/bin/env python3
"""
Jezero crater astronaut route planner - command line.

Examples
  python plan.py --from landing --to rim_top
  python plan.py --from 18.4446,77.4509 --to 18.4653,77.3550 --profile balanced
  python plan.py --from landing --to delta_front --compare
  python plan.py --list

Coordinates are "lat,lon" in degrees (planetocentric, longitude East),
or a landmark name from --list.
"""
import argparse
import json
import os
import sys

import numpy as np

from terrain import Terrain
from planner import RoutePlanner, Astronaut, PROFILES
from viz import plot_routes

HERE = os.path.dirname(os.path.abspath(__file__))
DEM = os.path.join(HERE, "data", "M20_JezeroCrater_CTXDEM_20m.tif")

# Places Perseverance actually stood (lat, lon) from NASA's M20 waypoint log
LANDMARKS = {
    "landing":       (18.44463, 77.45089, "Octavia E. Butler landing site (sol 13)"),
    "crater_floor":  (18.43327, 77.44176, "Crater floor south of landing (sol 202)"),
    "delta_front":   (18.44982, 77.40136, "Front of the western delta (sol 420)"),
    "delta_top":     (18.47437, 77.39743, "Top of the delta (sol 717)"),
    "upper_fan":     (18.48965, 77.34722, "Upper fan, west of delta (sol 950)"),
    "rim_base":      (18.49114, 77.30093, "Foot of the western rim climb (sol 1244)"),
    "rim_top":       (18.45753, 77.26853, "Top of the western crater rim (sol 1354)"),
    "beyond_rim":    (18.42760, 77.23515, "Outside the rim (sol 1710)"),
}


def parse_point(s, t):
    if s in LANDMARKS:
        lat, lon, _ = LANDMARKS[s]
    else:
        try:
            lat, lon = (float(v) for v in s.split(","))
        except ValueError:
            sys.exit(f"Can't read point '{s}'. Use lat,lon or one of: {', '.join(LANDMARKS)}")
    r, c = t.lonlat_to_rc(lon, lat)
    if not t.in_bounds(r, c):
        sys.exit(f"Point {s} ({lat},{lon}) is outside the Jezero DEM.")
    return (r, c), (lat, lon)


def to_geojson(t, rows, cols, rep):
    coords = [[round(v, 6) for v in t.rc_to_lonlat(r, c)] + [round(float(z), 1)]
              for r, c, z in zip(rows, cols, rep["_z"])]
    props = {k: v for k, v in rep.items() if not k.startswith("_")}
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
         "properties": props}]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="a")
    ap.add_argument("--to", dest="b")
    ap.add_argument("--profile", default="safest", choices=list(PROFILES))
    ap.add_argument("--compare", action="store_true", help="run all three profiles")
    ap.add_argument("--max-slope", type=float, default=25.0)
    ap.add_argument("--eva-hours", type=float, default=8.0)
    ap.add_argument("--suit-mass", type=float, default=60.0)
    ap.add_argument("--out", default=os.path.join(HERE, "output"))
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        for k, (lat, lon, d) in LANDMARKS.items():
            print(f"  {k:12s} {lat:9.5f} {lon:9.5f}  {d}")
        return
    if not (args.a and args.b):
        ap.error("--from and --to are required (or use --list)")

    t = Terrain(DEM, max_slope_deg=args.max_slope)
    astro = Astronaut(max_slope_deg=args.max_slope, eva_hours=args.eva_hours,
                      suit_mass_kg=args.suit_mass)
    planner = RoutePlanner(t, astro)
    a_rc, a_ll = parse_point(args.a, t)
    b_rc, b_ll = parse_point(args.b, t)

    os.makedirs(args.out, exist_ok=True)
    profiles = list(PROFILES) if args.compare else [args.profile]
    results = {}
    for p in profiles:
        rows, cols, rep = planner.plan(a_rc, b_rc, p)
        results[p] = (rows, cols, rep)
        name = f"{args.a}_to_{args.b}_{p}".replace(",", "_")
        with open(os.path.join(args.out, name + ".geojson"), "w") as f:
            json.dump(to_geojson(t, rows, cols, rep), f)
        print(f"\n=== {p.upper()} ===")
        print(f"  verdict        : {rep['verdict']}")
        print(f"  distance       : {rep['distance_km']} km (straight line {rep['straight_line_km']} km)")
        print(f"  walking time   : {rep['walking_time_h']} h  (EVA budget after reserve {rep['eva_budget_h']} h)")
        print(f"  energy         : {rep['energy_kJ']:.0f} kJ  (~{rep['energy_kJ']/4.184:.0f} kcal)")
        print(f"  climb / descent: +{rep['ascent_m']} m / -{rep['descent_m']} m")
        print(f"  steepest grade : up {rep['max_uphill_grade_deg']} deg, down {rep['max_downhill_grade_deg']} deg")
        print(f"  nearest hazard : {rep['closest_approach_to_hazard_m']:.0f} m")
        for s in rep["rest_stops"]:
            print(f"  rest stop      : km {s['after_km']} (~{s['after_min']} min) at {s['lat']},{s['lon']}, slope {s['slope_deg']} deg")
        for c in rep["caution_segments"]:
            print(f"  CAUTION        : km {c['from_km']}-{c['to_km']}: {c['why']}")
    png = os.path.join(args.out, f"{args.a}_to_{args.b}.png".replace(",", "_"))
    plot_routes(t, results, a_ll, b_ll, png, LANDMARKS)
    print(f"\nMap saved: {png}")


if __name__ == "__main__":
    main()
