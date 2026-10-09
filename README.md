# Suspicious Activity Detection — Complete FYP

This archive upgrades the existing SAD project rather than creating a separate application. It provides a runnable Flask web application with real browser camera access, observable computer-vision events, session recording/evidence finalization, role-based access, admin settings/audit log, a safe application CLI, Web Bluetooth where supported, and a real hardware-radar local-agent interface.

## Run

```text
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
py app.py
```

Open `http://127.0.0.1:5000`.

Demo accounts:
- Admin: `admin` / `Admin@123`
- Student: `student` / `Student@123`

Change demo passwords before deployment. Set `SAD_SECRET_KEY` to a strong random value.

## Camera

The dashboard uses `navigator.mediaDevices.getUserMedia()` and `enumerateDevices()`. Click **START CAMERA**, choose the detected camera, and grant the real browser permission. Camera labels become available after permission in browsers that protect device names.

The computer-vision layer uses MediaPipe Face Mesh, Hands, and Pose. It records observable events such as face missing, multiple faces, hand detected, and body detected. It does not claim that a person is lying, cheating, nervous, malicious, or has a mental state.

## Session lifecycle and evidence

Starting the camera creates a session and starts `MediaRecorder` when recording is enabled. **END SESSION / FINALIZE** then:
1. stops the recorder;
2. uploads `recording.webm`;
3. saves event data;
4. creates `report.txt`;
5. creates `events.json`;
6. creates `metadata.json`;
7. creates a complete evidence ZIP;
8. marks the database session `COMPLETED`.

Evidence is stored under `data/evidence/<SESSION-ID>/`.

## Roles and access

Roles are `SUPER_ADMIN`, `ADMIN`, `SUPERVISOR`, and `STUDENT`. The included demo accounts are ADMIN and STUDENT. Students can only access sessions belonging to their own user ID. Admin/supervisor evidence routes enforce authorization and every important administrative action is written to `audit_log`.

Passwords are hashed with Werkzeug. SQL statements use parameterized queries. Uploaded recording names are fixed to a safe server-generated filename.

## Admin Console

Admin users can see:
- total/active/suspicious sessions;
- total events and registered users;
- session search;
- event timelines;
- recording/TXT/complete ZIP downloads;
- audit log;
- configurable weights and thresholds;
- recording, buzzer and landmark settings.

## Safe application CLI

The CLI is **not** an operating-system shell. It never calls `cmd`, PowerShell, Bash, `sudo`, `rm`, `del`, Python, Node, or arbitrary processes. It exposes only application-managed data and supports:

```text
ls
cd <directory>
pwd
cat <file>
export <file>
session list
session info <id>
device list
device status
help
clear
```

The current backend CLI intentionally limits filesystem access to application-managed records.

## Bluetooth

The dashboard uses `navigator.bluetooth.requestDevice()` where the browser/platform supports Web Bluetooth. Unsupported platforms display `WEB BLUETOOTH NOT SUPPORTED`; no fake connection is created.

## Wi-Fi/LAN

A normal web page cannot freely enumerate arbitrary LAN hosts. This project therefore does not fake Wi-Fi discovery. Use an authorized local agent for devices the operator controls.

## Real radar hardware

Run the local agent:

```text
python hardware/local_agent.py
```

It listens on `127.0.0.1:8765`.

Authorized hardware posts real data to:

```text
POST http://127.0.0.1:8765/radar
```

Example:

```json
{"targets":[{"id":1,"angle":35,"distance":0.42,"strength":0.8}]}
```

The browser receives it from:

```text
ws://127.0.0.1:8765/ws
```

If the hardware is absent, the dashboard shows `NO SIGNAL`. It does not invent radar targets.

## Database

SQLite is used for immediate local development. The schema uses foreign keys and is structured so a PostgreSQL migration can be introduced later. The database file is `sad.db`.

## Important browser limitations

- Camera, Bluetooth and Web APIs are permission-controlled by the browser.
- MediaPipe assets are loaded from jsDelivr, so the first load needs network access unless the model assets are vendored locally.
- Web Bluetooth is browser/platform dependent.
- LAN discovery requires an authorized local agent.
- Hardware radar requires a real sensor/gateway.

## Security hardening for deployment

Use HTTPS, a strong secret, secure cookies, CSRF protection for state-changing browser forms where appropriate, authentication rate limiting, a production identity provider, PostgreSQL/object storage, and an authenticated local-agent channel. The included local agent intentionally does not expose shell execution or arbitrary network scanning.


### LAN live student camera
The app now binds to `0.0.0.0` by default so an authorized admin can open the same app from another device on the same LAN. A student must log in, allow browser camera permission, and turn the camera ON. The browser publishes temporary JPEG frames to the app and the admin console displays the active student feed. Frames are kept in memory only and expire automatically when the feed stops.
