#!/usr/bin/env python3
"""
Spec-compliant mission planner - command line.

    python plan_mission.py --goal 2500,1500                     # walk, min-ETA, from the lander
    python plan_mission.py --goal 7000,7000 --mode rover
    python plan_mission.py --goal -3200,2000 --sigma 60         # low start confidence -> wider buffers
    python plan_mission.py --goal 2500,1500 --dust 0,600,4.0    # dust storm (start,end min,peak tau)
    python plan_mission.py --sep-alert 90 --from 2000,1200      # SEP in 90 min: fastest route to shelter
    python plan_mission.py --goal 2500,1500 --json out.json     # spec 13 'Planner -> HUD' message
    python plan_mission.py --synthetic ...                      # no DEM download needed (test terrain)

Points are Site ENU metres east,north of the lander (the single frame the spec requires).
"""
import argparse
import json
import os
import tempfile
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEM = os.path.join(HERE, "data", "M20_JezeroCrater_CTXDEM_20m.tif")


def pt(s):
    x, y = (float(v) for v in s.split(","))
    return (x, y)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", type=pt, default=(0.0, 0.0), help="start ENU 'east,north' (default: lander)")
    ap.add_argument("--goal", type=pt, help="goal ENU 'east,north'")
    ap.add_argument("--mode", choices=["walk", "rover"], default="walk")
    ap.add_argument("--profile", choices=["min_eta", "balanced", "safest"], default="min_eta")
    ap.add_argument("--sigma", type=float, help="current 1-sigma position uncertainty in metres (default 5)")
    ap.add_argument("--dust", action="append", default=[], help="dust event 'start_min,end_min,peak_tau' (repeatable)")
    ap.add_argument("--sep-alert", type=float, metavar="MIN", help="SEP onset in MIN minutes: plan the shelter route")
    ap.add_argument("--no-refine", action="store_true", help="skip the fine-grid corridor refinement")
    ap.add_argument("--json", metavar="FILE", help="write the full planner message to FILE")
    ap.add_argument("--synthetic", action="store_true", help="use the built-in synthetic Jezero-like DEM")
    ap.add_argument("--dem", default=DEM)
    a = ap.parse_args()

    sys.path.insert(0, os.path.join(HERE, "tests"))
    dem = a.dem
    if a.synthetic:
        import synth_dem
        dem = os.path.join(tempfile.gettempdir(), "jezero_synth", "synth_700.tif")
        if not os.path.exists(dem):
            synth_dem.make(dem, n=700)
    elif not os.path.exists(dem):
        sys.exit(f"DEM not found: {dem}\nRun `python get_data.py` first, or use --synthetic.")

    from terrain import Terrain
    from mission_planner import MissionPlanner
    from environment import Weather, SEPAlert
    from localization import LocalizationState

    ev = [tuple(float(v) for v in d.split(",")) for d in a.dust]
    mp = MissionPlanner(Terrain(dem), weather=Weather(dust_events=ev),
                        sep=SEPAlert(onset_min=a.sep_alert) if a.sep_alert is not None else None)
    loc = None
    if a.sigma:
        loc = LocalizationState(covariance_enu_m2=[[a.sigma ** 2, 0, 0], [0, a.sigma ** 2, 0], [0, 0, 1]])
    if a.sep_alert is not None:
        p = mp.plan_shelter_route(a.start, a.mode, loc)
    else:
        if not a.goal:
            ap.error("--goal is required (or use --sep-alert)")
        p = mp.plan(a.start, a.goal, a.mode, a.profile, loc, refine=not a.no_refine)

    d = p.to_json()
    print(f"{d['verdict']}  [{p.mode}, {p.profile}]")
    print(f"  distance {d['distance_km']} km   ETA {d['eta_min']} min   energy {d['energy_kJ']} kJ   risk {d['risk_score']}")
    print(f"  confidence at goal: {d['confidence_at_goal']}   min return margin: {d['min_return_margin_frac']}")
    print(f"  turn-back point (E,N): {d['turn_back_point_enu_m']}   dose {d['dose_mSv']} mSv")
    print(f"  consumables left at goal: {d['consumables_at_goal']}")
    for s in d["fix_stops"]:
        print(f"  fix stop at waypoint {s['at_waypoint']}: {s['method']} ({s['duration_s']} s)")
    for al in d["alerts"]:
        print(f"  ALERT [{al['type']}] {al['msg']}")
    for v in d["violations"]:
        print(f"  VIOLATION {v}")
    print(f"  {p.stats.get('hierarchy')}; planning {p.stats.get('planning_s', p.stats.get('replan_s'))} s")
    if a.json:
        with open(a.json, "w") as f:
            json.dump(d, f, indent=1)
        print(f"  wrote {a.json}")


if __name__ == "__main__":
    main()
