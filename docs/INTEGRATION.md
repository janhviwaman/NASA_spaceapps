# Integration contracts

All parts use Site ENU metres (east, north from the lander). The planner and the HUD already
agree on this, and the converters only change the container shape.

## 1. HUD to planner (`components/hud`, Route planner menu)

`GET  {planner}/mission/meta`  (header `X-API-Key` when the planner has a key)
returns `landmarks[]` and `presets[]`, used to fill the destination list.

`POST {planner}/mission/plan`
```json
{"start_enu": [0, 0], "goal_enu": [2500, 1500], "mode": "walk",
 "profile": "min_eta", "refine": true, "slim": true,
 "localization": {"covariance_enu_m2": [[25, 0, 0], [0, 25, 0], [0, 0, 1]]}}
```
`profile` is `min_eta`, `balanced`, or `safest`. `localization` is optional. `slim` leaves out the
large `explain` block. The HUD reads these response fields: `route.enu`, `route.difficulty`,
`fix_stop_detail`, `turn_back_point_enu_m`, `verdict`, `distance_km`, `eta_min`, `alerts`.
A `verdict` beginning `NO-GO` or `HOLD` means the planner refused the route.

## 2. Planner route to HUD mission file

`python components/hud/tools/mission_plan_to_mission.py --plan plan.json --out mission.json`

The file is `{name, brief, path[{x,y}], waypoints[{x,y,name}], hazards[{x,y,r,name,kind}], alerts[]}`.
The base server serves it at `/mission.json`, and the HUD's **Load mission file** button reads it.

## 3. Pathfinder

`POST {pathfinder}/plan`. The body accepts either `a` / `b` as `[row, col]` or `a_latlon` /
`b_latlon` as `[lat, lon]` (planetocentric degrees), plus `profiles`, `max_slope`, `eva_hours`.
Each returned route includes `latlon: [[lat, lon], ...]`, which the converter reads:
`python components/hud/tools/pathfinder_to_mission.py --plan plan.json --profile balanced --out mission.json`

## 4. Planner route to MVP session (main path)

`POST {mvp}/api/session` creates a session and returns `join_code`.
`POST {mvp}/api/session/{code}/mission` with `{"mission": {plan_id, name, mode, eta_min, distance_m, waypoints_enu_m: [[x, y, z], ...]}}`.
`tools/planner_to_mvp.py` maps a planner response to this body, refuses `NO-GO` and `HOLD` routes
unless `--force` is given, and checks the upload by reading it back.

## 5. Authentication

The MVP's HUD (`/hud?code=...`) is served by the MVP over HTTPS on Render. The planner is never called from the phone in this path.

The planner's API routes require `X-API-Key` when `API_KEY` is set. The planner's page shell
(`/`, `/classic`) is public, so the page loads, but its planning buttons need the key. The
MVP's mission upload has no authentication (see KNOWN_LIMITS).
