from __future__ import annotations
import asyncio, math, random, uuid
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .engine import SITES, resolve_request, make_plan, make_frame

app = FastAPI(title="Mars Surface Navigation Prototype", version="0.1.0")
BASE = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

sessions = {}
clients = {}

class MissionRequest(BaseModel):
    request: str = "Sample carbonates near the delta, two hours max."
    mode: str = "walk"
    time_budget_min: int = Field(default=120, ge=10, le=600)
    localization_quality: str = "high"

class PlanRequest(BaseModel):
    site_id: str
    mode: str = "walk"
    localization_quality: str = "high"

@app.get("/", response_class=HTMLResponse)
async def index():
    return (BASE / "static" / "index.html").read_text()

@app.get("/hud", response_class=HTMLResponse)
async def hud():
    return (BASE / "static" / "hud.html").read_text()

@app.get("/api/health")
async def health():
    return {"status": "ok", "prototype": True, "data_source": "synthetic"}

@app.get("/api/sites")
async def sites():
    return {"sites": SITES, "data_source": "synthetic"}

@app.post("/api/resolve")
async def resolve(req: MissionRequest):
    return resolve_request(req.request, req.mode, req.time_budget_min)

@app.post("/api/plan")
async def plan(req: PlanRequest):
    site = next((s for s in SITES if s["id"] == req.site_id), None)
    if not site:
        raise HTTPException(404, "Unknown site")
    return make_plan(site, req.mode, req.localization_quality)

@app.post("/api/session")
async def create_session():
    code = uuid.uuid4().hex[:6].upper()
    sid = str(uuid.uuid4())
    sessions[code] = {"id": sid, "code": code, "status": "planned", "t": 0.0,
                      "tau": 0.2, "sep": False, "quality": "high",
                      "plan": None, "mode": "walk", "running": False, "imu": None}
    return {"session_id": sid, "join_code": code, "hud_url": f"/hud?code={code}"}

@app.post("/api/session/{code}/configure")
async def configure(code: str, payload: dict):
    s = sessions.get(code.upper())
    if not s: raise HTTPException(404, "Session not found")
    plan = payload.get("plan")
    if plan is not None and not isinstance(plan, dict):
        raise HTTPException(400, "plan must be a JSON object")
    if plan is not None and not isinstance(plan.get("waypoints_enu_m", []), list):
        raise HTTPException(400, "waypoints_enu_m must be an array")
    s["plan"] = plan
    s["quality"] = payload.get("quality", "high")
    s["mode"] = payload.get("mode", "walk")
    return {"ok": True}

@app.get("/api/session/{code}/mission")
async def get_mission(code: str):
    s = sessions.get(code.upper())
    if not s: raise HTTPException(404, "Session not found")
    return {"mission": s.get("plan"), "session_code": code.upper(), "status": "loaded" if s.get("plan") else "awaiting_mission"}

@app.post("/api/session/{code}/mission")
async def upload_mission(code: str, payload: dict):
    """Accept a mission plan uploaded by the mission-design console/server."""
    s = sessions.get(code.upper())
    if not s: raise HTTPException(404, "Session not found")
    plan = payload.get("mission", payload.get("plan", payload))
    if not isinstance(plan, dict) or not isinstance(plan.get("waypoints_enu_m"), list) or len(plan["waypoints_enu_m"]) < 2:
        raise HTTPException(400, "Mission requires a waypoints_enu_m array with at least two points")
    for point in plan["waypoints_enu_m"]:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise HTTPException(400, "Each waypoint must contain [east_m, north_m]")
        try:
            float(point[0]); float(point[1])
        except (TypeError, ValueError):
            raise HTTPException(400, "Waypoint coordinates must be numeric")
    s["plan"] = plan
    s["mode"] = plan.get("mode", s.get("mode", "walk"))
    s["quality"] = plan.get("localization_quality", s.get("quality", "high"))
    return {"ok": True, "mission_id": plan.get("plan_id"), "waypoint_count": len(plan["waypoints_enu_m"])}

@app.post("/api/session/{code}/start")
async def start(code: str):
    s = sessions.get(code.upper())
    if not s: raise HTTPException(404, "Session not found")
    s["running"] = True
    return {"ok": True}

@app.post("/api/session/{code}/pause")
async def pause(code: str):
    s = sessions.get(code.upper())
    if not s: raise HTTPException(404, "Session not found")
    s["running"] = False
    return {"ok": True}

@app.post("/api/session/{code}/event")
async def event(code: str, payload: dict):
    s = sessions.get(code.upper())
    if not s: raise HTTPException(404, "Session not found")
    kind = payload.get("kind")
    if kind == "dust":
        s["tau"] = 2.5
    elif kind == "sep":
        s["sep"] = True
    elif kind == "clear":
        s["tau"] = 0.2
        s["sep"] = False
    else:
        raise HTTPException(400, "Event must be dust, sep, or clear")
    return {"ok": True, "event": kind}

@app.post("/api/session/{code}/imu")
async def receive_imu(code: str, payload: dict):
    """Store the latest browser orientation sample for inclusion in HUD frames."""
    s = sessions.get(code.upper())
    if not s: raise HTTPException(404, "Session not found")
    def bounded_number(key, low, high, optional=False):
        value = payload.get(key)
        if value is None and optional: return None
        try: value = float(value)
        except (TypeError, ValueError): raise HTTPException(400, f"{key} must be numeric")
        if not math.isfinite(value) or not low <= value <= high:
            raise HTTPException(400, f"{key} must be between {low} and {high}")
        return round(value, 2)
    heading = bounded_number("heading_deg", 0, 360, optional=True)
    s["imu"] = {"pitch_deg": bounded_number("pitch_deg", -180, 180),
                 "roll_deg": bounded_number("roll_deg", -180, 180),
                 "heading_deg": heading, "mode": str(payload.get("mode", "both"))[:16],
                 "calibrated": bool(payload.get("calibrated", False)),
                 "source": "deviceorientation"}
    return {"ok": True, "received": True}

@app.get("/api/session/{code}/state")
async def state(code: str):
    s = sessions.get(code.upper())
    if not s: raise HTTPException(404, "Session not found")
    frame = make_frame(s)
    frame["imu"] = s.get("imu")
    return frame

@app.websocket("/ws/{code}")
async def websocket_endpoint(ws: WebSocket, code: str):
    code = code.upper()
    if code not in sessions:
        await ws.close(code=1008)
        return
    await ws.accept()
    clients.setdefault(code, set()).add(ws)
    try:
        while True:
            s = sessions[code]
            if s["running"]:
                s["t"] += 1.0
                if 20 <= s["t"] < 40 and s["tau"] < 1.0:
                    s["tau"] = 0.2 + (s["t"] - 20) / 20 * 2.3
                elif s["t"] >= 40 and s["tau"] > 0.2 and s["tau"] < 2.5:
                    s["tau"] = 2.5
                frame = make_frame(s)
                frame["imu"] = s.get("imu")
                await ws.send_json(frame)
            else:
                frame = make_frame(s)
                frame["imu"] = s.get("imu")
                await ws.send_json(frame)
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        clients.get(code, set()).discard(ws)

