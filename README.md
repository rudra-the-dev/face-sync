# Smart AI Attendance Terminal

Flask + MongoDB + Face-API.js (recognition runs in the browser). Everything lives in the repo root.

## Files
- `app.py` – Flask API, anti-spam, webhooks, exports, device endpoints
- `index.html` – dashboard (classes, devices, roster, logs, export)
- `scan.html` – scanner terminal (recognition, chime, badge, unknown capture, offline queue)
- `camera.html` – page a phone opens to act as a camera
- `firmware.html` – both ESP32 sketches with library lists and copy buttons
- `requirements.txt`, `render.yaml`, `.env.example`, `.gitignore`

## Setup
1. Create a free MongoDB Atlas cluster + DB user. Network Access: allow `0.0.0.0/0`.
2. Copy the `mongodb+srv://...` connection string.

## Run locally
    pip install -r requirements.txt
    export MONGODB_URI="..." ADMIN_PASSWORD="secret"
    python app.py
Open http://localhost:5000

## Deploy on Render
1. Push to GitHub. Render -> New -> Blueprint (or Web Service).
2. Set `MONGODB_URI` and `ADMIN_PASSWORD`. Keep ONE gunicorn worker (live frames/events are in memory).
3. Open the Render URL, log in (any username, your password).

## How device linking works
Each class has its own devices (Dashboard -> class -> Devices). Each device gets a secret token.

    ESP32-CAM / phone --POST frames--> server --GET frames--> scan.html (face-api) --> attendance
    server --events (polled by ESP32)--> ST7735 screen + satellite speaker

- **Camera source** (class settings): this scanner device's camera, or any ESP32-CAM / phone device of that class.
- **Phone camera**: add a "Phone" device, tap Pair, open the link / scan the QR on the phone, tap Start streaming.
- **ESP32-CAM**: add an "ESP32-CAM" device, paste token + server URL into the ESP32-CAM sketch, flash.
- **Screen / speaker**: add a "Display" or "Speaker" device (or one of each with its own token), paste into the screen/speaker sketch, flash. Set `USE_DISPLAY` / `USE_SPEAKER` to 0/1 for what you wired.
- Turn off "Play chime on the scanner device" if the satellite speaker should be the only audio.
- Recognition still runs in `/scan`, so keep a scanner page open (a laptop or an old phone) for each active class.

### Wiring
ST7735 (160x128): SCK 18, SDA/MOSI 23, CS 5, DC/A0 2, RESET 4, LED+VCC 3V3, GND.
PAM8403 speaker amp: ESP32 GPIO25 -> L input, ESP32 GND -> GND, ESP32 5V/VIN -> VCC, speaker on L output +/-.
Use a normal ESP32 dev board for screen + speaker. The ESP32-CAM has too few free pins.

### Arduino libraries
ESP32-CAM: board package Arduino-ESP32 (2.0.17 tested path), board "AI Thinker ESP32-CAM".
Output board: Adafruit GFX, Adafruit ST7735 and ST7789, ArduinoJson 7.
Each `.ino` must live in its own folder named after the sketch.

## Notes
- Photos and snapshots are stored in MongoDB (<=480px JPEG).
- Render free sleeps when idle; the first request is slow. Ping `/healthz` with a cron service if needed.
- Discord webhook URLs get a Discord message (with snapshot for unknown visitors); anything else gets JSON.
- Match threshold is 0.5 (`MATCH_THRESHOLD` in `scan.html`).
- Device tokens grant access to that device's endpoints only. Remove a device to revoke its token.

## Device setup
The ESP32 sketches (see `firmware.html`) need no editing. On first boot they open a hotspot (`Attendance-CAM-Setup` / `Attendance-Screen-Setup`); open `192.168.4.1` and enter WiFi, server URL and token. Reopen setup: hold BOOT 3 s (screen board) or tie GPIO13 to GND for 3 s (camera).
