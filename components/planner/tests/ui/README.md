# Headless UI smoke test

Loads `mission_ui.html` in jsdom against a running server, clicks/zooms/drags, places hazards, replans, and checks the
results. It checks logic and canvas drawing; it does **not** check CSS layout (jsdom has no layout engine).

    python app.py --synthetic --no-browser --port 8061      # terminal 1
    cd tests/ui && npm install && node ui_smoke.js          # terminal 2   (needs Node 18+)

`ui_smoke.js` tests the manual planner tab; `ui_terminal.js` tests the lander terminal (request, sites, select, confirm, en-route events).
