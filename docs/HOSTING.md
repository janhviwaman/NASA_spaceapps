# Hosting

Repository: `janhviwaman/NASA_spaceapps`. Deploy the main product first, then the others as needed.

## A. MVP console and crew HUD on Render (main)

1. Push this repository to `https://github.com/janhviwaman/NASA_spaceapps` (see "Publishing" below).
2. In Render: **New > Blueprint**, choose the repository. Render reads `render.yaml` (root) and builds
   `components/mvp` with Docker. Plan: free. Health check: `/api/health`.
3. Your address is `https://<service-name>.onrender.com` (the name is in `render.yaml`:
   `mars-nav-mvp`, so the address is `https://mars-nav-mvp.onrender.com` if the name is free).
4. Test: open `/api/health`, then `POST /api/session` (or use the console), and open the printed
   `/hud?code=...` on the phone. Allow motion access there (iPhone: tap Enable iPhone Sensors).

The MVP's `/api/session/{code}/mission` upload accepts planner routes. It has no authentication yet,
so give out join codes only to the crew.

## B. Planner host (planner, map, pathfinder): one server with HTTPS

Needs a Linux server with Docker, a domain pointing at it, and ports 80 and 443 open.

1. `cp deploy/planner-host/.env.example deploy/planner-host/.env` and set `PLANNER_DOMAIN`
   (your domain), `API_KEY` (a long random string), and `ALLOWED_ORIGIN` (already set to
   `https://janhviwaman.github.io`, used only by the secondary HUD).
2. `cd deploy/planner-host && docker compose up -d --build`
3. Check `https://<domain>/health`, `https://<domain>/map/`, and `https://<domain>/pathfinder/`.

The planner's routes reach the MVP through `tools/planner_to_mvp.py`, which runs on your laptop or
any server. Set `--mvp https://mars-nav-mvp.onrender.com` (your MVP address) and `--api https://<domain>`
(your planner address). The bridge refuses NO-GO and HOLD routes.

## C. Secondary HUD on GitHub Pages

The workflow `.github/workflows/pages.yml` publishes `components/hud` on every push to `main`.

1. On GitHub, open `janhviwaman/NASA_spaceapps` > **Settings > Pages > Build and deployment >
   Source: GitHub Actions**.
2. The site is `https://janhviwaman.github.io/NASA_spaceapps/`. The planner's `ALLOWED_ORIGIN`
   is `https://janhviwaman.github.io` (the origin, without the path). It is already set in `.env.example`.
3. On the phone, open the HUD, tap **Menu**, enter `https://<domain>` and the key under **Route planner**, then **Load destinations**.

## D. Base server (optional)

Only for live messages to the secondary HUD. Build from `components/hud` with
`docker build -f server/Dockerfile -t mars-hud-base components/hud`, or run `node server.js`
in `components/hud/server`. Deploy it to Render as a second web service with `rootDir: components/hud`.

## Publishing the repository

```bash
cd mars-suite                       # the unzipped suite, with its .git folder
git remote add origin https://github.com/janhviwaman/NASA_spaceapps.git
git push -u origin main
```

GitHub will ask you to sign in with your account. The build environment has no access to your
account, so the push is yours to run.
