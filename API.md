# Planner API

`python app.py` starts a small HTTP server (default `http://localhost:8050`).
The bundled map UI uses it, and any other front-end can too: a HUD, a game engine,
a phone app. CORS is open, so a page served from another port can call it directly.

To reach it from other devices on the same network, use `python app.py --host 0.0.0.0`.

## Coordinates

The terrain is a grid of **5067 rows × 4456 columns**, 20 m per cell
(Mars 2000 sphere, equirectangular, standard parallel 18.4663° N).
Row 0 is the north edge and column 0 the west edge.

You can work in grid cells (`row, col`) or in planetocentric **latitude / longitude
(degrees N / degrees E)**. `/plan` accepts and returns both.

```
x = x0 + (col + 0.5) * 20           # easting, metres
y = y0 - (row + 0.5) * 20           # northing, metres
lon = degrees(x / (3396190 * cos(radians(18.4663))))
lat = degrees(y / 3396190)
```
`x0` and `y0` come from `/meta`.

---

## `GET /meta`
Map size, the grid transform, named landmarks and Perseverance's track.
```json
{
  "H": 5067, "W": 4456, "res": 20.0,
  "x0": 4329000.0, "y0": 1143420.0,
  "landmarks": [{"key": "landing", "name": "Octavia E. Butler landing site (sol 13)", "r": 2506, "c": 1274}, ...],
  "traverse": [[row, col], ...],          // 708 Perseverance positions, sols 13-2022
  "profiles": {"safest": [0.5, 0.5, 4.0], "balanced": [1, 1, 1], "fastest": [2, 0.5, 0.15]}
}
```
`profiles` are the weights for `[time, energy, risk]`.

## `GET /probe?r=<row>&c=<col>&ms=<max_slope>`
What the ground is like at one cell (useful for a cursor readout or a HUD).
```json
{"lat": 18.4465, "lon": 77.4601, "elev": -2570.5, "slope": 0.43, "rough": 0.04, "hazd": 2213.5, "nogo": false}
```
| field | meaning |
|---|---|
| `elev` | elevation, metres relative to the Mars areoid |
| `slope` | ground slope, degrees |
| `rough` | roughness: std-dev of local bumps, metres |
| `hazd` | distance to the nearest impassable cell, metres |
| `nogo` | `true` if the cell can't be walked (too steep, cliff, or no data) |

Returns `{"out": true}` outside the map.

## `POST /plan`
Plans one or more routes. Body (JSON):
```json
{
  "a_latlon": [18.44463, 77.45089],      // or "a": [row, col]
  "b_latlon": [18.45753, 77.26853],      // or "b": [row, col]
  "profiles": ["safest", "balanced", "fastest"],
  "max_slope": 25,                        // degrees; steeper ground is impassable
  "eva_hours": 8                          // suit life support; 30 % is kept in reserve
}
```
The first plan at a new `max_slope` rebuilds the terrain (~20 s), then it's cached.
A route usually takes 0.5-5 s.

Response:
```json
{
  "terms": ["time", "energy", "steep ground", "steep descent", "side-slope", "rough ground", "near cliff"],
  "routes": {
    "safest": {
      "latlon": [[lat, lon], ...],        // the route, one point roughly every 20 m
      "rows": [...], "cols": [...],       // same route in grid cells (fractional)
      "cum_km": [...],                    // distance along the route at each point
      "elev":  [...],                     // elevation at each point (m)
      "slope": [...], "hazd": [...], "rough": [...],   // ground at each point
      "grade": [...],                     // per step: + uphill / - downhill, degrees
      "terms": {"time": [...], "energy": [...], ...},  // per-step cost of each ingredient
      "report": { ... },                  // see below
      "explored": "<base64>",             // cells A* examined (uint16 row,col pairs, in order)
      "explored_step": 4,                 // every Nth explored cell is included
      "seconds": 1.4
    }
  },
  "straight": {                           // the straight line A->B, for comparison
    "rows": [...], "cols": [...], "distance_km": 12.2, "cost": 68940, "route_cost": 25454,
    "nogo_m": 379,                        // metres of impassable ground it crosses
    "max_slope_deg": 30.1, "max_grade_deg": 23.3, "terms": {...}
  }
}
```

### `report`
| field | meaning |
|---|---|
| `verdict` | `"GO - ..."` (with time to walk back), `"GO (one-way) - ..."`, or `"NO-GO on foot - ..."` |
| `distance_km`, `straight_line_km` | route length and straight-line distance |
| `walking_time_h`, `eva_budget_h` | estimated walking time vs usable suit time |
| `energy_kJ` | estimated metabolic energy |
| `ascent_m`, `descent_m` | total climb and descent |
| `max_uphill_grade_deg`, `max_downhill_grade_deg` | steepest stretches walked |
| `max_terrain_slope_deg`, `mean_terrain_slope_deg` | ground slope along the way |
| `closest_approach_to_hazard_m` | nearest the route gets to impassable ground |
| `rest_stops` | `[{"lat", "lon", "after_km", "after_min", "slope_deg"}]`, about every 45 min |
| `caution_segments` | `[{"from_km", "to_km", "why"}]`: grade > 15°, < 60 m from a cliff, or rough |
| `snapped_start_m`, `snapped_goal_m` | how far A/B were moved off impassable ground |
| `nodes_expanded` | how many cells the search examined |
| `astronaut` | the suit/body parameters used |

The seven `terms` arrays add up exactly to the cost the planner minimised, so they
explain each step. For example, `terms["near cliff"][k]` is how much step `k` was
penalised for closeness to a cliff.

Decode `explored` in JavaScript:
```js
const bytes = Uint8Array.from(atob(route.explored), c => c.charCodeAt(0));
const cells = new Uint16Array(bytes.buffer);   // [row0, col0, row1, col1, ...]
```

## `POST /export`
Renders the last plan as a PNG map plus a GeoJSON file per route in `output/`.
```json
{"png": "/output/route_20261114_162538.png", "path": "/full/path/on/disk.png"}
```

## `GET /layer/<name>.png?ms=<max_slope>`
Map imagery, georeferenced to the full grid (stretch it over 4456 × 5067 cells).

| name | what | resolution |
|---|---|---|
| `relief.jpg` | natural-colour relief (elevation × hillshade) | 20 m |
| `hillshade.jpg` | grey hillshade | 20 m |
| `slope`, `rough`, `hazard`, `risk`, `nogo` | transparent PNG overlays | 40 m |

The first request for each layer renders and caches it in `data/app_cache/`.

## Using the planner without the server
```python
from terrain import Terrain
from planner import RoutePlanner, Astronaut

t = Terrain("data/M20_JezeroCrater_CTXDEM_20m.tif")
p = RoutePlanner(t, Astronaut(max_slope_deg=25, eva_hours=8))
a = t.lonlat_to_rc(77.45089, 18.44463)          # note: lon, lat
b = t.lonlat_to_rc(77.26853, 18.45753)
rows, cols, report = p.plan(a, b, "safest")
why = p.breakdown(rows, cols, "safest")          # per-step cost ingredients
```


---

# Mission planner endpoints (spec-compliant)

All points are **Site ENU metres** `[east, north]` from the lander (the frame the spec requires). Requests may carry
an optional `"weather"` (`start_hour`, `dust_events: [[start_min, end_min, peak_tau]]`, `ls_deg`, ...) and `"sep"`
(`onset_min`, `dose_rate_msv_h`) object.

## `POST /mission/plan`
```json
{"start_enu": [0, 0], "goal_enu": [2500, 1500], "mode": "walk", "profile": "min_eta",
 "localization": {"covariance_enu_m2": [[25,0,0],[0,25,0],[0,0,1]], "confidence": "high"},
 "weather": {"dust_events": [[0, 600, 4.0]]}, "refine": true}
```
Returns the spec's *Planner -> HUD* message (`waypoints_enu_m`, `eta_min`, `turn_back_point_enu_m`, `fix_stops`,
`predicted_sigma_m`, `segment_difficulty`, `alerts`) plus `verdict`, `distance_km`, `energy_kJ`, `risk_score`, `dose_mSv`,
`confidence_at_goal`, `min_return_margin_frac`, `consumables_at_goal`, `confidence_timeline`, `violations` (empty if the
independent verifier is satisfied) and `search_stats`. Infeasible requests return HTTP 400 with the reason
(e.g. hazard buffer, return-to-shelter margin).

## `POST /mission/replan`
`{"trigger": "new_hazard | confidence_drop | dust | consumables | sep_alert | integrity_alert", "current_enu": [x, y],
"goal_enu": [x, y], "hazards": [{"enu": [x, y], "radius_m": 80}], "t_elapsed_s": 0, "e_used_j": 0}` ->
`{"plan": <as above>, "info": {"trigger", "response", "latency_s", "meets_SYS3_5s"}}`. `new_hazard` uses D* Lite.

## `POST /mission/rank`
The AI layer -> planner message: `{"task", "target_features", "area_hint", "time_budget_min", "mode",
"candidate_sites": [{"id", "enu_m": [x, y], "science_score", "label"}]}` ->
`{"sites": [{"id", "score", "eta_min", "round_trip_min", "distance_km", "risk_score", "localizability", "fix_stops",
"reasoning", "feasible"}]}`. Every number comes from a coarse planner run; the language model sets no limits.


---

# AI layer endpoints (lander terminal)

## `GET /ai/status`
`{"resolver": "offline" | "ollama" | "anthropic", "offline": true, "catalogue": {"source", "illustrative": true, "n_sites", "areas", "features"}}`

## `POST /ai/interpret`
`{"text": "Collect carbonate samples near the delta, two hours max.", "context": {"query": <previous query or null>, "n_sites": 0}}` ->
```json
{"intent": "new_request | refine | select | confirm | cancel | simulate | help | unknown",
 "query": {"task": "sample_collection", "target_features": ["carbonate"], "area_hint": "delta",
           "time_budget_min": 120, "mode": null, "profile": null},
 "select": null, "simulate": null, "confirm": false, "reply": "Understood: ...",
 "warnings": ["GUARDRAIL: ..."], "notes": [], "resolver": "offline"}
```
The query is schema-validated: only these fields survive, values are range-checked, and any attempt to set safety limits is dropped and reported.

## `POST /ai/sites`
`{"query": <validated query>, "start_enu": [x, y], "localization": {...}, "weather": {...}}` ->
`{"query", "mode_used", "budget_min", "sites": [{"rank", "id", "name", "enu", "science_score", "score", "eta_min", "round_trip_min",
"distance_km", "risk_score", "localizability", "hard_sections", "fix_stops", "verdict", "reasoning", "why"}], "unreachable": [...
(with "rover_option" where the rover can reach a site that walking cannot)], "notes": [], "summary": "..."}`.
Every number comes from a coarse planner run; the AI layer only chose the candidates.


---

# Explanation of a route

Every `/mission/plan` and `/mission/replan` response now carries `explain` (or `null` if it could not be built):
`weights`, `per_m` (cost per metre by ingredient, aligned with `route`), `stop_cost`, `totals`, `pct`, `series` (grade, slope, sigma,
clearance, needed buffer per sample), `tight` (the tightest places), `straight` (what a straight line would cross), `steps` (the numbered recipe)
and `search` (`cells`, `levels`, `w`, `h`, `res`, `e0`, `n0`, `sigma_levels`, `expanded`, `step`: the states A* expanded, in order, as window cell
indices; `null` for D* Lite plans).

## `POST /mission/compare`
The same body as `/mission/plan`. Plans the trip with each profile (coarse, no refinement) and returns
`{"alternatives": [{"profile", "ok", "distance_km", "eta_min", "energy_kJ", "risk_score", "fix_stops", "min_clearance_m", "verdict", "route": [[x, y], ...]}]}`.
