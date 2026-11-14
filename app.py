import os, re, sqlite3, time
from functools import wraps
from flask import Flask, request, jsonify, session, g, abort, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
DB = os.environ.get("DB_PATH", "queue.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS managers(id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE, pw TEXT);
CREATE TABLE IF NOT EXISTS queues(id INTEGER PRIMARY KEY, manager_id INTEGER, name TEXT, prefix TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS tokens(id INTEGER PRIMARY KEY, queue_id INTEGER, number INTEGER, code TEXT, name TEXT,
  status TEXT, position REAL, created_at REAL, started_at REAL, completed_at REAL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, queue_id INTEGER, ts REAL, length INTEGER);
"""

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()

def init_db():
    with sqlite3.connect(DB) as c:
        c.executescript(SCHEMA)

@app.errorhandler(404)
def nf(_):
    return jsonify(error="Not found"), 404

def auth(f):
    @wraps(f)
    def w(*a, **k):
        if "uid" not in session:
            return jsonify(error="Please log in"), 401
        return f(*a, **k)
    return w

def own_queue(qid):
    q = db().execute("SELECT * FROM queues WHERE id=? AND manager_id=?", (qid, session["uid"])).fetchone()
    if not q:
        abort(404)
    return q

def own_token(tid):
    t = db().execute("""SELECT t.* FROM tokens t JOIN queues q ON q.id=t.queue_id
                        WHERE t.id=? AND q.manager_id=?""", (tid, session["uid"])).fetchone()
    if not t:
        abort(404)
    return t

def log_len(qid):
    n = db().execute("SELECT COUNT(*) FROM tokens WHERE queue_id=? AND status='waiting'", (qid,)).fetchone()[0]
    db().execute("INSERT INTO events(queue_id,ts,length) VALUES(?,?,?)", (qid, time.time(), n))

def body():
    return request.get_json(silent=True) or {}

# ---------- auth ----------
@app.post("/api/register")
def register():
    b = body()
    name, email, pw = (b.get("name") or "").strip(), (b.get("email") or "").strip().lower(), b.get("password") or ""
    if not name or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) or len(pw) < 6:
        return jsonify(error="Enter a name, a valid email and a password of 6+ characters"), 400
    try:
        cur = db().execute("INSERT INTO managers(name,email,pw) VALUES(?,?,?)", (name, email, generate_password_hash(pw)))
        db().commit()
    except sqlite3.IntegrityError:
        return jsonify(error="That email is already registered"), 400
    session["uid"] = cur.lastrowid
    return jsonify(user={"name": name, "email": email})

@app.post("/api/login")
def login():
    b = body()
    m = db().execute("SELECT * FROM managers WHERE email=?", ((b.get("email") or "").strip().lower(),)).fetchone()
    if not m or not check_password_hash(m["pw"], b.get("password") or ""):
        return jsonify(error="Wrong email or password"), 401
    session["uid"] = m["id"]
    return jsonify(user={"name": m["name"], "email": m["email"]})

@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)

@app.get("/api/me")
@auth
def me():
    m = db().execute("SELECT name,email FROM managers WHERE id=?", (session["uid"],)).fetchone()
    if not m:
        session.clear()
        return jsonify(error="Please log in"), 401
    return jsonify(user=dict(m))

# ---------- queues ----------
@app.get("/api/queues")
@auth
def list_queues():
    rows = db().execute("""SELECT q.id,q.name,
        (SELECT COUNT(*) FROM tokens t WHERE t.queue_id=q.id AND t.status='waiting') AS waiting
        FROM queues q WHERE manager_id=? ORDER BY q.id""", (session["uid"],)).fetchall()
    return jsonify(queues=[dict(r) for r in rows])

@app.post("/api/queues")
@auth
def create_queue():
    name = (body().get("name") or "").strip()[:40]
    if not name:
        return jsonify(error="Give the queue a name"), 400
    prefix = name[0].upper() if name[0].isalpha() else "Q"
    cur = db().execute("INSERT INTO queues(manager_id,name,prefix,created_at) VALUES(?,?,?,?)",
                       (session["uid"], name, prefix, time.time()))
    log_len(cur.lastrowid)
    db().commit()
    return jsonify(id=cur.lastrowid, name=name)

@app.get("/api/queues/<int:qid>")
@auth
def queue_detail(qid):
    q = own_queue(qid)
    rows = db().execute("""SELECT * FROM tokens WHERE queue_id=? AND status IN ('waiting','serving')
                           ORDER BY position""", (qid,)).fetchall()
    toks = [dict(r) for r in rows]
    st = db().execute("""SELECT COUNT(*) n, AVG(started_at-created_at) w FROM tokens
                         WHERE queue_id=? AND started_at IS NOT NULL""", (qid,)).fetchone()
    return jsonify(queue={"id": q["id"], "name": q["name"]}, now=time.time(),
                   waiting=[t for t in toks if t["status"] == "waiting"],
                   serving=[t for t in toks if t["status"] == "serving"],
                   stats={"served": st["n"], "avg_wait": st["w"]})

@app.delete("/api/queues/<int:qid>")
@auth
def delete_queue(qid):
    own_queue(qid)
    for t in ("tokens", "events"):
        db().execute(f"DELETE FROM {t} WHERE queue_id=?", (qid,))
    db().execute("DELETE FROM queues WHERE id=?", (qid,))
    db().commit()
    return jsonify(ok=True)

# ---------- tokens ----------
@app.post("/api/queues/<int:qid>/tokens")
@auth
def add_token(qid):
    q = own_queue(qid)
    name = (body().get("name") or "").strip()[:60]
    if not name:
        return jsonify(error="Enter the person's name"), 400
    r = db().execute("SELECT COALESCE(MAX(number),0)+1, COALESCE(MAX(position),0)+1 FROM tokens WHERE queue_id=?", (qid,)).fetchone()
    num, pos = r[0], r[1]
    code = f"{q['prefix']}{num:03d}"
    db().execute("""INSERT INTO tokens(queue_id,number,code,name,status,position,created_at)
                    VALUES(?,?,?,?, 'waiting',?,?)""", (qid, num, code, name, pos, time.time()))
    log_len(qid)
    db().commit()
    return jsonify(code=code)

@app.post("/api/tokens/<int:tid>/move")
@auth
def move_token(tid):
    t = own_token(tid)
    d = body().get("dir")
    if t["status"] != "waiting" or d not in ("up", "down"):
        return jsonify(error="Only waiting tokens can be moved"), 400
    op, order = ("<", "DESC") if d == "up" else (">", "ASC")
    n = db().execute(f"""SELECT * FROM tokens WHERE queue_id=? AND status='waiting' AND position{op}?
                         ORDER BY position {order} LIMIT 1""", (t["queue_id"], t["position"])).fetchone()
    if n:
        db().execute("UPDATE tokens SET position=? WHERE id=?", (n["position"], t["id"]))
        db().execute("UPDATE tokens SET position=? WHERE id=?", (t["position"], n["id"]))
        db().commit()
    return jsonify(ok=True)

@app.post("/api/queues/<int:qid>/serve")
@auth
def serve(qid):
    own_queue(qid)
    t = db().execute("""SELECT * FROM tokens WHERE queue_id=? AND status='waiting'
                        ORDER BY position LIMIT 1""", (qid,)).fetchone()
    if not t:
        return jsonify(error="No one is waiting"), 400
    db().execute("UPDATE tokens SET status='serving', started_at=? WHERE id=?", (time.time(), t["id"]))
    log_len(qid)
    db().commit()
    return jsonify(code=t["code"])

@app.post("/api/tokens/<int:tid>/complete")
@auth
def complete(tid):
    t = own_token(tid)
    if t["status"] != "serving":
        return jsonify(error="Token is not being served"), 400
    db().execute("UPDATE tokens SET status='served', completed_at=? WHERE id=?", (time.time(), tid))
    db().commit()
    return jsonify(ok=True)

@app.post("/api/tokens/<int:tid>/cancel")
@auth
def cancel(tid):
    t = own_token(tid)
    if t["status"] not in ("waiting", "serving"):
        return jsonify(error="Token is already closed"), 400
    db().execute("UPDATE tokens SET status='cancelled', completed_at=? WHERE id=?", (time.time(), tid))
    log_len(t["queue_id"])
    db().commit()
    return jsonify(ok=True)

# ---------- analytics ----------
@app.get("/api/queues/<int:qid>/analytics")
@auth
def analytics(qid):
    own_queue(qid)
    c = db()
    s = c.execute("""SELECT
        SUM(status='served') served, SUM(status='cancelled') cancelled, COUNT(*) total,
        AVG(CASE WHEN started_at IS NOT NULL THEN started_at-created_at END) avg_wait,
        MAX(CASE WHEN started_at IS NOT NULL THEN started_at-created_at END) max_wait,
        AVG(CASE WHEN status='served' THEN completed_at-started_at END) avg_service
        FROM tokens WHERE queue_id=?""", (qid,)).fetchone()
    ev = c.execute("SELECT ts,length FROM (SELECT * FROM events WHERE queue_id=? ORDER BY ts DESC LIMIT 80) ORDER BY ts",
                   (qid,)).fetchall()
    cur = c.execute("SELECT COUNT(*) FROM tokens WHERE queue_id=? AND status='waiting'", (qid,)).fetchone()[0]
    trend = [dict(e) for e in ev] + [{"ts": time.time(), "length": cur}]
    waits = c.execute("""SELECT code, started_at-created_at AS wait FROM tokens WHERE queue_id=? AND started_at IS NOT NULL
                         ORDER BY started_at DESC LIMIT 12""", (qid,)).fetchall()
    return jsonify(served=s["served"] or 0, cancelled=s["cancelled"] or 0, total=s["total"] or 0,
                   avg_wait=s["avg_wait"], max_wait=s["max_wait"], avg_service=s["avg_service"],
                   peak=max(x["length"] for x in trend), current=cur,
                   trend=trend, waits=[dict(w) for w in reversed(waits)])

@app.get("/")
def index():
    return send_from_directory("static", "index.html")

init_db()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
