# Known limits

## Not verified in the build environment

- **Real Jezero DEM.** The sandbox cannot reach the USGS servers. Everything ran on the planner's
  synthetic terrain, and the pathfinder was tested on a synthetic DEM with the same name. Run
  `python components/planner/get_data.py` (or the compose `data` service) on a connected machine and test again.
- **Docker.** Docker is not installed in the build environment. The Dockerfiles and compose file are
  written and the compose file parses, but no image was built and no container was started.
- **GitHub Pages and Render deployments.** Not performed. The workflow and blueprint are standard but unexecuted.
- **Main HUD on Render.** The MVP's HUD (`/hud`) was not opened on a phone or on Render. Its IMU and camera code is unchanged from the upload.
- **Phone sensors and camera.** No device was available. The IMU, camera, and step-counting code is
  unchanged from the uploads and untested here. iOS requires HTTPS for motion access.

## Design limits

- **The MVP's sites are placeholders.** They use the same frame, but their positions do not correspond
  to the planner's terrain features. A planner route shows the planner's geography; the MVP shows its own.
- **The MVP's mission upload has no authentication.** Anyone who knows a join code can replace the mission.
  Add a key before exposing the MVP publicly.
- **The base server has no authentication** and serves any mission file it is given.
- **The pathfinder has no API key** and is public on the planner host. Its routes are computed on the
  server, so a crowd of requests can slow the planner host. Limit it at the proxy if needed.
- **The planner's API key is visible to the browser** that uses it (it is stored in the HUD's local storage).
  It stops casual misuse, not a determined user.
- **One planning request at a time** on the planner and pathfinder servers.
- **The HUD does not replan** when the crew leaves the route. The planner has `/mission/replan` for that.
- **Position in the HUD is dead-reckoned from steps** and drifts. Mars has no GPS.
- **Nothing here is safety-certified.** Do not use it for real navigation.
