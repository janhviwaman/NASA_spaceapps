#!/usr/bin/env python3
"""Convert a Jezero mission planner /mission/plan response into an EVA HUD mission file.

  # ask a running planner
  python tools/mission_plan_to_mission.py --api https://planner.example.com --key KEY \
      --goal 2500,1500 --mode walk --profile min_eta --out mission.json
  # or convert a saved response
  python tools/mission_plan_to_mission.py --plan response.json --out mission.json

Both the planner and the HUD use Site ENU metres (x east, y north of the lander), so no map projection is needed.
"""
import argparse, json, math, sys, urllib.request


def rdp(pts, eps):
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts); keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        dx, dy = pts[b][0] - pts[a][0], pts[b][1] - pts[a][1]
        L = math.hypot(dx, dy) or 1e-9
        best, bi = -1.0, None
        for i in range(a + 1, b):
            d = abs(dy * (pts[i][0] - pts[a][0]) - dx * (pts[i][1] - pts[a][1])) / L
            if d > best:
                best, bi = d, i
        if best > eps:
            keep[bi] = True; stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def build(p, mode, prof):
    R = p.get("route") or {}
    pts = R.get("enu") or p["waypoints_enu_m"]
    r1 = lambda v: round(v, 1)
    near = lambda q: min(range(len(pts)), key=lambda i: (pts[i][0] - q[0]) ** 2 + (pts[i][1] - q[1]) ** 2)
    g = [(0, {"x": r1(pts[0][0]), "y": r1(pts[0][1]), "name": "Start"})]
    for n, f in enumerate(p.get("fix_stop_detail", []), 1):
        g.append((near(f["enu"]), {"x": r1(f["enu"][0]), "y": r1(f["enu"][1]),
                                   "name": f"Fix stop {n} ({f['method']}, {round(f['duration_s'])} s)"}))
    if p.get("turn_back_point_enu_m"):
        t = p["turn_back_point_enu_m"]
        g.append((near(t), {"x": r1(t[0]), "y": r1(t[1]), "name": "Turn-back point"}))
    e = pts[-1]
    g.append((len(pts) - 1, {"x": r1(e[0]), "y": r1(e[1]), "name": "Goal"}))
    g.sort(key=lambda t: t[0])

    hz, diff, i = [], R.get("difficulty"), 0
    while diff and i < len(pts):
        if diff[i] != "hard":
            i += 1; continue
        j = i
        while j + 1 < len(pts) and diff[j + 1] == "hard":
            j += 1
        m = pts[(i + j) // 2]
        ln = math.hypot(pts[j][0] - pts[i][0], pts[j][1] - pts[i][1])
        hz.append({"x": r1(m[0]), "y": r1(m[1]), "r": round(max(12, min(40, ln / 2))), "name": "Hard terrain", "kind": "caution"})
        i = j + 1

    brief = "  |  ".join(x for x in [p.get("verdict", ""), f"{p['distance_km']:.1f} km" if "distance_km" in p else "",
                                     f"ETA {round(p['eta_min'])} min" if "eta_min" in p else ""] if x)
    return {"name": f"{'Rover' if mode == 'rover' else 'Walk'}, {prof}", "brief": brief,
            "path": [{"x": r1(x), "y": r1(y)} for x, y in rdp(pts, 3.0)],
            "waypoints": [w for _, w in g], "hazards": hz, "alerts": p.get("alerts", [])}


def xy(s):
    a, b = s.split(","); return [float(a), float(b)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan"); ap.add_argument("--api"); ap.add_argument("--key", default="")
    ap.add_argument("--start", type=xy, default=[0.0, 0.0]); ap.add_argument("--goal", type=xy)
    ap.add_argument("--mode", default="walk", choices=["walk", "rover"])
    ap.add_argument("--profile", default="min_eta", choices=["min_eta", "balanced", "safest"])
    ap.add_argument("--sigma", type=float, help="position error in metres (optional)")
    ap.add_argument("--out", default="mission.json")
    a = ap.parse_args()
    if a.plan:
        plan = json.load(open(a.plan))
    elif a.api and a.goal:
        body = {"start_enu": a.start, "goal_enu": a.goal, "mode": a.mode, "profile": a.profile, "refine": True, "slim": True}
        if a.sigma:
            s2 = a.sigma ** 2; body["localization"] = {"covariance_enu_m2": [[s2, 0, 0], [0, s2, 0], [0, 0, 1]]}
        h = {"Content-Type": "application/json"}
        if a.key: h["X-API-Key"] = a.key
        req = urllib.request.Request(a.api.rstrip("/") + "/mission/plan", json.dumps(body).encode(), h)
        try:
            plan = json.load(urllib.request.urlopen(req, timeout=300))
        except urllib.error.HTTPError as e:
            sys.exit(f"Planner refused the request: {e.read().decode()[:300]}")
    else:
        ap.error("give --plan FILE, or --api URL with --goal x,y")
    m = build(plan, a.mode, a.profile)
    json.dump(m, open(a.out, "w"), indent=1)
    print(f"{a.out}: {len(m['path'])} path points, {len(m['waypoints'])} gates, {len(m['hazards'])} caution zones")
    print(m["brief"])


if __name__ == "__main__":
    main()
