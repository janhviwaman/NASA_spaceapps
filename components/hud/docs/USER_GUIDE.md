# User guide

## Getting started

1. Open the app on a phone in **Safari (iPhone)** or **Chrome (Android)**. It must be an https address.
2. Tap **Menu**, then **Enable motion**, and tap **Allow**.
3. Hold the phone upright, then tap **Face path**. The race line now points the way you are facing.

## The display

| Element | Meaning |
|---|---|
| Striped line | The route. Follow it. The faded part behind you is already walked |
| Tall gate | The next waypoint, with its name and distance |
| Red circle | A hazard to avoid |
| Top tape | Compass heading. The mint marker shows the bearing to the next waypoint |
| "TURN 40°" arrow | The next waypoint is off to the side |
| Bottom | Next waypoint, distance to it, and progress along the route |
| Right | O₂, power and CO₂ |
| Left | Link status, mission clock and sensor debug lines |

## Menu controls

| Button | What it does |
|---|---|
| Connect / Re-download mission | Link to the base server and fetch the mission |
| Load mission file | Pick a mission `.json` from the phone |
| Enable motion | Turns on the phone's motion sensors |
| Face path | Realigns the view so the next waypoint is ahead |
| Camera | Shows the rear camera behind the HUD (AR) |
| Goggle mode | Split view for a phone headset. Tap the screen to open the menu again |
| Sim walk | Moves along the route at walking pace |
| Step +5 m / Restart | Test movement, or return to the start |
| Fullscreen | Hides the browser bars |

Without motion sensors (for example on a laptop), drag with the mouse or finger to look around.

## How movement works

The phone counts steps with its accelerometer and moves you 0.7 m per step in the direction you are looking. This is an estimate and drifts over distance. The base can correct it by sending `position` messages.

## Before leaving base

1. Connect to the base server and tap **Re-download mission**.
2. Check the mission name appears in the Menu log.
3. The mission is saved on the phone, so it still works after leaving Wi-Fi range.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Motion permission denied" | Fully close Safari and reopen the page, then tap Enable motion and Allow. Make sure the page is not inside another app or a frame |
| IMU line says "off" | Needs https, a real phone and permission |
| View moves but position does not | Check `steps` rises when you walk. If not, hold the phone upright and walk normally, or use Step +5 m. Tell the maintainers the `acc` values |
| Camera does not start | Allow camera access, and use https |
| Cannot connect to the server | Check the Wi-Fi, the address and port, and that the page and server both use https (or both http) |
| Old version still showing | Close and reopen the page to refresh the cached app |
