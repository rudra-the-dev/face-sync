import base64
import csv
import hmac
import io
import json
import os
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from bson import Binary, ObjectId
from bson.errors import InvalidId
from flask import Flask, Response, abort, jsonify, request, send_from_directory
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from werkzeug.exceptions import HTTPException

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

mongo = MongoClient(os.environ["MONGODB_URI"], tz_aware=True)
db = mongo[os.environ.get("MONGODB_DB", "attendance")]
classes, students, logs, devices = db.classes, db.students, db.logs, db.devices
logs.create_index([("class_id", 1), ("timestamp", -1)])
logs.create_index("client_id", unique=True, sparse=True)
students.create_index("class_id")
devices.create_index("token", unique=True)
devices.create_index("class_id")

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# ---------- live device state (in memory; run a single gunicorn worker) ----------
CAMERA_KINDS = ("esp32cam", "phone")
DEVICE_KINDS = CAMERA_KINDS + ("display", "speaker")
FRAMES, LAST_SEEN, EVENTS, DEV_CACHE, SRC_CACHE = {}, {}, {}, {}, {}
state_lock = threading.Lock()


def device_from_token():
    """Authenticate an ESP32 / phone by its token (header X-Device-Token or ?t=)."""
    tok = request.headers.get("X-Device-Token") or request.args.get("t") or ""
    hit = DEV_CACHE.get(tok)
    if hit and hit["exp"] > time.time():
        dev = hit["dev"]
    else:
        d = devices.find_one({"token": tok}) if tok else None
        if not d:
            abort(401, "Bad device token")
        c = classes.find_one({"_id": d["class_id"]}, {"name": 1}) or {}
        dev = {"id": str(d["_id"]), "class_id": str(d["class_id"]), "kind": d["kind"],
               "name": d["name"], "class_name": c.get("name", "")}
        DEV_CACHE[tok] = {"dev": dev, "exp": time.time() + 60}
    LAST_SEEN[dev["id"]] = time.time()
    return dev


def push_event(class_id, typ, name, text):
    with state_lock:
        st = EVENTS.setdefault(str(class_id), {"seq": 0, "items": deque(maxlen=50)})
        st["seq"] += 1
        st["items"].append({"seq": st["seq"], "type": typ, "name": name, "text": text})


# ---------- auth + errors ----------
@app.before_request
def guard():
    if request.path in ("/healthz", "/camera") or request.path.startswith("/api/device/") or not ADMIN_PASSWORD:
        return None
    auth = request.authorization
    if not (auth and hmac.compare_digest(auth.password or "", ADMIN_PASSWORD)):
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Attendance"'})
    return None


@app.errorhandler(HTTPException)
def http_error(e):
    return jsonify(error=e.description), e.code


# ---------- helpers ----------
def oid(v):
    try:
        return ObjectId(v)
    except (InvalidId, TypeError):
        abort(404, "Not found")


def num(v, default):
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return default


def parse_class(d):
    name = (d.get("name") or "").strip()
    if not name:
        abort(400, "Class name is required")
    st, et = d.get("start_time", ""), d.get("end_time", "")
    if not (TIME_RE.match(st) and TIME_RE.match(et)):
        abort(400, "Times must be HH:MM")
    tz = d.get("timezone") or "UTC"
    try:
        ZoneInfo(tz)
    except Exception:
        abort(400, "Unknown timezone")
    mode = d.get("anti_spam_mode", "cooldown")
    if mode not in ("cooldown", "once_per_session"):
        abort(400, "Bad anti-spam mode")
    url = (d.get("webhook_url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        abort(400, "Webhook URL must start with http:// or https://")
    return {
        "name": name,
        "start_time": st,
        "end_time": et,
        "timezone": tz,
        "anti_spam_enabled": bool(d.get("anti_spam_enabled")),
        "anti_spam_mode": mode,
        "cooldown_minutes": num(d.get("cooldown_minutes"), 5),
        "unknown_threshold_sec": num(d.get("unknown_threshold_sec"), 5),
        "webhook_url": url,
        "camera_source": str(d.get("camera_source") or "browser"),
        "audio_local": bool(d.get("audio_local", True)),
    }


def class_out(c):
    out = {k: c.get(k) for k in (
        "name", "start_time", "end_time", "timezone", "anti_spam_enabled",
        "anti_spam_mode", "cooldown_minutes", "unknown_threshold_sec", "webhook_url")}
    out["id"] = str(c["_id"])
    out["camera_source"] = c.get("camera_source", "browser")
    out["audio_local"] = c.get("audio_local", True)
    return out


def parse_ts(s):
    now = datetime.now(timezone.utc)
    if not s:
        return now
    try:
        ts = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return now
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    return min(ts, now)  # never accept timestamps from the future


def in_window(c, ts):
    t = ts.astimezone(ZoneInfo(c["timezone"])).strftime("%H:%M")
    s, e = c["start_time"], c["end_time"]
    return (s <= t <= e) if s <= e else (t >= s or t <= e)


def session_key(c, ts):
    local = ts.astimezone(ZoneInfo(c["timezone"]))
    if c["start_time"] > c["end_time"] and local.strftime("%H:%M") <= c["end_time"]:
        local -= timedelta(days=1)  # overnight class belongs to the day it started
    return f"{c['_id']}:{local.date()}"


def decode_data_url(s):
    if not s or "," not in s:
        return None
    try:
        return base64.b64decode(s.split(",", 1)[1])
    except Exception:
        return None


def fire_webhook(url, text, payload, snapshot=None):
    if not url:
        return

    def run():
        try:
            if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
                if snapshot:
                    requests.post(url, data={"payload_json": json.dumps({"content": text})},
                                  files={"file": ("snapshot.jpg", snapshot, "image/jpeg")}, timeout=10)
                else:
                    requests.post(url, json={"content": text}, timeout=10)
            else:
                requests.post(url, json=payload, timeout=10)
        except Exception as ex:  # webhooks must never break attendance
            app.logger.warning("webhook failed: %s", ex)

    threading.Thread(target=run, daemon=True).start()


# ---------- pages ----------
@app.get("/")
def page_index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/scan")
def page_scan():
    return send_from_directory(BASE_DIR, "scan.html")


@app.get("/healthz")
def healthz():
    return "ok"


# ---------- classes ----------
@app.get("/api/classes")
def list_classes():
    return jsonify([class_out(c) for c in classes.find().sort("name", 1)])


@app.post("/api/classes")
def create_class():
    doc = parse_class(request.get_json(force=True))
    doc["camera_source"] = "browser"
    doc["created_at"] = datetime.now(timezone.utc)
    doc["_id"] = classes.insert_one(doc).inserted_id
    return jsonify(class_out(doc)), 201


@app.put("/api/classes/<cid>")
def update_class(cid):
    _id = oid(cid)
    doc = parse_class(request.get_json(force=True))
    if doc["camera_source"] != "browser" and not devices.find_one(
            {"_id": oid(doc["camera_source"]), "class_id": _id, "kind": {"$in": list(CAMERA_KINDS)}}):
        abort(400, "Camera device not found in this class")
    SRC_CACHE.clear()
    if not classes.update_one({"_id": _id}, {"$set": doc}).matched_count:
        abort(404, "Class not found")
    return jsonify(class_out(classes.find_one({"_id": _id})))


@app.delete("/api/classes/<cid>")
def delete_class(cid):
    _id = oid(cid)
    classes.delete_one({"_id": _id})
    students.delete_many({"class_id": _id})
    logs.delete_many({"class_id": _id})
    for d in devices.find({"class_id": _id}, {"_id": 1}):
        FRAMES.pop(str(d["_id"]), None)
    devices.delete_many({"class_id": _id})
    DEV_CACHE.clear()
    SRC_CACHE.clear()
    return jsonify(ok=True)


# ---------- students ----------
@app.get("/api/classes/<cid>/students")
def list_students(cid):
    out = []
    for s in students.find({"class_id": oid(cid)}, {"image": 0}).sort("name", 1):
        sid = str(s["_id"])
        out.append({"id": sid, "name": s["name"], "image_url": f"/api/students/{sid}/image"})
    return jsonify(out)


@app.post("/api/classes/<cid>/students")
def add_student(cid):
    _id = oid(cid)
    if not classes.find_one({"_id": _id}, {"_id": 1}):
        abort(404, "Class not found")
    name = (request.form.get("name") or "").strip()
    f = request.files.get("image")
    if not name or not f:
        abort(400, "Name and image are required")
    if not (f.mimetype or "").startswith("image/"):
        abort(400, "File must be an image")
    sid = students.insert_one({
        "class_id": _id, "name": name, "image": Binary(f.read()),
        "content_type": f.mimetype, "created_at": datetime.now(timezone.utc),
    }).inserted_id
    return jsonify(id=str(sid), name=name, image_url=f"/api/students/{sid}/image"), 201


@app.get("/api/students/<sid>/image")
def student_image(sid):
    s = students.find_one({"_id": oid(sid)}, {"image": 1, "content_type": 1}) or abort(404, "Not found")
    return Response(bytes(s["image"]), mimetype=s.get("content_type", "image/jpeg"),
                    headers={"Cache-Control": "private, max-age=86400"})


@app.delete("/api/students/<sid>")
def delete_student(sid):
    students.delete_one({"_id": oid(sid)})
    return jsonify(ok=True)


# ---------- attendance ----------
@app.post("/api/attendance")
def mark_attendance():
    d = request.get_json(force=True)
    c = classes.find_one({"_id": oid(d.get("class_id"))}) or abort(404, "Class not found")
    s = students.find_one({"_id": oid(d.get("student_id")), "class_id": c["_id"]}, {"image": 0}) \
        or abort(404, "Student not found")
    ts = parse_ts(d.get("client_ts"))
    client_id = d.get("client_id")

    if client_id and logs.find_one({"client_id": client_id}, {"_id": 1}):
        return jsonify(status="duplicate", name=s["name"])  # already synced
    if not in_window(c, ts):
        return jsonify(status="outside_window", name=s["name"])

    sk = session_key(c, ts)
    if c.get("anti_spam_enabled"):
        q = {"student_id": s["_id"], "status": "present"}
        if c.get("anti_spam_mode") == "once_per_session":
            q["session_key"] = sk
        else:
            cd = timedelta(minutes=c.get("cooldown_minutes", 5))
            q["timestamp"] = {"$gt": ts - cd, "$lt": ts + cd}
        if logs.find_one(q, {"_id": 1}):
            return jsonify(status="duplicate", name=s["name"])

    doc = {
        "class_id": c["_id"], "student_id": s["_id"], "student_name": s["name"],
        "status": "present", "timestamp": ts, "session_key": sk, "has_snapshot": False,
    }
    if client_id:
        doc["client_id"] = client_id
    try:
        logs.insert_one(doc)
    except DuplicateKeyError:
        return jsonify(status="duplicate", name=s["name"])

    local = ts.astimezone(ZoneInfo(c["timezone"])).strftime("%Y-%m-%d %H:%M:%S")
    if datetime.now(timezone.utc) - ts < timedelta(seconds=30):  # skip stale offline syncs
        push_event(c["_id"], "marked", s["name"], "Attendance Marked")
    fire_webhook(
        c.get("webhook_url"),
        f"✅ {s['name']} marked present in {c['name']} at {local}",
        {"event": "attendance_marked", "class": c["name"], "student": s["name"],
         "timestamp_utc": ts.isoformat(), "local_time": local},
    )
    return jsonify(status="marked", name=s["name"], timestamp=ts.isoformat())


@app.post("/api/unknown")
def unknown_visitor():
    d = request.get_json(force=True)
    c = classes.find_one({"_id": oid(d.get("class_id"))}) or abort(404, "Class not found")
    ts = parse_ts(d.get("client_ts"))
    client_id = d.get("client_id")
    if client_id and logs.find_one({"client_id": client_id}, {"_id": 1}):
        return jsonify(status="duplicate")
    if not in_window(c, ts):
        return jsonify(status="outside_window")
    snap = decode_data_url(d.get("snapshot"))
    doc = {
        "class_id": c["_id"], "student_id": None, "student_name": "Unknown",
        "status": "unknown", "timestamp": ts, "session_key": session_key(c, ts),
        "has_snapshot": bool(snap),
    }
    if snap:
        doc["snapshot"] = Binary(snap)
    if client_id:
        doc["client_id"] = client_id
    try:
        lid = logs.insert_one(doc).inserted_id
    except DuplicateKeyError:
        return jsonify(status="duplicate")
    local = ts.astimezone(ZoneInfo(c["timezone"])).strftime("%Y-%m-%d %H:%M:%S")
    if datetime.now(timezone.utc) - ts < timedelta(seconds=30):
        push_event(c["_id"], "unknown", "UNKNOWN", "Visitor detected")
    fire_webhook(
        c.get("webhook_url"),
        f"⚠️ Unknown visitor detected in {c['name']} at {local}",
        {"event": "unknown_visitor", "class": c["name"], "timestamp_utc": ts.isoformat(),
         "local_time": local, "snapshot_url": f"{request.host_url}api/logs/{lid}/snapshot"},
        snapshot=snap,
    )
    return jsonify(status="logged")


# ---------- devices (ESP32-CAM, phone camera, ST7735 display, satellite speaker) ----------
@app.get("/camera")
def page_camera():
    return send_from_directory(BASE_DIR, "camera.html")


@app.get("/api/classes/<cid>/devices")
def list_devices(cid):
    now, out = time.time(), []
    for d in devices.find({"class_id": oid(cid)}).sort("created_at", 1):
        did = str(d["_id"])
        seen = LAST_SEEN.get(did)
        out.append({"id": did, "name": d["name"], "kind": d["kind"], "token": d["token"],
                    "online": bool(seen and now - seen < 20),
                    "seen_ago": int(now - seen) if seen else None})
    return jsonify(out)


@app.post("/api/classes/<cid>/devices")
def add_device(cid):
    _id = oid(cid)
    if not classes.find_one({"_id": _id}, {"_id": 1}):
        abort(404, "Class not found")
    d = request.get_json(force=True)
    name, kind = (d.get("name") or "").strip(), d.get("kind")
    if not name or kind not in DEVICE_KINDS:
        abort(400, "Name and a valid device kind are required")
    doc = {"class_id": _id, "name": name, "kind": kind, "token": secrets.token_urlsafe(24),
           "created_at": datetime.now(timezone.utc)}
    doc["_id"] = devices.insert_one(doc).inserted_id
    return jsonify(id=str(doc["_id"]), name=name, kind=kind, token=doc["token"]), 201


@app.delete("/api/devices/<did>")
def delete_device(did):
    _id = oid(did)
    d = devices.find_one({"_id": _id})
    if d:
        devices.delete_one({"_id": _id})
        classes.update_one({"_id": d["class_id"], "camera_source": did}, {"$set": {"camera_source": "browser"}})
        FRAMES.pop(did, None)
        LAST_SEEN.pop(did, None)
        DEV_CACHE.clear()
        SRC_CACHE.clear()
    return jsonify(ok=True)


@app.post("/api/device/frame")
def device_frame():
    dev = device_from_token()
    if dev["kind"] not in CAMERA_KINDS:
        abort(403, "This device is not a camera")
    data = request.get_data()
    if len(data) < 100 or len(data) > 2 * 1024 * 1024 or data[:2] != b"\xff\xd8":
        abort(400, "Expected a JPEG frame")
    with state_lock:
        prev = FRAMES.get(dev["id"])
        FRAMES[dev["id"]] = {"jpg": data, "seq": (prev["seq"] if prev else 0) + 1, "t": time.time()}
    return "", 204


@app.get("/api/device/events")
def device_events():
    dev = device_from_token()
    with state_lock:
        st = EVENTS.get(dev["class_id"])
        seq = st["seq"] if st else 0
        after = request.args.get("after", "")
        items = [e for e in st["items"] if e["seq"] > int(after)] if (st and after.isdigit()) else []
    return jsonify(seq=seq, class_name=dev["class_name"], device_name=dev["name"], events=items)


@app.get("/api/classes/<cid>/frame")
def class_frame(cid):
    hit = SRC_CACHE.get(cid)
    if hit and hit[1] > time.time():
        src = hit[0]
    else:
        c = classes.find_one({"_id": oid(cid)}, {"camera_source": 1}) or abort(404, "Class not found")
        src = c.get("camera_source", "browser")
        SRC_CACHE[cid] = (src, time.time() + 5)
    if src == "browser":
        abort(409, "This class uses the scanner device's own camera")
    f = FRAMES.get(src)
    if not f or time.time() - f["t"] > 15:
        return Response(status=204, headers={"X-Camera-Online": "0"})
    after = request.args.get("after", "")
    if after.isdigit() and int(after) == f["seq"]:
        return Response(status=204, headers={"X-Camera-Online": "1"})
    return Response(f["jpg"], mimetype="image/jpeg",
                    headers={"X-Frame-Seq": str(f["seq"]), "X-Camera-Online": "1", "Cache-Control": "no-store"})


# ---------- logs ----------
@app.get("/api/logs")
def get_logs():
    q = {}
    if request.args.get("class_id"):
        q["class_id"] = oid(request.args["class_id"])
    fmt = request.args.get("format")
    limit = 100000 if fmt else min(num(request.args.get("limit"), 200), 1000)
    cmap = {c["_id"]: c for c in classes.find()}
    rows = []
    for l in logs.find(q, {"snapshot": 0}).sort("timestamp", -1).limit(limit):
        c = cmap.get(l["class_id"])
        tz = c["timezone"] if c else "UTC"
        rows.append({
            "id": str(l["_id"]),
            "class_name": c["name"] if c else "(deleted)",
            "student_name": l["student_name"],
            "status": l["status"],
            "timestamp": l["timestamp"].isoformat(),
            "local_time": l["timestamp"].astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S"),
            "has_snapshot": l.get("has_snapshot", False),
            "snapshot_url": f"/api/logs/{l['_id']}/snapshot" if l.get("has_snapshot") else "",
        })
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["local_time", "timestamp", "class_name", "student_name",
                                            "status", "snapshot_url"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=attendance.csv"})
    if fmt == "json":
        return Response(json.dumps(rows, indent=2), mimetype="application/json",
                        headers={"Content-Disposition": "attachment; filename=attendance.json"})
    return jsonify(rows)


@app.get("/api/logs/<lid>/snapshot")
def log_snapshot(lid):
    l = logs.find_one({"_id": oid(lid)}, {"snapshot": 1}) or abort(404, "Not found")
    if not l.get("snapshot"):
        abort(404, "No snapshot")
    return Response(bytes(l["snapshot"]), mimetype="image/jpeg")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
