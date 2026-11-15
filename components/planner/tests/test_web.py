"""Backend checks for the mission planner web UI (run: python tests/test_web.py)."""
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import test_spec as T                              # noqa: E402
from mission_web import MissionWeb                 # noqa: E402
from PIL import Image                              # noqa: E402


def build():
    return MissionWeb(lambda ms: T.dem(), synthetic=True)


def req_from_preset(p):
    sg = float(p.get("sigma", 5))
    r = {"start_enu": p["start"], "goal_enu": p["goal"], "mode": p["mode"], "profile": "min_eta", "refine": True,
         "localization": {"covariance_enu_m2": [[sg * sg, 0, 0], [0, sg * sg, 0], [0, 0, 1]]},
         "weather": {"start_hour": 9.0, "dust_events": []}, "sep": {}}
    if p.get("dust_tau"):
        pk = p.get("dust_peak_min", 35)
        r["weather"]["dust_events"] = [[pk - 60, pk + 60, p["dust_tau"]]]
    return r


def main():
    mw = build()
    m = mw.meta()
    e0, e1, n0, n1 = m["extent"]
    assert e0 < 0 < e1 and n0 < 0 < n1, m["extent"]
    print(f"meta ok: extent E[{e0:.0f},{e1:.0f}] N[{n0:.0f},{n1:.0f}], {len(m['presets'])} presets")
    for name in ["base"] + m["layers"]:
        body, ctype = mw.layer(name)
        im = Image.open(io.BytesIO(body))
        print(f"layer {name:15s} {ctype:11s} {im.size}  {len(body) / 1024:.0f} kB")
    pr = mw.probe(0, 0)
    assert not pr.get("out") and "slope" in pr, pr
    assert mw.probe(1e7, 0)["out"]
    print("probe ok:", pr)
    for p in m["presets"]:
        t0 = time.time()
        if p.get("sep_alert"):
            r = mw.replan({**req_from_preset(p), "trigger": "sep_alert", "current_enu": p["start"],
                           "sep": {"onset_min": p["sep_alert"]}})
            plan = r["plan"]
            ok = plan["verdict"].startswith("SHELTER")
        else:
            plan = mw.plan(req_from_preset(p))
            ok = plan["verdict"].startswith("GO")
        json.dumps(plan)
        print(f"{'OK  ' if ok else 'WARN'} preset {p['name'][:46]:46s} {plan['verdict'][:34]:34s} "
              f"{plan['distance_km']:5.1f} km ETA {plan['eta_min']:6.1f} min, {len(plan['route']['enu'])} pts, "
              f"{len(plan['fix_stop_detail'])} stops, {time.time() - t0:.1f}s")
    # hazards: plan, then block the middle of the route
    base = mw.plan(req_from_preset(m["presets"][0]))
    mid = base["route"]["enu"][len(base["route"]["enu"]) // 2]
    r = mw.replan({**req_from_preset(m["presets"][0]), "trigger": "new_hazard", "current_enu": [0, 0],
                   "hazards": [{"enu": mid, "radius_m": 120}]})
    print("replan:", r["info"]["response"], r["info"]["latency_s"], "s,", r["plan"]["verdict"])
    # an infeasible request must produce a readable error, not a crash
    try:
        mw.plan({**req_from_preset(m["presets"][0]), "goal_enu": [9000, 9000]})
        print("NOTE: far goal planned")
    except Exception as e:                              # noqa: BLE001
        print("infeasible request ->", type(e).__name__, str(e)[:100])


if __name__ == "__main__":
    main()
