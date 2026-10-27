# Smart AI Attendance Terminal

Flask + MongoDB + Face-API.js (runs in the browser). Everything lives in the repo root.

## Files
- `app.py` – Flask API, anti-spam, webhooks, exports
- `index.html` – dashboard (classes, roster, logs, export)
- `scan.html` – camera terminal (recognition, chime, badge, unknown capture, offline queue)
- `requirements.txt`, `render.yaml`, `.env.example`, `.gitignore`

## Setup
1. Create a free MongoDB Atlas cluster. Create a DB user. Under Network Access allow `0.0.0.0/0` (Render free has no fixed IP).
2. Copy the connection string (`mongodb+srv://...`).

## Run locally
    pip install -r requirements.txt
    export MONGODB_URI="..."  ADMIN_PASSWORD="secret"
    python app.py
Open http://localhost:5000 (camera works on localhost).

## Deploy on Render
1. Push this folder to GitHub.
2. Render -> New -> Blueprint (or Web Service) -> pick the repo.
3. Set env vars `MONGODB_URI` and `ADMIN_PASSWORD`.
4. Open the Render URL, log in (any username, your password).

## Notes
- Photos and snapshots are stored in MongoDB (resized to <=480px JPEG), so Render's ephemeral disk doesn't matter.
- Free Render sleeps when idle; the first request is slow. Ping `/healthz` with a cron service if needed.
- Webhook URL: Discord webhook URLs get a Discord-formatted message (with snapshot for unknown visitors). Anything else gets a JSON POST.
- Face matching distance threshold is 0.5 (in `scan.html`, `MATCH_THRESHOLD`).
