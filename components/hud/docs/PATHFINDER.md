# Using the Jezero mission planner with the HUD

The [Jezero mission planner](https://github.com/aaryanbadgujar12345/jezero-mission-planner) plans walking and rover routes with position-uncertainty, return-to-shelter and fix-stop logic. Its output is in **Site ENU metres** (x east, y north of the lander), which is also the HUD's coordinate frame, so no map conversion is needed.

```
phone (HUD on GitHub Pages)  --https-->  planner on your server   POST /mission/plan
```

> **Planner server version:** use the patched `app.py` from the `mission-planner-server` package. The original only listens on localhost and sends no CORS headers, so the HUD cannot call it from another site.

## In the HUD (recommended)

1. Tap **Menu**. In **Route planner** enter the planner's https address and, if you set one, the access key.
2. Tap **Load destinations** to fill the goal list, or type a goal as `x,y` metres. Start defaults to `0,0` (the lander).
3. Choose **Walk** or **Rover**, and **Fastest**, **Balanced** or **Safest**. Optionally enter the crew's current position error in metres.
4. Tap **Plan route**. The route loads into the HUD and is saved on the phone, so it works with no signal. Plan before leaving base.

If the planner refuses a route (for example, the crew could not get back to the lander), the reason is shown in the Menu log.

## What the HUD shows from a plan

| Planner output | On the HUD |
|---|---|
| `route.enu` (dense route) | The race line, simplified to about 3 m accuracy |
| `fix_stop_detail` | Named gates such as "Fix stop 1 (landmark, 60 s)" |
| `turn_back_point_enu_m` | A gate named "Turn-back point" |
| Runs of `hard` in `route.difficulty` | Amber "Hard terrain" caution zones |
| `verdict`, `distance_km`, `eta_min` | A summary line in the Menu log |
| `alerts` | Logged in the Menu, and the first one is shown in view |

## With the command-line tool

```bash
python tools/mission_plan_to_mission.py --api https://planner.example.com --key KEY \
    --goal 2500,1500 --mode walk --profile min_eta --out mission.json
```
Then send `mission.json` to the goggles through the base server (`MISSION=../mission.json node server.js`, type `reload` to push a change) or use **Load mission file** in the HUD. `--plan response.json` converts a saved response instead.

## Test without the planner

`examples/mission_plan.sample.json` is **synthetic** data shaped like the planner's response (it is not real planner output). Convert it with `python tools/mission_plan_to_mission.py --plan examples/mission_plan.sample.json`.

## Notes and limits

- The planner's science and AI layer (the lander terminal) stays on the planner's own web page. The HUD only takes the finished route.
- The HUD does not yet replan when the crew leaves the route. The planner has `/mission/replan` for that, and calling it from the HUD is a natural next step.
- The HUD's position comes from step counting, which drifts. The base can correct it with `position` messages over the link.

## Older pathfinder planner

`tools/pathfinder_to_mission.py` still converts routes from the earlier Jezero Pathfinder planner (latitude and longitude based). The HUD menu now targets the mission planner.
