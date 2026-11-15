# Link protocol

The goggles talk to the base server over Wi-Fi using plain HTTP and a WebSocket. All payloads are JSON. When the page is served over https, the server must use `https://` and `wss://`, because browsers block insecure connections from secure pages.

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/mission.json` | GET | Download the current mission file |
| `/ws` | WebSocket | Live messages from the base to the goggles |

The address field in the app takes `host:port`, for example `192.168.1.10:8443`.

## Coordinate frame

- Local metres, relative to the base: `x` = east, `y` = north, `0,0` = habitat airlock.
- Compass headings are degrees clockwise from north.

## Mission file

```json
{
  "name": "Jezero Delta Front Traverse",
  "waypoints": [ { "x": 0, "y": 0, "name": "Hab airlock" }, { "x": 8, "y": 40, "name": "Rover pad" } ],
  "hazards":   [ { "x": -40, "y": 118, "name": "Loose regolith", "r": 7 } ]
}
```

- `waypoints`: at least 2, in travel order. The first is the start. The path is a smooth curve through them.
- `hazards` (optional): circles with radius `r` in metres.
- The app saves the last mission in the device's local storage, so it works offline.

## WebSocket messages (base to goggles)

| `type` | Fields | Effect |
|---|---|---|
| `mission` | same as mission file | Replace the mission and restart at the first waypoint |
| `position` | `x`, `y` | Set the current position and stop simulated or step movement from the previous target |
| `telemetry` | `o2`, `battery`, `co2` (numbers, percent) | Update the readouts |
| `msg` | `text` | Show a notice in view for 6 seconds |
| `command` | `action`: `recenter` or `goto`; `index` for `goto` | Realign the view to the next waypoint, or jump to waypoint number `index` |

On connect, the server should send a `mission` message. Unknown message types are ignored. Malformed JSON is logged in the app's menu and skipped.

## Examples

```json
{"type":"msg","text":"Dust storm warning: return to hab within 30 min"}
{"type":"position","x":-12.5,"y":88.0}
{"type":"telemetry","o2":84,"battery":71,"co2":2}
{"type":"command","action":"goto","index":3}
```

## Security notes

This proof of concept has no authentication. Before any real use, add transport security with trusted certificates, authenticate the server and sign mission files so the goggles can reject tampered routes.

## Optional mission fields

| Field | Purpose |
|---|---|
| `path` | A dense list of `{x, y}` points for the race line. If present, the line follows it instead of the waypoints. `waypoints` then act only as named gates (start, rest stops, goal) |
| `brief` | One-line summary (for example a go / no-go verdict). Shown in the Menu log |
| `alerts` | List of `{type, msg}`. Logged in the Menu, and the first is shown in view when the mission loads |
| `hazards[].kind` | `"caution"` draws an amber zone instead of a red one |
