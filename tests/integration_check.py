#!/usr/bin/env python3
"""End-to-end check of the Mars suite on one machine (stdlib only; needs the component dependencies).

It copies each component to a temporary folder (so nothing is written into the suite), starts
  - the planner on synthetic terrain        (port 18050)
  - the pathfinder on a synthetic DEM       (port 18051)
  - the MVP console                         (port 18100)
  - the HUD base server, if node is present (port 18790)
  - the planner-host Caddyfile, if caddy is present (port 18080), routing /, /map/, /pathfinder/
and checks each contract the parts rely on: the HUD request and response fields, the HUD mission
converters, the pathfinder lat/lon API, the MVP mission upload, the API key, CORS, and routing.

  python tests/integration_check.py [--caddy /path/to/caddy]
Exit status is 0 only if every check passes. Expect about 3-5 minutes on the first run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SUITE = Path(__file__).resolve().parents[1]
COMP = SUITE / "components"
KEY = "suite-test-key"
ORIGIN = "https://hud.example.github.io"
PORTS = {"planner": 18050, "pathfinder": 18051, "mvp": 18100, "base": 18790, "caddy": 18080}
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)


def http(url: str, *, method: str = "GET", body: dict | None = None, headers: dict | None = None,
         timeout: float = 240.0) -> tuple[int, dict[str, str], bytes]:
    data = json.dumps(body).encode() if body is not None else None
    hdrs = dict(headers or {})
    if data is not None:
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def http_no_redirect(url: str, timeout: float = 30.0) -> tuple[int, dict[str, str], bytes]:
    """Like http(), but returns the 3xx response itself instead of following it."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def wait_for(url: str, timeout: float = 300.0, headers: dict | None = None) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            status, _, _ = http(url, headers=headers, timeout=10)
            if status == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(3)
    return False


def copy_component(name: str, work: Path) -> Path:
    dst = work / name
    shutil.copytree(COMP / name, dst, ignore=shutil.ignore_patterns(
        "__pycache__", "node_modules", "output", "app_cache", "*.pyc"))
    return dst


def run(cmd: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env={**os.environ, **(env or {})}, capture_output=True,
                          text=True, timeout=300)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--caddy", default=shutil.which("caddy") or os.environ.get("CADDY", ""))
    ap.add_argument("--keep", action="store_true", help="keep the temporary workspace and logs")
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="mars-suite-check-"))
    logs = work / "logs"
    logs.mkdir()
    procs: list[tuple[str, subprocess.Popen]] = []
    py = sys.executable
    try:
        # ---------------------------------------------------------- start services
        planner = copy_component("planner", work)
        procs.append(("planner", subprocess.Popen(
            [py, "app.py", "--synthetic", "--host", "127.0.0.1", "--port", str(PORTS["planner"]), "--no-browser"],
            cwd=planner, stdout=open(logs / "planner.log", "w"), stderr=subprocess.STDOUT,
            env={**os.environ, "ALLOWED_ORIGIN": ORIGIN, "API_KEY": KEY})))

        pathfinder = copy_component("pathfinder", work)
        (pathfinder / "data").mkdir(exist_ok=True)
        dem_dir = work / "dem"
        sys.path.insert(0, str(COMP / "planner" / "tests"))
        import synth_dem  # type: ignore[import-not-found]
        synth_dem.make(str(pathfinder / "data" / "M20_JezeroCrater_CTXDEM_20m.tif"))
        procs.append(("pathfinder", subprocess.Popen(
            [py, "app.py", "--host", "127.0.0.1", "--port", str(PORTS["pathfinder"]), "--no-browser"],
            cwd=pathfinder, stdout=open(logs / "pathfinder.log", "w"), stderr=subprocess.STDOUT)))

        mvp = copy_component("mvp", work)
        procs.append(("mvp", subprocess.Popen(
            [py, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORTS["mvp"])],
            cwd=mvp, stdout=open(logs / "mvp.log", "w"), stderr=subprocess.STDOUT)))

        hud = copy_component("hud", work)
        node = shutil.which("node")

        print("waiting for services (the planner and pathfinder render their layers on first start)...", flush=True)
        planner_up = wait_for(f"http://127.0.0.1:{PORTS['planner']}/health")
        check("planner starts (synthetic terrain)", planner_up)
        pf_up = wait_for(f"http://127.0.0.1:{PORTS['pathfinder']}/meta", timeout=400)
        check("pathfinder starts (synthetic DEM)", pf_up)
        mvp_up = wait_for(f"http://127.0.0.1:{PORTS['mvp']}/api/health", timeout=60)
        check("MVP console starts", mvp_up)
        if not planner_up:
            print((logs / "planner.log").read_text()[-2000:])
            return 1

        # ------------------------------------------------ planner: auth, CORS, HUD contract
        s, _, _ = http(f"http://127.0.0.1:{PORTS['planner']}/mission/meta")
        check("planner API refuses requests without the key", s == 401, f"HTTP {s}")
        s, _, _ = http(f"http://127.0.0.1:{PORTS['planner']}/")
        check("planner page shell loads without the key", s == 200, f"HTTP {s}")
        s, _, body = http(f"http://127.0.0.1:{PORTS['planner']}/mission/meta", headers={"X-API-Key": KEY})
        meta = json.loads(body) if s == 200 else {}
        check("planner meta with the key lists destinations", s == 200 and bool(meta.get("presets")),
              f"{len(meta.get('presets', []))} presets, {len(meta.get('landmarks', []))} landmarks")
        s, hdrs, _ = http(f"http://127.0.0.1:{PORTS['planner']}/mission/plan", method="OPTIONS",
                          headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"})
        check("planner allows the HUD origin (CORS)",
              hdrs.get("Access-Control-Allow-Origin") == ORIGIN, hdrs.get("Access-Control-Allow-Origin", "none"))

        hud_body = {"start_enu": [0, 0], "goal_enu": [2500, 1500], "mode": "walk",
                    "profile": "min_eta", "refine": True, "slim": True}
        s, _, body = http(f"http://127.0.0.1:{PORTS['planner']}/mission/plan", method="POST",
                          body=hud_body, headers={"X-API-Key": KEY, "Origin": ORIGIN})
        plan = json.loads(body) if s == 200 else {}
        check("planner plans the HUD's request", s == 200 and not plan.get("error"), f"HTTP {s}")
        needed = ["route", "fix_stop_detail", "turn_back_point_enu_m", "verdict", "distance_km", "eta_min", "alerts"]
        missing = [k for k in needed if k not in plan]
        check("planner response has every field the HUD reads", not missing, f"missing {missing}" if missing else "")
        check("planner route has the HUD's arrays (enu, difficulty)",
              bool(plan.get("route", {}).get("enu")) and len(plan["route"].get("difficulty", [])) == len(plan["route"]["enu"]))
        check("slim response leaves out the explain block", "explain" not in plan)
        plan_path = work / "plan.json"
        plan_path.write_text(json.dumps(plan))

        # --------------------------------------------- HUD: converter and base server
        mission_path = work / "mission.json"
        r = run([py, "tools/mission_plan_to_mission.py", "--plan", str(plan_path), "--out", str(mission_path)], hud)
        check("HUD converter turns a planner route into a mission file", r.returncode == 0 and mission_path.exists(),
              r.stderr.strip()[-160:] or r.stdout.strip().splitlines()[0] if r.stdout else r.stderr.strip()[-160:])
        if mission_path.exists():
            m = json.loads(mission_path.read_text())
            check("mission file has the path, gates and caution zones the HUD draws",
                  all(k in m for k in ("path", "waypoints", "hazards", "brief")))
        if node and mission_path.exists():
            npm = shutil.which("npm")
            if npm:
                run([npm, "install", "--silent"], hud / "server")  # installs ws into the temporary copy
            base_env = {"PORT": str(PORTS["base"]), "MISSION": str(mission_path)}
            base_p = subprocess.Popen([node, "server.js"], cwd=hud / "server", env={**os.environ, **base_env},
                                      stdout=open(logs / "base.log", "w"), stderr=subprocess.STDOUT)
            procs.append(("base", base_p))
            ok = wait_for(f"http://127.0.0.1:{PORTS['base']}/mission.json", timeout=60)
            check("HUD base server starts and serves the mission", ok,
                  "needs `npm install` in components/hud/server" if not ok else "")
        else:
            check("HUD base server", True, "skipped: node not found" if not node else "skipped: no mission")

        # -------------------------------------------------- pathfinder: API and converter
        if pf_up:
            pf_plan = {"a_latlon": [18.4446, 77.4509], "b_latlon": [18.4700, 77.4900],
                       "profiles": ["balanced"], "max_slope": 25, "eva_hours": 8}
            s, _, body = http(f"http://127.0.0.1:{PORTS['pathfinder']}/plan", method="POST", body=pf_plan)
            pf = json.loads(body) if s == 200 else {}
            route = pf.get("routes", {}).get("balanced", {})
            check("pathfinder accepts lat/lon endpoints (API.md)", s == 200 and not pf.get("error"), f"HTTP {s}")
            check("pathfinder route includes latlon (API.md)", len(route.get("latlon", [])) > 1,
                  f"{len(route.get('latlon', []))} points")
            pf_path = work / "pf_plan.json"
            pf_path.write_text(json.dumps(pf))
            r = run([py, "tools/pathfinder_to_mission.py", "--plan", str(pf_path), "--profile", "balanced",
                     "--out", str(work / "pf_mission.json")], hud)
            check("HUD converter turns a pathfinder route into a mission file",
                  r.returncode == 0 and (work / "pf_mission.json").exists(), r.stderr.strip()[-160:])
            s, _, body = http(f"http://127.0.0.1:{PORTS['pathfinder']}/plan", method="POST",
                              body={"a": [350, 350], "b": [274, 459], "profiles": ["safest"], "max_slope": 25})
            check("pathfinder still accepts row/col requests (what its UI sends)", s == 200 and "routes" in json.loads(body))

        # ------------------------------------------------------ MVP: mission upload bridge
        if mvp_up:
            s, _, body = http(f"http://127.0.0.1:{PORTS['mvp']}/api/session", method="POST", body={})
            code = json.loads(body).get("join_code") if s == 200 else None
            check("MVP creates a session", bool(code), f"code {code}")
            if code:
                bridge = run([py, "tools/planner_to_mvp.py", "--plan", str(plan_path),
                              "--mvp", f"http://127.0.0.1:{PORTS['mvp']}", "--code", code], SUITE)
                check("planner route is uploaded to an MVP session", bridge.returncode == 0,
                      bridge.stderr.strip()[-160:] if bridge.returncode else "verified by read-back")
                s, _, body = http(f"http://127.0.0.1:{PORTS['mvp']}/api/session/{code}/mission")
                held = len(json.loads(body)["mission"]["waypoints_enu_m"]) if s == 200 else 0
                check("MVP holds the same waypoints as the planner", held == len(plan.get("waypoints_enu_m", [])),
                      f"{held} waypoints")

        # ---------------------------------------------------------- Caddy routing
        caddy_bin = args.caddy
        if caddy_bin and Path(caddy_bin).exists():
            caddyfile = SUITE / "deploy" / "planner-host" / "Caddyfile"
            cenv = {"PLANNER_DOMAIN": f":{PORTS['caddy']}",
                    "PLANNER_UPSTREAM": f"127.0.0.1:{PORTS['planner']}",
                    "PATHFINDER_UPSTREAM": f"127.0.0.1:{PORTS['pathfinder']}",
                    "MAP_ROOT": str(planner / "web"),
                    "XDG_DATA_HOME": str(work / "caddy-data"), "XDG_CONFIG_HOME": str(work / "caddy-config")}
            caddy_p = subprocess.Popen([caddy_bin, "run", "--config", str(caddyfile), "--adapter", "caddyfile"],
                                       env={**os.environ, **cenv}, stdout=open(logs / "caddy.log", "w"),
                                       stderr=subprocess.STDOUT)
            procs.append(("caddy", caddy_p))
            base = f"http://127.0.0.1:{PORTS['caddy']}"
            ok = wait_for(f"{base}/health", timeout=60)
            check("Caddy serves the planner host", ok)
            if ok:
                s, _, body = http(f"{base}/health")
                check("/ and /health route to the planner", s == 200 and b'"ok"' in body)
                s, _, body = http(f"{base}/map/meta.json")
                check("/map/ serves the static map files", s == 200 and b'"res"' in body, f"HTTP {s}")
                s, _, body = http(f"{base}/map/")
                check("/map/ returns the static map page", s == 200 and b"<html" in body.lower(), f"HTTP {s}")
                s, hdrs, _ = http_no_redirect(f"{base}/pathfinder")
                check("/pathfinder redirects to /pathfinder/", s in (301, 308) and "/pathfinder/" in hdrs.get("Location", ""),
                      f"HTTP {s} -> {hdrs.get('Location', '-')}")
                s, _, body = http(f"{base}/pathfinder/")
                check("/pathfinder/ serves the pathfinder UI with relative API paths",
                      s == 200 and b'fetch("meta"' in body and b'fetch("/meta"' not in body, f"HTTP {s}")
                s, _, body = http(f"{base}/pathfinder/meta")
                check("/pathfinder/meta is reached through the prefix", s == 200 and b'"res"' in body, f"HTTP {s}")
                s, _, _ = http(f"{base}/mission/meta", headers={"X-API-Key": KEY})
                check("API key passes through Caddy to the planner", s == 200, f"HTTP {s}")
        elif args.caddy:
            check("Caddy routing", False, f"caddy not found at {args.caddy}")
        else:
            print("[SKIP] Caddy routing: pass --caddy /path/to/caddy (https://caddyserver.com) to test it")

    finally:
        for name, p in reversed(procs):
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    p.kill()
        if args.keep:
            print(f"workspace and logs kept at {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
