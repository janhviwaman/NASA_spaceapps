# Planner server patch

The planner's `app.py` in this folder is the patched server (CORS, `--host`, `PORT`, `API_KEY`,
`ALLOWED_ORIGIN`, `slim`, `/health`), with one change: the page shell (`/`, `/classic`) is public.
Deployment files are in `../../deploy/planner-host/`. See `../../docs/HOSTING.md`.
