#!/usr/bin/env python3
"""Send a planner route to the MVP console's session (stdlib only).

The MVP exposes POST /api/session/{code}/mission for external mission designers. This tool is
the bridge: it takes a route from the Jezero mission planner and uploads it to a session.

  # from a saved planner response (POST /mission/plan)
  python tools/planner_to_mvp.py --plan plan.json --mvp https://mvp.example.com --code K7Q2MX

  # plan live, create a session, and upload in one step
  python tools/planner_to_mvp.py --api https://planner.example.com --from 0,0 --to 2500,1500 \
      --mode walk --profile min_eta --mvp http://localhost:8000 --create-session

Both systems use Site ENU metres (east, north from the lander), so no conversion is needed.
A route whose verdict is NO-GO or HOLD is not uploaded unless --force is given.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.request

REFUSED_PREFIXES = ("NO-GO", "HOLD")


def _post(url: str, body: dict, key: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(url, json.dumps(body).encode(), headers, method="POST")
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)


def _get(url: str, key: str | None = None) -> dict:
    headers = {"X-API-Key": key} if key else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
        return json.load(resp)


def mission_body(plan: dict) -> dict:
    """Map a planner /mission/plan response to the MVP's mission object."""
    if plan.get("error"):
        raise ValueError(f"planner error: {plan['error']}")
    points = plan.get("waypoints_enu_m") or []
    if not points:
        raise ValueError("planner returned no waypoints")
    waypoints = []
    for p in points:
        x, y = float(p[0]), float(p[1])
        z = float(p[2]) if len(p) > 2 else 0.0
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            raise ValueError("planner returned a non-finite waypoint")
        waypoints.append([round(x, 2), round(y, 2), round(z, 2)])
    eta = plan.get("eta_min")
    dist_km = plan.get("distance_km")
    return {
        "plan_id": f"planner-{plan.get('profile', 'plan')}-{len(waypoints)}",
        "name": f"{str(plan.get('mode', 'walk')).capitalize()} route, {plan.get('profile', '')}".strip(", "),
        "mode": plan.get("mode", "walk"),
        "eta_min": round(float(eta), 1) if eta is not None else None,
        "distance_m": round(float(dist_km) * 1000) if dist_km is not None else None,
        "waypoints_enu_m": waypoints,
    }


def check_verdict(plan: dict, force: bool) -> None:
    verdict = str(plan.get("verdict", ""))
    if verdict.startswith(REFUSED_PREFIXES) and not force:
        raise SystemExit(f"not uploaded: planner verdict is '{verdict}' (use --force to override)")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--plan", help="saved planner /mission/plan response (JSON file)")
    src.add_argument("--api", help="planner base URL, to plan live")
    ap.add_argument("--from", dest="start", default="0,0", help="start x,y metres (live planning)")
    ap.add_argument("--to", dest="goal", help="goal x,y metres (live planning)")
    ap.add_argument("--mode", default="walk", choices=["walk", "rover"])
    ap.add_argument("--profile", default="min_eta", choices=["min_eta", "balanced", "safest"])
    ap.add_argument("--key", help="planner API key, if the planner has one")
    ap.add_argument("--mvp", required=True, help="MVP base URL, for example http://localhost:8000")
    ap.add_argument("--code", help="join code of an existing MVP session")
    ap.add_argument("--create-session", action="store_true", help="create a new session first")
    ap.add_argument("--force", action="store_true", help="upload even if the verdict is NO-GO or HOLD")
    ap.add_argument("--dry-run", action="store_true", help="print the mission body and stop")
    args = ap.parse_args(argv)

    if args.plan:
        with open(args.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
    else:
        if not args.goal:
            ap.error("--to is required with --api")
        body = {"start_enu": [float(v) for v in args.start.split(",")],
                "goal_enu": [float(v) for v in args.goal.split(",")],
                "mode": args.mode, "profile": args.profile, "refine": True, "slim": True}
        plan = _post(args.api.rstrip("/") + "/mission/plan", body, args.key)

    check_verdict(plan, args.force)
    mission = mission_body(plan)
    print(f"route: {len(mission['waypoints_enu_m'])} waypoints, ETA {mission['eta_min']} min, "
          f"verdict {plan.get('verdict', '?')}")
    if args.dry_run:
        print(json.dumps({"mission": mission}, indent=2))
        return 0

    base = args.mvp.rstrip("/")
    code = args.code
    if args.create_session:
        code = _post(base + "/api/session", {})["join_code"]
        print(f"created session {code}")
    if not code:
        ap.error("give --code, or --create-session")
    result = _post(f"{base}/api/session/{code}/mission", {"mission": mission})
    back = _get(f"{base}/api/session/{code}/mission")
    count = len(back.get("mission", {}).get("waypoints_enu_m", []))
    if count != len(mission["waypoints_enu_m"]):
        raise SystemExit(f"upload check failed: sent {len(mission['waypoints_enu_m'])}, MVP holds {count}")
    print(f"uploaded to session {code}: {result}")
    print(f"HUD: {base}/hud?code={code}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
