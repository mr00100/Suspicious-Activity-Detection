import io, json, os, re, sqlite3, uuid, zipfile, datetime, pathlib, threading, time
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, send_from_directory, abort, send_file, redirect
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

try:
    from flask_sock import Sock
except ImportError:
    Sock = None

BASE = pathlib.Path(__file__).resolve().parent
DATA = BASE / "data"
EVIDENCE = DATA / "evidence"
UPLOADS = DATA / "uploads"
REPORTS = DATA / "reports"
STUDENT_URL = os.environ.get("SAD_STUDENT_URL", "/")
ADMIN_URL = os.environ.get("SAD_ADMIN_URL", "/admin")
for p in (DATA, EVIDENCE, UPLOADS, REPORTS):
    p.mkdir(parents=True, exist_ok=True)
DB = BASE / "sad.db"

app = Flask(__name__)
app.secret_key = os.environ.get("SAD_SECRET_KEY", "change-this-secret-before-deployment")
app.config.update(
    MAX_CONTENT_LENGTH=300 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SAD_SECURE_COOKIE", "0") == "1",
)
sock = Sock(app) if Sock else None

# Consent-based live student camera relay. Frames stay in memory only.
live_camera_lock = threading.Lock()
live_camera = {"session_id": None, "user_id": None, "username": None, "frame": None, "updated": 0.0, "score": 0}
# Latest authorized radar sample relayed by a student browser to the admin console.
live_radar_lock = threading.Lock()
live_radar = {"session_id": None, "user_id": None, "username": None, "targets": [], "updated": 0.0, "state": "OFF"}

DEFAULT_SETTINGS = {
    "face_missing_weight": 10, "multiple_person_weight": 30,
    "camera_obstruction_weight": 25, "unexpected_movement_weight": 10,
    "hand_detection_weight": 5, "person_left_weight": 20,
    "warning_threshold": 31, "suspicious_threshold": 61, "critical_threshold": 81,
    "score_decay": 2, "recording_enabled": 1, "buzzer_enabled": 1, "landmarks_enabled": 1,
    "radar_enabled": 1,
}

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('SUPER_ADMIN','ADMIN','SUPERVISOR','STUDENT','USER'))
    );
    CREATE TABLE IF NOT EXISTS sessions(
      id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
      status TEXT NOT NULL DEFAULT 'ACTIVE', max_score INTEGER DEFAULT 0, final_score INTEGER DEFAULT 0,
      FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, event_type TEXT NOT NULL,
      severity TEXT NOT NULL, score_change INTEGER DEFAULT 0, confidence REAL, description TEXT,
      metadata TEXT, created_at TEXT NOT NULL, FOREIGN KEY(session_id) REFERENCES sessions(id)
    );
    CREATE TABLE IF NOT EXISTS evidence(
      id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT UNIQUE NOT NULL,
      recording_filename TEXT, report_filename TEXT, events_filename TEXT, metadata_filename TEXT,
      zip_filename TEXT, finalized_at TEXT, FOREIGN KEY(session_id) REFERENCES sessions(id)
    );
    CREATE TABLE IF NOT EXISTS devices(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, device_type TEXT NOT NULL,
      transport TEXT NOT NULL, identifier TEXT, ip TEXT, status TEXT NOT NULL DEFAULT 'DISCONNECTED',
      last_seen TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS settings(
      key TEXT PRIMARY KEY, value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS audit_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT NOT NULL, target TEXT,
      details TEXT, timestamp TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)
    for u,p,r in [("admin","Admin@123","ADMIN"),("student","Student@123","STUDENT")]:
        if not c.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone():
            c.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                      (u, generate_password_hash(p), r))
    for k,v in DEFAULT_SETTINGS.items():
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,str(v)))
    c.commit(); c.close()

def current_user():
    if "user_id" not in session: return None
    c=db(); u=c.execute("SELECT id,username,role FROM users WHERE id=?", (session["user_id"],)).fetchone(); c.close()
    return u

def login_required(f):
    @wraps(f)
    def w(*a,**kw):
        if not current_user(): return jsonify(error="Authentication required"),401
        return f(*a,**kw)
    return w

def role_required(*roles):
    def deco(f):
        @wraps(f)
        def w(*a,**kw):
            u=current_user()
            if not u: return jsonify(error="Authentication required"),401
            if u["role"] not in roles: return jsonify(error="Insufficient permissions"),403
            return f(*a,**kw)
        return w
    return deco

def audit(action,target="",details=""):
    u=current_user()
    c=db(); c.execute("INSERT INTO audit_log(user_id,action,target,details,timestamp) VALUES(?,?,?,?,?)",
                      (u["id"] if u else None,action,target,details,now())); c.commit(); c.close()

def settings_dict():
    c=db(); rows=c.execute("SELECT key,value FROM settings").fetchall(); c.close()
    out={}
    for r in rows:
        try: out[r["key"]]=int(r["value"])
        except: out[r["key"]]=r["value"]
    return out

def session_for_user(sid, uid):
    c=db(); s=c.execute("SELECT * FROM sessions WHERE id=? AND user_id=?", (sid,uid)).fetchone(); c.close(); return s

def evidence_dir(sid):
    p=EVIDENCE/secure_filename(sid); p.mkdir(parents=True,exist_ok=True); return p

def safe_session_path(sid, filename):
    p=(evidence_dir(sid)/secure_filename(filename)).resolve()
    if evidence_dir(sid).resolve() not in p.parents: abort(400)
    return p

@app.route("/")
def index():
    u=current_user()
    if not u:
        return render_template("login.html")
    if u["role"] in ("SUPER_ADMIN","ADMIN","SUPERVISOR"):
        return redirect(ADMIN_URL)
    return render_template("dashboard.html")

@app.get("/admin")
def admin_page():
    u=current_user()
    if not u or u["role"] not in ("SUPER_ADMIN","ADMIN","SUPERVISOR"): return render_template("login.html")
    return render_template("admin.html")

@app.post("/api/login")
def api_login():
    d=request.get_json() or {}; username=str(d.get("username",""))[:80]; password=str(d.get("password",""))[:200]
    c=db(); u=c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone(); c.close()
    if not u or not check_password_hash(u["password_hash"],password): return jsonify(error="Invalid credentials"),401
    session.clear(); session.update(user_id=u["id"],username=u["username"],role=u["role"])
    audit("ADMIN LOGIN" if u["role"] in ("ADMIN","SUPER_ADMIN") else "LOGIN",username,"Successful login")
    return jsonify(ok=True,role=u["role"],username=u["username"],redirect_url=(ADMIN_URL if u["role"] in ("SUPER_ADMIN","ADMIN","SUPERVISOR") else STUDENT_URL))

@app.post("/api/logout")
def logout():
    audit("LOGOUT",session.get("username",""),"Session ended"); session.clear(); return jsonify(ok=True)

@app.get("/api/me")
def me():
    u=current_user(); return jsonify(authenticated=bool(u),username=u["username"] if u else None,role=u["role"] if u else None)

@app.post("/api/sessions")
@login_required
def start_session():
    sid=str(uuid.uuid4()); t=now(); c=db()
    c.execute("INSERT INTO sessions(id,user_id,started_at) VALUES(?,?,?)",(sid,session["user_id"],t)); c.commit(); c.close()
    audit("SESSION STARTED",sid); return jsonify(session_id=sid,started_at=t)

@app.post("/api/sessions/<sid>/events")
@login_required
def save_events(sid):
    s=session_for_user(sid,session["user_id"])
    if not s:return jsonify(error="Session not found"),404
    events=request.get_json() or []; events=events if isinstance(events,list) else []
    c=db(); max_score=int(request.args.get("max_score",0))
    for e in events[:500]:
        c.execute("""INSERT INTO events(session_id,event_type,severity,score_change,confidence,description,metadata,created_at)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (sid,str(e.get("type","UNKNOWN"))[:80],str(e.get("severity","LOW"))[:20],
                   int(e.get("score_change",0)),float(e.get("confidence",0) or 0),
                   str(e.get("description",""))[:500],json.dumps(e.get("metadata",{}))[:2000],
                   e.get("created_at") or now()))
    c.execute("UPDATE sessions SET max_score=MAX(max_score,?),final_score=? WHERE id=?",(max_score,max_score,sid))
    c.commit(); c.close(); return jsonify(ok=True)

@app.post("/api/sessions/<sid>/recording")
@login_required
def recording(sid):
    if not session_for_user(sid,session["user_id"]): return jsonify(error="Session not found"),404
    f=request.files.get("video")
    if not f or not f.filename:return jsonify(error="No video received"),400
    p=safe_session_path(sid,"recording.webm"); f.save(p)
    c=db(); c.execute("INSERT INTO evidence(session_id,recording_filename) VALUES(?,?) ON CONFLICT(session_id) DO UPDATE SET recording_filename=excluded.recording_filename",(sid,p.name))
    c.commit(); c.close(); audit("RECORDING DOWNLOADED" if False else "RECORDING SAVED",sid); return jsonify(ok=True,filename=p.name)

@app.post("/api/sessions/<sid>/finish")
@login_required
def finish(sid):
    s=session_for_user(sid,session["user_id"])
    if not s:return jsonify(error="Session not found"),404
    t=now(); ed=evidence_dir(sid)
    c=db(); ev=c.execute("SELECT * FROM events WHERE session_id=? ORDER BY created_at",(sid,)).fetchall()
    peak=max([int(s["max_score"] or 0)]+[int(x["score_change"] or 0) for x in ev])
    c.execute("UPDATE sessions SET ended_at=?,status='FINALIZING',max_score=MAX(max_score,?) WHERE id=?",(t,peak,sid)); c.commit()
    report=ed/"report.txt"
    lines=["SUSPICIOUS ACTIVITY DETECTION","SESSION REPORT","",
           f"Session ID: {sid}",f"User: {session.get('username')}",f"Start Time: {s['started_at']}",f"End Time: {t}",
           f"Peak Suspicion: {max(0,min(100,int(s['max_score'] or 0)))}",f"Final Suspicion: {max(0,min(100,int(s['final_score'] or 0)))}","",
           "Detection Metrics:"]
    counts={}
    for x in ev: counts[x["event_type"]]=counts.get(x["event_type"],0)+1
    for k in ["FACE_MISSING","MULTIPLE_PERSONS","HAND_DETECTED","BODY_DETECTED","CAMERA_OBSTRUCTED","RADAR_TARGET_DETECTED"]:
        lines.append(f"{k}: {counts.get(k,0)}")
    lines += ["","Event Timeline:"]
    for x in ev: lines.append(f"[{x['created_at']}] {x['event_type']} | {x['severity']} | +{x['score_change']} | {x['description']}")
    report.write_text("\n".join(lines),encoding="utf-8")
    (ed/"events.json").write_text(json.dumps([dict(x) for x in ev],indent=2),encoding="utf-8")
    meta={"session_id":sid,"user":session.get("username"),"started_at":s["started_at"],"ended_at":t,"status":"COMPLETED","peak_suspicion":int(s["max_score"] or 0)}
    (ed/"metadata.json").write_text(json.dumps(meta,indent=2),encoding="utf-8")
    zipname=f"{sid}.zip"; zpath=ed/zipname
    with zipfile.ZipFile(zpath,"w",zipfile.ZIP_DEFLATED) as z:
        for p in ed.iterdir():
            if p.name!=zipname: z.write(p,p.name)
    c.execute("""INSERT INTO evidence(session_id,report_filename,events_filename,metadata_filename,zip_filename,finalized_at)
                 VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET report_filename=excluded.report_filename,
                 events_filename=excluded.events_filename,metadata_filename=excluded.metadata_filename,zip_filename=excluded.zip_filename,finalized_at=excluded.finalized_at""",
              (sid,report.name,"events.json","metadata.json",zipname,t))
    c.execute("UPDATE sessions SET status='COMPLETED',ended_at=? WHERE id=?",(t,sid)); c.commit(); c.close()
    audit("SESSION FINISHED",sid,"Evidence finalized"); return jsonify(ok=True,report=report.name,zip=zipname)

def can_view_session(sid):
    u=current_user()
    if not u:return False
    c=db(); s=c.execute("SELECT user_id FROM sessions WHERE id=?",(sid,)).fetchone(); c.close()
    return bool(s and (u["role"] in ("ADMIN","SUPER_ADMIN","SUPERVISOR") or s["user_id"]==u["id"]))

@app.get("/api/sessions/<sid>/evidence/<name>")
@login_required
def own_evidence(sid,name):
    if not can_view_session(sid):return jsonify(error="Forbidden"),403
    p=safe_session_path(sid,name)
    if not p.exists() or not p.is_file():abort(404)
    audit("EVIDENCE DOWNLOADED",sid,name); return send_file(p,as_attachment=True,download_name=p.name)

@app.get("/api/admin/summary")
@role_required("SUPER_ADMIN","ADMIN","SUPERVISOR")
def admin_summary():
    c=db()
    total=c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"]
    active=c.execute("SELECT COUNT(*) n FROM sessions WHERE status='ACTIVE'").fetchone()["n"]
    suspicious=c.execute("SELECT COUNT(*) n FROM sessions WHERE max_score>=61").fetchone()["n"]
    events=c.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
    users=c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    c.close(); return jsonify(total_sessions=total,active_sessions=active,suspicious_sessions=suspicious,total_events=events,registered_users=users)

@app.get("/api/admin/sessions")
@role_required("SUPER_ADMIN","ADMIN","SUPERVISOR")
def admin_sessions():
    c=db(); rows=c.execute("""SELECT s.*,u.username,e.recording_filename,e.report_filename,e.events_filename,e.metadata_filename,e.zip_filename
                             FROM sessions s JOIN users u ON u.id=s.user_id LEFT JOIN evidence e ON e.session_id=s.id
                             ORDER BY s.started_at DESC""").fetchall(); c.close()
    return jsonify(sessions=[dict(x) for x in rows])

@app.get("/api/admin/sessions/<sid>/events")
@role_required("SUPER_ADMIN","ADMIN","SUPERVISOR")
def admin_events(sid):
    c=db(); rows=c.execute("SELECT * FROM events WHERE session_id=? ORDER BY created_at",(sid,)).fetchall(); c.close()
    audit("SESSION VIEWED",sid); return jsonify(events=[dict(x) for x in rows])

@app.get("/api/admin/audit")
@role_required("SUPER_ADMIN","ADMIN")
def admin_audit():
    c=db(); rows=c.execute("""SELECT a.*,u.username FROM audit_log a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.timestamp DESC LIMIT 500""").fetchall(); c.close()
    return jsonify(audit=[dict(x) for x in rows])

@app.get("/api/admin/evidence-list")
@role_required("SUPER_ADMIN","ADMIN","SUPERVISOR")
def admin_evidence_list():
    rows=[]
    for d in sorted(EVIDENCE.glob("*"), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True):
        if not d.is_dir():
            continue
        sid=d.name
        c=db(); srow=c.execute("SELECT s.id,u.username,s.started_at,s.status FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=?",(sid,)).fetchone(); c.close()
        files=[]
        for f in sorted(d.iterdir(), key=lambda x:x.name.lower()):
            if f.is_file():
                files.append({"name":f.name,"size":f.stat().st_size,"url":f"/api/admin/evidence/{sid}/{secure_filename(f.name)}"})
        rows.append({"session_id":sid,"username":srow["username"] if srow else "Unknown","started_at":srow["started_at"] if srow else "","status":srow["status"] if srow else "","files":files})
    audit("EVIDENCE FOLDER VIEWED","admin")
    return jsonify(folders=rows)

@app.get("/api/admin/evidence/<sid>/<name>")
@role_required("SUPER_ADMIN","ADMIN","SUPERVISOR")
def admin_evidence(sid,name):
    if not can_view_session(sid):return jsonify(error="Forbidden"),403
    p=safe_session_path(sid,name)
    if not p.exists():abort(404)
    audit("EVIDENCE EXPORTED",sid,name); return send_file(p,as_attachment=True,download_name=p.name)

@app.get("/api/admin/evidence/<sid>/<name>/view")
@role_required("SUPER_ADMIN","ADMIN","SUPERVISOR")
def admin_evidence_view(sid,name):
    if not can_view_session(sid):return jsonify(error="Forbidden"),403
    p=safe_session_path(sid,name)
    if not p.exists() or not p.is_file():abort(404)
    if p.suffix.lower() not in {".txt",".json",".log"}: return jsonify(error="Only text evidence can be viewed inline"),415
    audit("EVIDENCE VIEWED",sid,name); return send_file(p,as_attachment=False,mimetype="text/plain; charset=utf-8")

@app.post("/api/live-camera/frame")
@login_required
def live_camera_frame():
    u=current_user()
    if u["role"] != "STUDENT": return jsonify(error="Only a student session can publish a live camera frame"),403
    sid=str(request.form.get("session_id", ""))
    if not sid or not session_for_user(sid,u["id"]): return jsonify(error="Session not found"),404
    f=request.files.get("frame")
    if not f: return jsonify(error="No frame received"),400
    data=f.read(700*1024+1)
    if not data or len(data)>700*1024: return jsonify(error="Invalid frame size"),400
    try: live_score=max(0,min(100,int(float(request.form.get("score",0) or 0))))
    except Exception: live_score=0
    with live_camera_lock:
        live_camera.update(session_id=sid,user_id=u["id"],username="RAIMA",frame=data,updated=time.time(),score=live_score)
    return jsonify(ok=True)

@app.post("/api/live-camera/stop")
@login_required
def live_camera_stop():
    u=current_user()
    with live_camera_lock:
        if live_camera.get("user_id") == u["id"]:
            live_camera.update(session_id=None,user_id=None,username=None,frame=None,updated=0.0,score=0)
    return jsonify(ok=True)

@app.get("/api/admin/live-camera")
@role_required("SUPER_ADMIN","ADMIN","SUPERVISOR")
def admin_live_camera():
    with live_camera_lock:
        fresh = bool(live_camera.get("frame")) and (time.time()-live_camera.get("updated",0) < 5.0)
        if not fresh:
            return jsonify(active=False)
        return jsonify(active=True,session_id=live_camera["session_id"],username=live_camera["username"],updated=live_camera["updated"],score=live_camera.get("score",0))

@app.get("/api/admin/live-camera/frame")
@role_required("SUPER_ADMIN","ADMIN","SUPERVISOR")
def admin_live_camera_frame():
    from flask import Response
    with live_camera_lock:
        fresh = bool(live_camera.get("frame")) and (time.time()-live_camera.get("updated",0) < 5.0)
        data = live_camera.get("frame") if fresh else None
    if not data: return Response(status=204)
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0"})

@app.get("/api/admin/live-camera/stream")
@role_required("SUPER_ADMIN","ADMIN","SUPERVISOR")
def admin_live_camera_stream():
    from flask import Response
    def generate():
        last_seen=0.0
        last_heartbeat=time.time()
        # Send ONLY valid JPEG parts. Text parts inside an <img> MJPEG stream can
        # make Chromium treat the stream as a broken/black image.
        while True:
            with live_camera_lock:
                fresh=bool(live_camera.get("frame")) and (time.time()-live_camera.get("updated",0)<5.0)
                data=live_camera.get("frame") if fresh else None
                stamp=live_camera.get("updated",0)
            if data and stamp != last_seen:
                last_seen=stamp
                yield b"--frame\r\nContent-Type: image/jpeg\r\nCache-Control: no-cache\r\n\r\n" + data + b"\r\n"
                last_heartbeat=time.time()
            time.sleep(0.04)
    return Response(generate(),mimetype="multipart/x-mixed-replace; boundary=frame",headers={"Cache-Control":"no-cache, no-store, must-revalidate, max-age=0","Pragma":"no-cache","X-Accel-Buffering":"no"})

@app.post("/api/live-radar/frame")
@login_required
def live_radar_frame():
    u=current_user()
    if u["role"] != "STUDENT":
        return jsonify(error="Only a student session can publish radar data"),403
    sid=str((request.get_json() or {}).get("session_id", ""))
    if not sid or not session_for_user(sid,u["id"]):
        return jsonify(error="Session not found"),404
    raw=(request.get_json() or {}).get("targets",[])
    if not isinstance(raw,list):
        return jsonify(error="Invalid radar payload"),400
    cleaned=[]
    for t in raw[:100]:
        try:
            cleaned.append({"id":t.get("id"),"angle":float(t.get("angle",0)),"distance":float(t.get("distance",0)),"strength":float(t.get("strength",0))})
        except Exception:
            continue
    with live_radar_lock:
        live_radar.update(session_id=sid,user_id=u["id"],username="RAIMA",targets=cleaned,updated=time.time(),state="ON")
    return jsonify(ok=True,active=True,targets=cleaned)

@app.post("/api/live-radar/heartbeat")
@login_required
def live_radar_heartbeat():
    u=current_user()
    if u["role"] != "STUDENT":
        return jsonify(error="Only a student session can publish radar status"),403
    sid=str((request.get_json() or {}).get("session_id", ""))
    if not sid or not session_for_user(sid,u["id"]):
        return jsonify(error="Session not found"),404
    with live_radar_lock:
        live_radar.update(session_id=sid,user_id=u["id"],username="RAIMA",updated=time.time(),state="ON")
    return jsonify(ok=True,active=True)

@app.post("/api/live-radar/stop")
@login_required
def live_radar_stop():
    u=current_user()
    with live_radar_lock:
        if live_radar.get("user_id") == u["id"]:
            live_radar.update(session_id=None,user_id=None,username=None,targets=[],updated=0.0,state="OFF")
    return jsonify(ok=True)

@app.get("/api/admin/live-radar")
@role_required("SUPER_ADMIN","ADMIN","SUPERVISOR")
def admin_live_radar():
    with live_radar_lock:
        fresh=bool(live_radar.get("updated")) and (time.time()-live_radar.get("updated",0)<2.5)
        if not fresh:
            return jsonify(active=False,targets=[])
        return jsonify(active=True,session_id=live_radar["session_id"],username=live_radar["username"],targets=live_radar["targets"],updated=live_radar["updated"],state=live_radar.get("state","ON"))

@app.get("/api/devices")
@login_required
def devices():
    c=db(); rows=c.execute("SELECT * FROM devices ORDER BY id DESC").fetchall(); c.close(); return jsonify(devices=[dict(x) for x in rows])

@app.post("/api/devices")
@login_required
def add_device():
    d=request.get_json() or {}; c=db()
    c.execute("""INSERT INTO devices(name,device_type,transport,identifier,ip,status,last_seen,created_at)
                 VALUES(?,?,?,?,?,?,?,?)""",(str(d.get("name","Unknown"))[:100],str(d.get("type","Unknown"))[:50],
                 str(d.get("transport","Unknown"))[:50],str(d.get("identifier",""))[:200],str(d.get("ip",""))[:64],
                 str(d.get("status","CONNECTED"))[:30],now(),now()))
    c.commit();c.close();return jsonify(ok=True)

@app.get("/api/settings")
@role_required("SUPER_ADMIN","ADMIN")
def get_settings(): return jsonify(settings=settings_dict())

@app.post("/api/settings")
@role_required("SUPER_ADMIN","ADMIN")
def save_settings():
    d=request.get_json() or {}; c=db()
    allowed=set(DEFAULT_SETTINGS)
    for k,v in d.items():
        if k not in allowed:continue
        if isinstance(v,bool):v=int(v)
        if k.endswith("_threshold"):v=max(0,min(100,int(v)))
        elif k=="score_decay":v=max(0,min(20,int(v)))
        elif k.endswith("_weight"):v=max(0,min(100,int(v)))
        elif k in ("recording_enabled","buzzer_enabled","landmarks_enabled","radar_enabled"):v=int(bool(v))
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,str(v)))
    c.commit();c.close();audit("SETTINGS CHANGED","settings");return jsonify(ok=True,settings=settings_dict())

def _cli_virtual_path(cwd, arg):
    """Resolve a path only inside the application's virtual storage tree."""
    import posixpath
    cwd = cwd if cwd.startswith('/') else '/'
    if arg.startswith('/'):
        path=arg
    else:
        path=posixpath.join(cwd,arg)
    path=posixpath.normpath(path)
    if not path.startswith('/'): path='/'+path
    return path

def _cli_real_path(vpath):
    # Virtual root maps only to known application-owned directories.
    if vpath=='/': return DATA
    if vpath=='/evidence': return EVIDENCE
    if vpath.startswith('/evidence/'):
        rest=vpath[len('/evidence/'):]
        return (EVIDENCE / rest).resolve()
    if vpath=='/uploads': return UPLOADS
    if vpath.startswith('/uploads/'):
        return (UPLOADS / vpath[len('/uploads/'):]).resolve()
    if vpath=='/reports': return REPORTS
    if vpath.startswith('/reports/'):
        return (REPORTS / vpath[len('/reports/'):]).resolve()
    return None

def _cli_safe(real):
    if real is None:return False
    for root in (DATA,EVIDENCE,UPLOADS,REPORTS):
        try:
            if real.resolve()==root.resolve() or root.resolve() in real.resolve().parents:return True
        except Exception: pass
    return False

@app.post("/api/cli")
@role_required("SUPER_ADMIN","ADMIN","SUPERVISOR")
def cli():
    d=request.get_json() or {}; cmd=str(d.get("command","" )).strip(); cwd=str(d.get("cwd","/"))
    if len(cmd)>300:return jsonify(error="Command too long"),400
    parts=cmd.split(); name=parts[0].lower() if parts else ''; args=parts[1:]
    if name in {"bash","sh","cmd","powershell","sudo","rm","del","exec","python","node","format","shutdown","reboot"}:
        audit("CLI BLOCKED",name,"OS command execution is disabled")
        return jsonify(output="OS commands are disabled. This terminal only accesses SAD application storage.",cwd=cwd)
    if not cwd.startswith('/'):cwd='/'
    if name in ('clear','cls'):return jsonify(output='',cwd=cwd,clear=True)
    if name=='help':
        out="""Application commands:
  ls [path]              list application storage
  cd <directory>         change virtual directory
  pwd                    print virtual directory
  cat <file>             read a text application file
  export <file>          download an application file
  session list           list authorized sessions
  session info <id>      show session information
  user list              list database users
  user info <username>   show database user activity summary
  user report <username> generate a linked TXT activity report
  device list            list registered devices
  device status          show device center status
  help                   show this help
  clear                  clear terminal"""
        return jsonify(output=out,cwd=cwd)
    if name=='pwd':return jsonify(output=cwd,cwd=cwd)
    if name=='cd':
        target=_cli_virtual_path(cwd,args[0] if args else '/')
        real=_cli_real_path(target)
        if target=='/' or (_cli_safe(real) and real.exists() and real.is_dir()):return jsonify(output='',cwd=target)
        return jsonify(output=f"cd: {args[0] if args else ''}: directory not found",cwd=cwd)
    if name=='ls':
        target=_cli_virtual_path(cwd,args[0] if args else '.')
        real=_cli_real_path(target)
        if not _cli_safe(real) or not real.exists() or not real.is_dir():return jsonify(output='ls: path not found',cwd=cwd)
        items=[]
        for x in sorted(real.iterdir(),key=lambda z:(not z.is_dir(),z.name.lower())):
            items.append(x.name+('/' if x.is_dir() else ''))
        return jsonify(output='\n'.join(items) or '(empty)',cwd=cwd)
    if name=='cat':
        if not args:return jsonify(output='cat: missing file operand',cwd=cwd)
        target=_cli_virtual_path(cwd,args[0]); real=_cli_real_path(target)
        if not _cli_safe(real) or not real.exists() or not real.is_file():return jsonify(output='cat: file not found',cwd=cwd)
        if real.stat().st_size>2*1024*1024:return jsonify(output='cat: file is too large; use export',cwd=cwd)
        try:return jsonify(output=real.read_text(encoding='utf-8',errors='replace'),cwd=cwd)
        except Exception:return jsonify(output='cat: binary file; use export',cwd=cwd)
    if name=='export':
        if not args:return jsonify(output='export: missing file operand',cwd=cwd)
        target=_cli_virtual_path(cwd,args[0]); real=_cli_real_path(target)
        if not _cli_safe(real) or not real.exists() or not real.is_file():return jsonify(output='export: file not found',cwd=cwd)
        audit('EVIDENCE EXPORTED','cli',target)
        # The download route re-checks the same virtual-root restriction.
        from urllib.parse import quote
        return jsonify(output=f"Preparing download: {target}",cwd=cwd,download='/api/cli/export?path='+quote(target,safe=''))
    if name=='user' and args and args[0]=='list':
        cdb=db(); rows=cdb.execute("SELECT username,role FROM users ORDER BY username").fetchall(); cdb.close()
        out='\n'.join(f"{r['username']} | role={r['role']}" for r in rows) or '(empty)'
        return jsonify(output=out,cwd=cwd)
    if name=='user' and len(args)>1 and args[0]=='info':
        target=args[1][:80]; cdb=db()
        urow=cdb.execute("SELECT id,username,role FROM users WHERE username=?",(target,)).fetchone()
        if not urow:
            cdb.close(); return jsonify(output='user: not found',cwd=cwd)
        ss=cdb.execute("SELECT COUNT(*) n, MAX(started_at) last_started, MAX(max_score) max_score FROM sessions WHERE user_id=?",(urow['id'],)).fetchone()
        ev=cdb.execute("SELECT COUNT(*) n FROM events WHERE session_id IN (SELECT id FROM sessions WHERE user_id=?)",(urow['id'],)).fetchone()['n']; cdb.close()
        return jsonify(output=f"username={urow['username']}\nrole={urow['role']}\nsessions={ss['n']}\nlast_session={ss['last_started'] or '-'}\nmax_suspicion={ss['max_score'] or 0}\nevents={ev}",cwd=cwd)
    if name=='user' and len(args)>1 and args[0]=='report':
        target=args[1][:80]; cdb=db(); urow=cdb.execute("SELECT id,username,role FROM users WHERE username=?",(target,)).fetchone()
        if not urow:
            cdb.close(); return jsonify(output='user: not found',cwd=cwd)
        sessions=cdb.execute("SELECT id,started_at,ended_at,status,max_score,final_score FROM sessions WHERE user_id=? ORDER BY started_at DESC",(urow['id'],)).fetchall()
        events=cdb.execute("SELECT e.created_at,e.event_type,e.severity,e.score_change,e.description FROM events e JOIN sessions s ON s.id=e.session_id WHERE s.user_id=? ORDER BY e.created_at",(urow['id'],)).fetchall(); cdb.close()
        out=["SAD USER ACTIVITY REPORT","",f"Username: {urow['username']}",f"Role: {urow['role']}",f"Sessions: {len(sessions)}","", "Sessions:"]
        for x in sessions: out.append(f"{x['id']} | {x['started_at']} | {x['status']} | peak={x['max_score'] or 0} | final={x['final_score'] or 0}")
        out += ["","Event Timeline:"]
        for x in events: out.append(f"[{x['created_at']}] {x['event_type']} | {x['severity']} | +{x['score_change']} | {x['description']}")
        rp=REPORTS/"users"/f"{secure_filename(urow['username'])}.txt"; rp.write_text("\n".join(out),encoding='utf-8')
        audit('USER REPORT GENERATED',urow['username'],'CLI'); return jsonify(output=f"User report generated: /reports/users/{rp.name}",cwd=cwd)
    if name=='device' and args and args[0]=='list':
        cdb=db(); rows=cdb.execute("SELECT name,device_type,ip,status,last_seen FROM devices ORDER BY id DESC").fetchall(); cdb.close()
        out='\n'.join(f"{r['name']} | {r['device_type']} | {r['ip'] or '-'} | {r['status']} | {r['last_seen'] or '-'}" for r in rows) or '(empty)'
        return jsonify(output=out,cwd=cwd)
    if name=='device' and args and args[0]=='status':return jsonify(output='Device Center uses real browser/local-agent integrations. No OS shell access is provided.',cwd=cwd)
    if name=='session' and args and args[0]=='list':
        cdb=db(); rows=cdb.execute("SELECT id,started_at,status,max_score FROM sessions ORDER BY started_at DESC LIMIT 50").fetchall(); cdb.close()
        out='\n'.join(f"{r['id']} | {r['started_at']} | {r['status']} | score={r['max_score']}" for r in rows) or '(empty)'
        return jsonify(output=out,cwd=cwd)
    if name=='session' and len(args)>1 and args[0]=='info':
        target=args[1]; cdb=db(); ss=cdb.execute("SELECT s.*,u.username FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=?",(target,)).fetchone(); ev=cdb.execute("SELECT COUNT(*) n FROM events WHERE session_id=?",(target,)).fetchone()['n']; cdb.close()
        if not ss:return jsonify(output='session: not found',cwd=cwd)
        return jsonify(output=f"session={target}\nuser={ss['username']}\nstatus={ss['status']}\nstarted={ss['started_at']}\nevents={ev}\nmax_score={ss['max_score']}",cwd=cwd)
    return jsonify(output='Unknown command. Type help.',cwd=cwd)

@app.get('/api/cli/export')
@role_required('SUPER_ADMIN','ADMIN','SUPERVISOR')
def cli_export():
    target=request.args.get('path','/'); real=_cli_real_path(target)
    if not _cli_safe(real) or not real.exists() or not real.is_file():return jsonify(error='File not found'),404
    audit('EVIDENCE EXPORTED','cli',target)
    return send_file(real,as_attachment=True,download_name=real.name)

@app.get("/api/health")
def health(): return jsonify(ok=True,database=DB.exists(),radar="websocket" if sock else "unavailable")

if sock:
    @sock.route("/ws/radar")
    def radar_ws(ws):
        # One-way receiver: local hardware agent sends JSON; browser clients can use a separate
        # WebSocket gateway in production. This endpoint validates and stores last-seen hardware data.
        while True:
            msg=ws.receive()
            if msg is None: break
            try:
                data=json.loads(msg)
                targets=data.get("targets",[])
                if not isinstance(targets,list): raise ValueError
                cleaned=[]
                for t in targets[:100]:
                    cleaned.append({"id":t.get("id"),"angle":float(t.get("angle",0)),"distance":float(t.get("distance",0)),"strength":float(t.get("strength",0))})
                ws.send(json.dumps({"type":"radar","targets":cleaned}))
            except Exception:
                ws.send(json.dumps({"type":"error","message":"Invalid radar payload"}))

if __name__=="__main__":
    init_db()
    app.run(host=os.environ.get("SAD_HOST","0.0.0.0"),port=int(os.environ.get("SAD_PORT","5000")),debug=False)
