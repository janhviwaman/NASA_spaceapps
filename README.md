# Mars surface navigation suite

Repository: https://github.com/janhviwaman/NASA_spaceapps

The main product is the **MVP console and its crew HUD**. The HUD is a web page served by the
MVP, so it runs on Render with HTTPS (the phone's motion sensors need HTTPS). The planner computes
the route, and the MVP delivers it to the phone. Everything else supports that path.

| Part | Role | Where it runs | Needs the others? |
|---|---|---|---|
| **MVP console + HUD** (`components/mvp`) | **Main.** Session console, crew HUD at `/hud?code=...`, IMU controls, mission upload | Render (`render.yaml` at the repo root) | No |
| **Planner** (`components/planner`) | Route planner: map, lander terminal, uncertainty-aware A*, replanning, JSON API | Planner host (Docker + Caddy, HTTPS) | No. Feeds routes to the MVP through `tools/planner_to_mvp.py` |
| **Map** (`components/planner/web`) | Static terrain map, no server | Planner host at `/map/` | No |
| **Pathfinder** (`components/pathfinder`) | Older lat/lon planner with its own map | Planner host at `/pathfinder/` | No |
| **Secondary HUD** (`components/hud`) | Phone HUD (race line, IMU view, Route planner menu) | GitHub Pages, from this repository | No |
| **Base server** (`components/hud/server`) | Optional Node WebSocket server for the secondary HUD | Any Node host | No |

## Main flow

```
planner (route) --tools/planner_to_mvp.py--> MVP session --> phone opens /hud?code=... on the MVP
```

1. Plan the route on the planner (map or API). Check the verdict is GO.
2. Upload it to an MVP session: `python tools/planner_to_mvp.py --api https://planner.example.com --to 2500,1500 --mvp https://<your-mvp>.onrender.com --create-session`
3. On the phone, open the printed `/hud?code=...` link on the MVP's HTTPS address.

The secondary HUD loads routes from the planner itself or from mission files, so it works without the MVP.

## Quick start (one machine, no Docker)

Requires Python 3.11+ and Node 18+. The first planner start renders its map layers (about a minute).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r components/planner/requirements.txt -r components/mvp/requirements.txt
(cd components/mvp && uvicorn app.main:app --host 0.0.0.0 --port 8000)     # MVP: http://localhost:8000
(cd components/planner && python app.py --synthetic --host 127.0.0.1 --port 8050)   # planner
python tools/planner_to_mvp.py --api http://127.0.0.1:8050 --to 2500,1500 --mvp http://127.0.0.1:8000 --create-session
```

To check the whole chain: `python tests/integration_check.py --caddy /path/to/caddy`.
Read [docs/HOSTING.md](docs/HOSTING.md) for deployment and [docs/INTEGRATION.md](docs/INTEGRATION.md) for the contracts.

## Repository layout

```
components/mvp/          main: MVP console + crew HUD (FastAPI)
components/planner/      planner, lander terminal, map, Dockerfile
components/pathfinder/   legacy lat/lon planner (Dockerfile)
components/hud/          secondary phone HUD (static) and its base server (components/hud/server)
shared-data/             terrain data for planner and pathfinder (the 90 MB DEM is downloaded here)
deploy/planner-host/     docker-compose.yml, Caddyfile, .env.example for the planner host
tools/                   planner_to_mvp.py (main bridge). HUD converters are in components/hud/tools
.github/workflows/pages.yml   publishes the secondary HUD to GitHub Pages
render.yaml              Render blueprint for the MVP (main)
tests/integration_check.py   end-to-end check of every contract on one machine
docs/                    hosting, integration contracts, changes, known limits
```

## Read before relying on it

[docs/KNOWN_LIMITS.md](docs/KNOWN_LIMITS.md). The real Jezero DEM, Docker images, Render, GitHub
Pages, and phone sensors were not run in the build environment. The MVP's sites are placeholders,
and its mission upload has no authentication yet.
