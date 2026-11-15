#!/usr/bin/env python3
"""Convert a Jezero Pathfinder route into an EVA HUD mission file.

Two ways to use it (Python 3.8+, no extra packages):

  # 1. Ask a running planner (python app.py --no-browser) for a route
  python tools/pathfinder_to_mission.py --api http://localhost:8050 \
      --from 18.44463,77.45089 --to 18.45753,77.26853 --profile safest --out mission.json

  # 2. Convert a saved /plan response
  python tools/pathfinder_to_mission.py --plan plan_response.json --profile safest --out mission.json

The start of the route becomes the local origin (0,0). x is metres east, y is metres north.
"""
import argparse, json, math, sys, urllib.request

R = 3396190.0                      # Mars 2000 sphere radius used by the planner (m)
PHI0 = math.radians(18.4663)       # standard parallel of the planner's projection


def to_xy(lat, lon):
    return R * math.cos(PHI0) * math.radians(lon), R * math.radians(lat)


def rdp(pts, eps):
    """Douglas-Peucker simplification (iterative). pts = [(x, y), ...]."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]; bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy) or 1e-9
        best, bi = -1.0, None
        for i in range(a + 1, b):
            d = abs(dy * (pts[i][0] - ax) - dx * (pts[i][1] - ay)) / L
            if d > best:
                best, bi = d, i
        if best > eps:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def fetch_plan(api, a, b, profile, max_slope, eva_hours):
    body = json.dumps({"a_latlon": a, "b_latlon": b, "profiles": [profile],
                       "max_slope": max_slope, "eva_hours": eva_hours}).encode()
    req = urllib.request.Request(api.rstrip("/") + "/plan", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def build_mission(plan, profile, to_name="Goal", eps=3.0):
    route = plan["routes"][profile]
    ll = route["latlon"]
    xy = [to_xy(*p) for p in ll]
    ox, oy = xy[0]
    rel = [(x - ox, y - oy) for x, y in xy]
    path = [{"x": round(x, 1), "y": round(y, 1)} for x, y in rdp(rel, eps)]
    rep = route.get("report", {})

    wps = [{"x": 0.0, "y": 0.0, "name": "Start"}]
    for i, rs in enumerate(rep.get("rest_stops", []), 1):
        x, y = to_xy(rs["lat"], rs["lon"])
        wps.append({"x": round(x - ox, 1), "y": round(y - oy, 1),
                    "name": f"Rest stop {i} ({round(rs.get('after_min', 0))} min)"})
    wps.append({"x": path[-1]["x"], "y": path[-1]["y"], "name": to_name})

    cum = route.get("cum_km", [])
    hazards = []
    for seg in rep.get("caution_segments", []):
        idx = [i for i, k in enumerate(cum) if seg["from_km"] <= k <= seg["to_km"]]
        if not idx:
            continue
        m = rel[idx[len(idx) // 2]]
        r = max(12.0, min(40.0, (seg["to_km"] - seg["from_km"]) * 500.0))
        hazards.append({"x": round(m[0], 1), "y": round(m[1], 1), "r": round(r),
                        "name": str(seg.get("why", "caution"))[:40], "kind": "caution"})

    brief = rep.get("verdict", "")
    if "distance_km" in rep:
        brief += f"  |  {rep['distance_km']:.1f} km, {rep.get('walking_time_h', 0):.1f} h walking"
    return {"name": f"{profile.capitalize()} route", "brief": brief.strip(),
            "path": path, "waypoints": wps, "hazards": hazards}


def latlon(s):
    a, b = s.split(",")
    return [float(a), float(b)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", help="saved /plan response (json)")
    ap.add_argument("--api", help="planner base URL, e.g. http://localhost:8050")
    ap.add_argument("--from", dest="a", type=latlon, help="start lat,lon")
    ap.add_argument("--to", dest="b", type=latlon, help="goal lat,lon")
    ap.add_argument("--to-name", default="Goal")
    ap.add_argument("--profile", default="safest", choices=["safest", "balanced", "fastest"])
    ap.add_argument("--max-slope", type=float, default=25)
    ap.add_argument("--eva-hours", type=float, default=8)
    ap.add_argument("--simplify", type=float, default=3.0, help="path simplification in metres")
    ap.add_argument("--out", default="mission.json")
    a = ap.parse_args()
    if a.plan:
        plan = json.load(open(a.plan))
    elif a.api and a.a and a.b:
        plan = fetch_plan(a.api, a.a, a.b, a.profile, a.max_slope, a.eva_hours)
    else:
        ap.error("give --plan FILE, or --api URL with --from and --to")
    if a.profile not in plan.get("routes", {}):
        sys.exit(f"profile '{a.profile}' not in the plan. Found: {list(plan.get('routes', {}))}")
    m = build_mission(plan, a.profile, a.to_name, a.simplify)
    json.dump(m, open(a.out, "w"), indent=1)
    print(f"{a.out}: {len(m['path'])} path points, {len(m['waypoints'])} waypoints, {len(m['hazards'])} caution zones")
    if m["brief"]:
        print(m["brief"])


if __name__ == "__main__":
    main()
