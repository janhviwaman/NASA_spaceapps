#!/usr/bin/env python3
"""
Jezero EVA route planner - local explainable map app.

    python app.py                # opens http://localhost:8050 in your browser
    python app.py --port 9000    # different port
    python app.py --no-browser   # don't auto-open

Everything runs on this computer at the full 20 m DEM resolution.
"""
import argparse
import base64
import io
import json
import math
import os
import re
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import numpy as np
import pandas as pd
from PIL import Image

from terrain import Terrain
from planner import RoutePlanner, Astronaut, PROFILES
from plan import LANDMARKS, to_geojson, DEM
from viz import plot_routes, _hillshade

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "app_cache")
OUT = os.path.join(HERE, "output")
LOCK = threading.Lock()

STATE = {"terrains": {}, "last": None}
DEM_PATH = [DEM]          # replaced by --synthetic

Image.MAX_IMAGE_PIXELS = None


# --------------------------------------------------------------------- terrain
def terrain(max_slope):
    max_slope = float(max_slope)
    if max_slope not in STATE["terrains"]:
        print(f"[app] preparing terrain for max slope {max_slope:g} deg ...")
        STATE["terrains"][max_slope] = Terrain(DEM_PATH[0], max_slope_deg=max_slope)
    return STATE["terrains"][max_slope]


def _rgba_png(rgba, path):
    Image.fromarray(rgba, "RGBA").save(path, optimize=False, compress_level=6)


def _ramp(v, stops):
    """Map 0..1 values onto a list of (r,g,b) colour stops."""
    stops = np.array(stops, dtype=np.float32)
    x = np.clip(v, 0, 1) * (len(stops) - 1)
    i = np.minimum(x.astype(int), len(stops) - 2)
    f = (x - i)[..., None]
    return stops[i] * (1 - f) + stops[i + 1] * f


def layer_path(name, max_slope):
    os.makedirs(CACHE, exist_ok=True)
    tag = "" if name in ("hillshade", "relief", "slope", "rough") else f"_s{max_slope:g}"
    ext = "jpg" if name in ("hillshade", "relief") else "png"
    p = os.path.join(CACHE, f"{name}{tag}.{ext}")
    if os.path.exists(p):
        return p
    t = terrain(max_slope)
    print(f"[app] rendering layer '{name}' (one-off) ...")
    H, W = t.shape
    if name == "hillshade":
        hs = _hillshade(t.elev, t.res)
        Image.fromarray((np.clip(hs * 1.08, 0, 1) * 255).astype(np.uint8)).save(p, quality=82)
        return p
    if name == "relief":
        # natural-colour Mars relief: elevation ramp (umber low -> pale dust high) x hillshade
        hs = _hillshade(t.elev, t.res, exag=2.5)
        lo, hi = np.percentile(t.elev, [1, 99])
        v = np.clip((t.elev - lo) / (hi - lo), 0, 1) ** 0.8
        col = _ramp(v, [(66, 38, 28), (124, 72, 46), (176, 110, 70), (212, 158, 112), (236, 208, 170)])
        img = col * (0.38 + 0.78 * hs[..., None])
        Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).save(p, quality=84)
        return p
    rgba = np.zeros((H, W, 4), np.uint8)
    if name == "slope":
        v = (t.slope - 8) / 22
        rgba[..., :3] = _ramp(v, [(255, 236, 160), (250, 170, 70), (220, 60, 40), (120, 0, 40)])
        rgba[..., 3] = np.where(t.slope > 8, 120 + 110 * np.clip(v, 0, 1), 0)
    elif name == "rough":
        v = np.clip(t.rough / 3.0, 0, 1)
        rgba[..., :3] = _ramp(v, [(200, 230, 255), (120, 120, 255), (200, 40, 220)])
        rgba[..., 3] = np.where(t.rough > 0.6, 60 + 170 * v, 0)
    elif name == "hazard":
        v = 1 - np.clip(t.hazdist / 400.0, 0, 1)
        rgba[..., :3] = _ramp(v, [(255, 230, 120), (255, 120, 30), (220, 20, 20)])
        rgba[..., 3] = np.where(t.hazdist < 400, 40 + 170 * v, 0)
    elif name == "risk":
        soft = 15.0
        risk = (t.slope / soft) ** 3 + (t.rough / 1.5) ** 2 + 2 * np.exp(-t.hazdist / 80.0)
        v = np.clip(np.log1p(risk) / math.log1p(8), 0, 1)
        rgba[..., :3] = _ramp(v, [(40, 200, 120), (250, 220, 60), (240, 110, 30), (200, 20, 60)])
        rgba[..., 3] = (60 + 150 * v).astype(np.uint8)
    elif name == "nogo":
        rgba[t.nogo] = (150, 0, 40, 220)
    else:
        raise KeyError(name)
    _rgba_png(np.ascontiguousarray(rgba[::2, ::2]), p)   # overlays at 40 m keep files small
    return p


# --------------------------------------------------------------------- planning
def pack_cells(rows, cols, limit=250_000):
    step = max(1, int(math.ceil(len(rows) / limit)))
    arr = np.empty(2 * len(rows[::step]), np.uint16)
    arr[0::2] = rows[::step]
    arr[1::2] = cols[::step]
    return base64.b64encode(arr.tobytes()).decode(), step


def plan(req):
    ms = float(req.get("max_slope", 25))
    t = terrain(ms)
    astro = Astronaut(max_slope_deg=ms, eva_hours=float(req.get("eva_hours", 8)))
    planner = RoutePlanner(t, astro)
    a = tuple(int(round(v)) for v in req["a"])
    b = tuple(int(round(v)) for v in req["b"])
    for p in (a, b):
        if not t.in_bounds(*p):
            raise ValueError("Point is outside the elevation map.")
    profiles = req.get("profiles") or ["safest"]
    out = {"routes": {}, "terms": list(planner.TERM_NAMES)}
    results = {}
    for prof in profiles:
        t0 = time.time()
        rows, cols, rep = planner.plan(a, b, prof)
        secs = time.time() - t0
        bd = planner.breakdown(rows, cols, prof)
        ex_r, ex_c = rep["_explored"]
        packed, step = pack_cells(ex_r, ex_c)
        results[prof] = (rows, cols, rep)
        ri, ci = np.rint(rows).astype(int), np.rint(cols).astype(int)
        out["routes"][prof] = {
            "rows": np.round(rows, 2).tolist(), "cols": np.round(cols, 2).tolist(),
            "cum_km": np.round(rep["_cum_d"] / 1000, 4).tolist(),
            "elev": np.round(rep["_z"], 1).tolist(),
            "slope": np.round(t.slope[ri, ci], 1).tolist(),
            "hazd": np.round(np.minimum(t.hazdist[ri, ci], 9999), 0).tolist(),
            "rough": np.round(t.rough[ri, ci], 2).tolist(),
            "grade": np.round(bd["grade"], 1).tolist(),
            "terms": {k: np.round(v, 2).tolist() for k, v in bd["terms"].items()},
            "report": {k: v for k, v in rep.items() if not k.startswith("_")},
            "explored": packed, "explored_step": step,
            "seconds": round(secs, 2),
        }
    first = profiles[0]
    sl = planner.straight_line(a, b, first)
    out["straight"] = {"rows": np.round(sl["rows"], 2).tolist(), "cols": np.round(sl["cols"], 2).tolist(),
                       **{k: v for k, v in sl.items() if k not in ("rows", "cols")}}
    out["straight"]["route_cost"] = float(sum(sum(v) for v in out["routes"][first]["terms"].values()))
    STATE["last"] = (t, results, a, b)
    return out


def export_map():
    if not STATE["last"]:
        raise ValueError("Plan a route first.")
    t, results, a, b = STATE["last"]
    os.makedirs(OUT, exist_ok=True)
    a_ll = t.rc_to_lonlat(*a)[::-1]
    b_ll = t.rc_to_lonlat(*b)[::-1]
    name = time.strftime("route_%Y%m%d_%H%M%S")
    png = os.path.join(OUT, name + ".png")
    plot_routes(t, results, a_ll, b_ll, png, LANDMARKS)
    for p, (rows, cols, rep) in results.items():
        with open(os.path.join(OUT, f"{name}_{p}.geojson"), "w") as f:
            json.dump(to_geojson(t, rows, cols, rep), f)
    return {"png": f"/output/{name}.png", "path": png}


def meta():
    t = terrain(25.0)
    w = pd.read_csv(os.path.join(HERE, "data", "M20_waypoints.csv"))
    trav = [t.xy_to_rc(x, y) for x, y in zip(w.easting, w.northing)]
    lms = []
    for k, (lat, lon, desc) in LANDMARKS.items():
        r, c = t.lonlat_to_rc(lon, lat)
        lms.append({"key": k, "name": desc, "r": r, "c": c})
    tr = t.transform
    return {"H": t.shape[0], "W": t.shape[1], "res": t.res, "x0": tr.c, "y0": tr.f,
            "traverse": trav, "landmarks": lms, "profiles": PROFILES}


# --------------------------------------------------------------------- http
# ------------------------------------------------------------ mission planner (spec-compliant)
MW = None          # mission_web.MissionWeb, created in main()


def mission_plan(req):
    return MW.plan(req)


def mission_replan(req):
    return MW.replan(req)


def mission_rank(req):
    return MW.rank(req)


class Server(ThreadingHTTPServer):
    """On Windows, SO_REUSEADDR lets a SECOND server bind a port that is already in use, and the older one keeps
    answering: you open the page and silently talk to the old program. Refuse a busy port instead."""
    allow_reuse_address = (os.name != "nt")

    def server_bind(self):
        import socket
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json", cache=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype, cache=True)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "mission_ui.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif u.path == "/classic":
                with open(os.path.join(HERE, "app_ui.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif u.path == "/mission/meta":
                self._send(200, MW.meta())
            elif u.path.startswith("/mission/layer/"):
                body, ctype = MW.layer(u.path.split("/")[-1].split(".")[0])
                self._send(200, body, ctype, cache=True)
            elif u.path == "/ai/status":
                self._send(200, MW.ai_status())
            elif u.path == "/mission/probe":
                self._send(200, MW.probe(float(q["x"][0]), float(q["y"][0])))
            elif u.path == "/meta":
                with LOCK:
                    self._send(200, meta())
            elif u.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            elif u.path == "/probe":
                t = terrain(float(q.get("ms", ["25"])[0]))
                r, c = int(float(q["r"][0])), int(float(q["c"][0]))
                if not t.in_bounds(r, c):
                    self._send(200, {"out": True})
                else:
                    lon, lat = t.rc_to_lonlat(r, c)
                    self._send(200, {"lat": lat, "lon": lon, "elev": float(t.elev[r, c]),
                                     "slope": float(t.slope[r, c]), "rough": float(t.rough[r, c]),
                                     "hazd": float(t.hazdist[r, c]), "nogo": bool(t.nogo[r, c])})
            elif u.path.startswith("/layer/"):
                name = u.path.split("/")[-1].split(".")[0]
                ms = float(q.get("ms", ["25"])[0])
                with LOCK:
                    p = layer_path(name, ms)
                self._file(p, "image/jpeg" if p.endswith(".jpg") else "image/png")
            elif u.path.startswith("/output/"):
                fn = os.path.basename(u.path)
                if not re.fullmatch(r"[\w.\-]+", fn):
                    raise FileNotFoundError
                self._file(os.path.join(OUT, fn), "image/png" if fn.endswith(".png") else "application/json")
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            with LOCK:
                if u.path == "/plan":
                    self._send(200, plan(req))
                elif u.path == "/export":
                    self._send(200, export_map())
                elif u.path == "/mission/plan":
                    self._send(200, mission_plan(req))
                elif u.path == "/mission/replan":
                    self._send(200, mission_replan(req))
                elif u.path == "/mission/rank":
                    self._send(200, mission_rank(req))
                elif u.path == "/mission/compare":
                    self._send(200, MW.compare(req))
                elif u.path == "/ai/interpret":
                    self._send(200, MW.ai_interpret(req))
                elif u.path == "/ai/sites":
                    self._send(200, MW.ai_sites(req))
                else:
                    self._send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self._send(400, {"error": str(e)})


def main():
    global MW
    ap = argparse.ArgumentParser(description="Jezero mission planner: map UI for the spec-compliant planner")
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--synthetic", action="store_true",
                    help="use a built-in synthetic Jezero-like terrain (no 90 MB DEM download needed)")
    ap.add_argument("--classic", action="store_true", help="also pre-render the classic planner's map layers")
    args = ap.parse_args()
    if args.synthetic:
        import tempfile
        sys.path.insert(0, os.path.join(HERE, "tests"))
        import synth_dem
        p = os.path.join(tempfile.gettempdir(), "jezero_synth", "synth_700.tif")
        if not os.path.exists(p):
            synth_dem.make(p, n=700)
        DEM_PATH[0] = p
        print(f"[app] SYNTHETIC terrain: {p}")
    elif not os.path.exists(DEM_PATH[0]):
        sys.exit(f"DEM not found: {DEM_PATH[0]}\nRun `python get_data.py` first, or start with `python app.py --synthetic`.")
    from mission_web import MissionWeb
    MW = MissionWeb(terrain, synthetic=args.synthetic)
    print("[app] loading terrain ...")
    terrain(25.0)
    MW.mp                                   # build localizability layer + compile kernels now, not on first click
    MW.prewarm()                            # full-resolution base map renders in the background (cached on disk afterwards)
    if args.classic:
        for name in ("relief", "slope", "rough", "hazard", "risk", "nogo"):
            layer_path(name, 25.0)
    url = f"http://localhost:{args.port}"
    try:
        srv = Server(("127.0.0.1", args.port), Handler)
    except OSError:
        sys.exit(f"\nPort {args.port} is already in use. The planner is probably still running in another window "
                 f"(an older run).\nClose that window (press Ctrl+C in it), or start this one on another port, e.g. "
                 f"--port {args.port + 1}.")
    print(f"\n  Jezero mission planner running at {url}\n  terrain: {'SYNTHETIC test terrain' if args.synthetic else 'real Jezero CTX DEM (' + os.path.basename(DEM_PATH[0]) + ')'}"
          f"\n  (classic planner: {url}/classic;  press Ctrl+C to stop)\n")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[app] stopped")


if __name__ == "__main__":
    main()
