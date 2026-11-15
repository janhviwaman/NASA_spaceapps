# Mars EVA HUD

A phone-based augmented/virtual reality heads-up display for Mars surface walks. It draws the planned route as a glowing **race line** on the ground, so an astronaut can simply follow it. The view moves with the phone in real time using its motion sensors (IMU), and mission files are sent from a base server over Wi-Fi (IP) before the crew leaves the habitat.

> Built as a proof of concept for a NASA Space Apps Challenge project. It is not flight software and is not affiliated with or endorsed by NASA.

## Features

- **Race-line navigation:** smoothed path ribbon with striped guidance, waypoint gates, hazard zones and an off-screen turn arrow.
- **HUD:** compass tape, pitch ladder, distance and progress to the next waypoint, O₂ / power / CO₂ readouts, link status, mission clock.
- **IMU-driven view:** head or phone orientation controls the camera in real time.
- **Step-based movement:** the phone's accelerometer counts steps and moves the position along the viewing direction.
- **AR / VR modes:** optional rear-camera passthrough, and a side-by-side goggle mode for a phone headset.
- **IP link to base:** WebSocket plus HTTP over Wi-Fi to receive missions, positions, telemetry, messages and commands.
- **Offline-ready:** the app and the last downloaded mission are cached on the device (PWA).
- **No build step:** one `index.html`, no dependencies.

## Try it

1. Open the deployed site (see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)) in **Safari (iPhone)** or **Chrome (Android)**.
2. Tap **Menu**, then **Enable motion**, and allow motion access.
3. Tap **Face path**, hold the phone upright, and look around. Tap **Sim walk** or **Step +5 m** to move without walking.

Full controls and troubleshooting: [docs/USER_GUIDE.md](docs/USER_GUIDE.md).

## Run with a base server (local network)

```bash
cd server
npm install
node server.js          # http, for desktop testing
```

Phones only allow motion sensors on https, so for a phone on the same Wi-Fi use a trusted local certificate. Setup is in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#field-setup-base-server-on-local-wi-fi). Message formats are in [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Repository layout

```
index.html               the whole app (HTML, CSS, JavaScript)
sw.js, manifest.webmanifest, icon.svg   offline/PWA support
mission.example.json     sample mission file
server/                  mock base-station server (Node.js)
docs/                    protocol, deployment and user guide
.github/workflows/       GitHub Pages deployment
```

## Known limitations

- Position is **dead-reckoned from steps**, so it drifts. Mars has no GPS; a real system should feed positions from rover, beacon or visual-odometry data over the link (`position` messages).
- Compass heading comes from the phone's sensors and is relative; use **Face path** to align at the start.
- The app has not been tested across a wide range of phones. Step sensitivity may need tuning.
- Nothing here is safety-certified. Do not use it for real navigation.

## Roadmap

- Fuse step length with stride calibration and gyro drift correction
- Beacon or visual-odometry position source
- Signed mission files and message authentication on the link
- Terrain heights (a DEM) for a 3D path
- Voice cues and a hands-free mode

## Contributing

Issues and pull requests are welcome. Keep the app dependency-free and in one file where practical. See [docs/PROTOCOL.md](docs/PROTOCOL.md) before changing message formats.

## License

[MIT](LICENSE)
