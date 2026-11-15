# Spec compliance: Mars Surface Navigation System, Integrated Design Specification v0.2

This repo now contains a planner that implements the **path-planning side** of the spec
(Sections 8, 9, 10, 13 and the planner-related parts of 4, 6, 14, 15.3). The original
`planner.py` / `app.py` / `plan.py` are untouched and still work; the new code sits beside them.

```
frames.py          Site ENU frame anchored at the lander (4)
terrain.py         + per-mode hazard layers, soil proxy, corridor up-sampling (8)
localization.py    sigma model, confidence levels, localizability layer, Localization->planner contract (7.5, 8.2, 13)
environment.py     dust / temperature / wind, Sun, SEP + dose, consumables (8.1, 9.1, 9.5, 10)
kernels.py         numba kernels: uncertainty-aware A* over (cell, sigma bucket), return-to-shelter field (9)
mission_planner.py hierarchical planner, verifier, shelter route, replan triggers, site ranking (6, 9, 13, 14)
replan.py          D* Lite incremental replanning (9.4, 9.5)
plan_mission.py    CLI        app.py: POST /mission/plan | /mission/replan | /mission/rank
ai_layer.py        AI layer / target resolver (spec 6): offline parser, optional LLM adapters, guardrails, science catalogue
explain.py         'how was this route calculated': cost ingredients, buffer vs position error, search replay, straight-line check
mission_web.py     web backend;  mission_ui.html: the lander terminal + manual planner (python app.py --synthetic)
tests/             test_spec.py (19 groups), test_ai.py (12 groups), test_web.py, ui/ (headless UI tests: manual planner + terminal) + bench_spec.py (SYS-2 / SYS-3 timing) + synth_dem.py
demo_moments.py    renders the four demo moments of 15.2 -> docs/demo_moments.png
```

## Traceability

Status: **done** = implemented and tested; **model** = implemented with an engineering estimate the spec
itself marks (est.); **out of scope** = not part of the path planner (listed honestly below).

### Planner (Section 9)
| Spec | Status | Where | Test |
|---|---|---|---|
| 9.1 minimise ETA s.t. hard terrain limits | done | `kernels.search_aug`, profile `min_eta` | `hierarchical_search_and_outputs` |
| 9.1 dose budget + shelter reachability | done | `environment.dose_msv`, `kernels.return_field`, `res_ok` | `sep_shelter_route`, `return_to_shelter_and_turn_back` |
| 9.1 consumables + return margin (O2, power, cooling, CO2) | done | `environment.Consumables`, `kernels.res_ok` | `return_to_shelter_and_turn_back` |
| 9.1 astronaut energy / fatigue limits | model | energy via Minetti + consumables; no heart-rate / fatigue state (see Section 10 below) | - |
| 9.1 weather time windows | done | `Weather.window_end_min`, `MissionPlanner._deadline_s` | `dust_storm_changes_...` |
| 9.2 sigma propagated along candidate paths | done | `localization.sigma_move`; search state is (cell, sigma bucket) | `uncertainty_inflates_buffers_...` |
| 9.2 buffers scale with predicted sigma; reject or insert fix stop | done | hazard test `hazd >= n_sigma*sigma + base` in `search_aug` | same + `fix_stop_inserted_and_paid_for` |
| 9.3 localization penalty on low-localizability cells | done | `wl*(1-L)*d` in the cost | `localizability_prefers_good_terrain` |
| 9.3 optimised fix stops, cost counted in ETA | done | stop action in `search_aug`; `LocParams.fix_stop_s` = 60 s | `fix_stop_inserted_and_paid_for` |
| 9.4 hierarchical: coarse A* on ~20 m, refine 1-5 m | **model** | coarse A* on the DEM grid + corridor refinement on a 4x finer grid. **The fine grid is the 20 m DEM resampled**, so it straightens routes but adds no new terrain information. A real HiRISE DTM is not wired in. | `hierarchical_search_and_outputs` |
| 9.4 D* Lite replanning | done | `replan.DStarReplanner` (verified equal to Dijkstra ground truth after sequences of hazards) | `dstar_lite_replan_and_latency` |
| 9.4 walking and rover modes, different layers/limits | done | `WalkAgent` / `RoverAgent`; rover 15 deg, bans sand and blocky ground | `rover_mode_differs` |
| 9.4 outputs: path, ETA, energy, risk, turn-back, fix stops, sigma profile | done | `MissionPlan.hud_json()` / `to_json()` | `hierarchical_search_and_outputs` |
| 9.5 replan triggers (6 rows) | done | `MissionPlanner.replan(trigger, ...)` | `dstar_...`, `sep_...` |

### Cost map and localizability (Section 8)
| Spec | Status | Notes |
|---|---|---|
| slope, roughness/rocks, crater rims/cliffs | done | from the CTX DEM (`terrain.py`); unchanged in spirit from the original |
| soil trafficability | **model** | `Terrain.sand` is a *proxy* (very flat + very smooth). Assign real THEMIS / HiRISE rock-abundance arrays to `terrain.sand` / `terrain.rough` when you have them. |
| illumination and shadow cost | **not done** | `environment.sun_elevation_deg` exists; no DEM-shading cost term yet |
| weather (dust, temperature, wind) | done | time-varying cost multipliers; dust also degrades camera / celestial availability |
| space weather | done | `SEPAlert`, dose model (dose rates are estimates) |
| 8.2 localizability layer (skyline, orbital, celestial availability) | **model** | skyline score from DEM-rendered horizons; orbital score is a DEM-roughness proxy (`LocalizabilityLayer.set_orbital_score` accepts a HiRISE-derived array); celestial availability is time + dust dependent |

### Contracts (Section 13) and AI layer (Section 6)
| Spec | Status |
|---|---|
| Localization -> planner message | done: `LocalizationState.from_dict`, planner starts from its covariance |
| Planner -> HUD message | done: exact keys `waypoints_enu_m, eta_min, turn_back_point_enu_m, fix_stops, predicted_sigma_m, segment_difficulty, alerts` (+ extras) |
| AI layer -> planner message | done: `ai_layer.py` (offline parser by default, optional local / cloud LLM behind the same schema + guardrails) feeds `rank_sites`, which runs the coarse planner run and returns every number shown to the crew |
| Single Site ENU frame, geodetic only at the boundaries | done: `frames.SiteFrame` |

### Requirements table (Section 3)
| ID | Target | Status |
|---|---|---|
| SYS-2 initial plan over ~10 km radius | <= 30 s | **1.2-3.4 s** (`tests/bench_spec.py`, 22 km synthetic DEM, coarse + refinement) |
| SYS-3 replan on new hazard / state change | <= 5 s | **0.8 s** D* Lite repair on a 10 km route; 0.3-1.4 s for the other triggers |
| SYS-8 every pose and path carries uncertainty | mandatory | done: sigma per waypoint, confidence timeline |
| SYS-9 return-to-shelter always feasible with margin | hard constraint | done and *verified independently* after planning |
| SYS-12 crew can reject any target / path | mandatory | planner never commits; every result is a proposal with a verdict |
| SYS-1, 4-7, 10, 11 | - | localization / HUD / LLM requirements: **out of scope** |

### Failure modes (Section 14) and metrics (15.3), planner-relevant rows
| Spec | Status |
|---|---|
| Dust storm: stars lost, power / visibility drop, shorter route, recommend wait or return | done (`Weather.hard_limits`, `NO-GO - weather limits`) |
| Featureless terrain: localizability-aware routing | done |
| "Planner output checked independently" | done: `verify_route` re-derives sigma / time / energy on a 10 m resample with a sub-cell hazard map |
| 15.3 planner hazard violations under injected localization error = 0 | done: worst-case 3-sigma offset toward the nearest hazard never enters no-go; Monte-Carlo: 0 / 192 300 samples |
| 15.3 replan latency <= SYS-3 | done |
| Compute failure / DEM geolocation offset / clock drift / filter divergence | out of scope |

## AI layer and lander terminal (spec Sections 2 and 6)
| Spec | Status |
|---|---|
| 6: speech/text -> structured query (task, targets, time budget, mode, constraints) | done: `OfflineResolver` (no model, no network); optional `LLMResolver` (Ollama / Anthropic API), **mock-tested only** |
| 6 guardrail: structured output with schema validation | done: `validate_query` accepts only schema fields, clamps ranges, reports everything dropped |
| 6 guardrail: the model cannot set slopes, dose limits or consumable margins | done and tested: such keys are stripped and shown as a GUARDRAIL warning |
| 6 guardrail: all numeric claims come from the planner | done: site cards use `rank_sites` output; reply text is deterministic code |
| 6 guardrail: crew can always ignore the suggestion; planner only runs after confirm | done: select, then confirm; nothing is planned before |
| 6 guardrail: runs on-device, no Earth round trip | done for the offline parser; **not** true for the cloud LLM option (stated in the README and the terminal) |
| 6: tool call over geology / mineral layers to find candidate sites | **model**: a small catalogue of sites with hand-assigned tags (**illustrative**; NOT the CRISM / geologic-map layers) |
| 6: sites scored by science value, accessibility, ETA, risk, localizability | done: science match from the catalogue + coarse planner runs (`rank_sites`) |
| 6: top sites shown with plain-language reasoning; crew confirms or edits | done: site cards, editable request card, confirm bar |

Spec Section 2, operational scenario:
| Step | Status |
|---|---|
| 1 crew request | done (text; optional browser microphone, online in Chrome) |
| 2 three ranked sites with science value, ETA, risk | done |
| 3 crew picks a site and mode and confirms | done |
| 4 cold-start localization from the surveyed lander pose | **modelled**: starting sigma from the lander pose; the star / skyline / image solvers are not implemented |
| 5 min-ETA path with turn-back point and fix stops | done |
| 6 path ribbon and halo in the HUD | **not a HUD**: the map shows the path, uncertainty halo and confidence colouring |
| 7 en route: VIO tracking, periodic absolute fixes | **simulated**: "advance crew" moves the crew along the route with the predicted sigma |
| 8-9 dust rises, stars unavailable, replan | done, including "recommend wait or return" when no fix is possible (spec 14) |
| 10 SEP alert: fastest route to shelter and countdown | done |

## Showing how the route was calculated (not a spec requirement; added for the terminal)
`explain.py` recomputes, along the finished route and with the same kernels the search uses, the cost ingredients (time, energy, risk,
localization penalty, fix stops), the position error against the 3-sigma buffer, and what a straight line would have hit. A test checks that the
time and energy ingredients add up to the route's own time and energy. The **search replay** shows the (cell, position-error) states A* expanded in
order; to keep memory bounded the kernel keeps at most 120,000 and thins itself (every 2nd, 4th, 8th ... state) on big searches, which the panel
states ("every 8th shown"). Caveats: the risk term is recomputed from the verifier's clearance (measured to the cell edge) plus half a cell, so it
matches the search's value to within a small amount rather than exactly; a plan produced by D* Lite has no search to replay (the panel explains the
incremental repair instead); the finer refinement grid is the same elevation data resampled.

## What is **not** implemented (and was never going to be a path-planning change)
The spec is a whole system. These parts are separate subsystems, not planner logic, and are **not** here:
- the real geology / CRISM data layers behind the AI layer's science scores (the catalogue is illustrative)
- celestial solver, skyline *matching*, orbital image *matching*, VIO, beacon ranging, the fusion filter and cold start (Section 7) - the planner consumes their output (`LocalizationState`) and models their accuracy
- the HUD renderer and lander-screen UI (Section 11) - the planner emits everything the HUD needs (confidence timeline, per-segment difficulty, turn-back marker, alerts)
- biometrics / heart-rate fatigue model (Section 10)
- SPICE ephemeris: `environment.sun_elevation_deg` is an analytic approximation (~0.2 deg)

## Caveats you should know before presenting
1. **Tested on a synthetic Jezero-like DEM**, georeferenced like the CTX mosaic. The real 90 MB USGS DEM could not be downloaded in the environment this was built in. Run `python get_data.py && python tests/bench_spec.py` on the real DEM; scenario coordinates in `test_spec.py` are tuned to the synthetic terrain, but every generic check (verifier clean, return margin, consistency, latency) applies to any DEM.
2. All sensor accuracies, drift rates, dose rates, battery size, slope limits are **(est.)**, as the spec itself says. They are plain fields in `LocParams`, `Consumables`, `WalkAgent`, `RoverAgent`, `SEPAlert`.
3. The search carries time / energy / consumables along the single best label per (cell, sigma bucket). That is a standard resource-constrained heuristic, **not** a proof of global optimality; that is why the independent verifier exists and why a violation downgrades the verdict to NO-GO.
4. D* Lite cannot carry sigma in its state, so it plans with an assumed sigma; its route is re-verified and the planner falls back to the full uncertainty-aware A* if a sigma-dependent constraint fails (`plan.stats["engine"]`).
5. Hazard-proximity cost changes farther than max(640 m, buffer + 200 m) from a new hazard are ignored by the incremental D* repair (< 0.05 % of edge cost).

## Web interface
`python app.py --synthetic` (or `python app.py` on the real DEM) serves a single-page map UI. It was verified **headless** (jsdom +
canvas, against the live server): boot, 7 scenario presets, click-to-plan, pan/zoom, overlays, hazards + D* replan, dust, SEP,
error display, with no JavaScript errors, and the canvas output was inspected. **Not verified: CSS layout in a real browser.** If a
panel looks off on your screen, tell me which browser and I will fix it.
Map pictures (terrain, no-go masks, slope) are rendered at the DEM's **full 20 m resolution** (an earlier version shrank a real-size map to 60 m per
pixel, which looked blocky). They are rendered in strips so memory stays modest (about 1.4 GB peak at 20 million cells, most of it the terrain data
itself), and the base map is cached in the `data` folder as `mission_layer_*.jpg`, so only the first start renders it. The search replay is drawn as a
smooth raster, not as individual squares. Measured on synthetic maps of 10 and 20 million cells (the real map is 22.6 million).
The planner can also answer **HOLD** ("take a position fix before moving") when the crew's own start position is already inside a
hazard's uncertainty buffer; the verifier previously reported this as a generic constraint violation.

## Bugs found while building this (fixed)
- **AI layer: an unknown mineral ("collect sulfate samples") silently became "sample anything"** and returned random sites. It is now
  refused honestly, both in the parser and in the site search.
- **Dust storm replan failed with a raw error** when no position fix was possible. The planner now does what spec 14 says: it recommends
  wait or return and plans the way home.
- **Planning hung forever on the real terrain** for points far from the lander: the search window kept growing until it covered the
  whole 22-million-cell map. The window is now capped (~2.5 M cells), impossible requests are refused in ~1-2 s with a plain
  explanation ("the trip back to the shelter alone would take ~N min"), and the page shows an elapsed-time counter and a timeout.
  A checkbox, **"require a safe return to the lander"**, can be switched off for a plain start-to-goal path anywhere on the map
  (verdict then reads "GO - return to shelter NOT checked").
- **Search and verifier disagreed in two edge cases** (a straight leg cutting a cliff corner by ~1.5 m; one isolated steep pixel the
  terrain model treats as noise). Both made a valid route read NO-GO. Fixed, so the verifier and the search now agree.
- **Windows: a second server could silently share a busy port**, so the browser kept talking to an older run. The app now refuses a
  busy port and prints which terrain it loaded.
- **Weather limit check sampled the EVA window at only 12 points**, so a short intense dust peak could slip through and be called GO.
  Now sampled every 5 minutes (regression test `narrow_dust_peak_is_not_missed`).
- **Integer mode id passed as a string into numba kernels** silently selected the rover physics branch inside the walking search. Reported ETAs were right (the verifier used the correct physics) but the search was optimising the wrong cost. Now guarded by `test_search_and_verifier_agree` (search time vs independently recomputed time within 5 %, A* heuristic admissibility).
- **Inadmissible heuristic** exposed the above through D* Lite giving results that differed from Dijkstra.
- **numba `cache=True` does not notice edits to functions in *other* modules**, so editing `localization.sigma_move` left the search running the old model. `kernels._numba_cache_guard` now hashes the dependency sources and clears stale caches.
- **Original `terrain.py` cache name ignored which DEM it came from**: two DEMs in one folder silently shared layers. The cache is now validated against the DEM's shape and age.
- D* repair re-evaluated every cell whose nearest hazard changed (3 s). Now bounded to the proximity horizon, with a precomputed edge-cost table (4.7 s -> 0.8 s for a 10 km route).
