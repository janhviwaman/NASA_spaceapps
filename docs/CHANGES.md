# What was combined, fixed, and removed

## Sources

| Upload | Used as | Notes |
|---|---|---|
| `files.zip` (`mission-planner-server`) | planner server patch, planner deployment files | Patched `app.py`: CORS, `--host`, `API_KEY`, `slim`, `/health` |
| `files.zip` (`mars-eva-hud`) | HUD, base server, converters, docs | Newer than the top-level HUD zip (it has the planner integration) |
| `mars-eva-hud.zip` | not used | Older HUD, superseded by the copy inside `files.zip` |
| `jezero-mission-planner-main.zip` | planner | Full planner, map, lander terminal |
| `jezero-pathfinder-main__1_.zip` | pathfinder | Older lat/lon planner. Shares the planner's terrain code and data |
| `convert-main.zip` (`mars_hud_work`) | MVP console | FastAPI prototype with its own HUD |

## Fixes made during combination

1. **Pathfinder: lat/lon input was documented but not implemented.** `API.md` says `a_latlon`/`b_latlon` are accepted. The handler required `a`/`b`. Added `_endpoint()`, which accepts either form.
2. **Pathfinder: `latlon` route field was documented but not returned.** The HUD's pathfinder converter crashed on every response. Routes now include `latlon`, computed from the same cells.
3. **Pathfinder: listened only on 127.0.0.1.** Added `--host` and `PORT` (the same options as the planner patch).
4. **Pathfinder: absolute URLs.** Its UI and output paths started with `/`, so they broke under `/pathfinder/`. Changed to relative paths.
5. **Pathfinder: duplicate map folder.** `web/` was byte-identical to the planner's. Removed it; the planner host serves the map at `/map/`.
6. **Planner: its own page returned 401 when an API key was set.** The page shell is now public; the API stays keyed.
7. **Shared terrain cache.** The planner and pathfinder build different cache formats, so they must not share a cache folder. The compose file gives each its own cache volume.
8. **Removed:** the MVP's scratch `nano code .rtf` (a copy of its server code with an unauthenticated `/message` endpoint that prints to the console), the MVP's old `render.yaml` (wrong `rootDir`; replaced by the root blueprint), and a stray empty file named `git` in the planner.

## Verified and not verified

Verified in the build environment (see KNOWN_LIMITS for the exact scope): planner, pathfinder, and MVP
start and serve their APIs; the HUD's request and response fields match; the converters work on real
planner and pathfinder output; the planner-to-MVP upload round-trips; the HUD scripts parse; the base
server serves a mission; the planner-host Caddyfile validates and routes (see the integration run log).
