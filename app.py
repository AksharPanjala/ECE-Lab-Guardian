import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlparse

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
import qrcode
from io import BytesIO
import base64

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-before-deploying-" + secrets.token_hex(8))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
ASSISTANT_PASSWORD = os.environ.get("ASSISTANT_PASSWORD", "change-me-before-deploying")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# This starter supports SQLite for local testing. For real public deployment,
# use a managed PostgreSQL database and set DATABASE_URL.
def connect_db():
    if DATABASE_URL.startswith(("postgres://", "postgresql://")):
        try:
            import psycopg
            conn = psycopg.connect(DATABASE_URL, row_factory=psycopg.rows.dict_row)
            return conn, "postgres"
        except Exception as exc:
            raise RuntimeError("Could not connect to PostgreSQL. Check DATABASE_URL and psycopg installation.") from exc
    path = os.environ.get("SQLITE_PATH", os.path.join(os.path.dirname(__file__), "lab_guardian.db"))
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn, "sqlite"

def execute(conn, dbtype, sql, params=()):
    if dbtype == "postgres":
        sql = sql.replace("?", "%s")
        return conn.execute(sql, params)
    return conn.execute(sql, params)

def init_db():
    conn, dbtype = connect_db()
    if dbtype == "postgres":
        conn.execute("""
        CREATE TABLE IF NOT EXISTS equipment (
            id TEXT PRIMARY KEY, category TEXT NOT NULL, label TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'available', borrower TEXT, due_date TEXT, note TEXT
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY, student_name TEXT NOT NULL, items_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
            allocated_json TEXT, assistant_note TEXT
        )""")
    else:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS equipment (
            id TEXT PRIMARY KEY, category TEXT NOT NULL, label TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'available', borrower TEXT, due_date TEXT, note TEXT
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY, student_name TEXT NOT NULL, items_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
            allocated_json TEXT, assistant_note TEXT
        )""")
    count = execute(conn, dbtype, "SELECT COUNT(*) AS n FROM equipment").fetchone()
    if (count["n"] if isinstance(count, dict) else count["n"]) == 0:
        seed = [
            ("MM01","Multimeter","Multimeter MM01","borrowed","Demo Student",(datetime.now()+timedelta(days=1)).strftime("%Y-%m-%d"),"Demo record"),
            ("MM02","Multimeter","Multimeter MM02","available",None,None,""),
            ("MM03","Multimeter","Multimeter MM03","available",None,None,""),
            ("MM04","Multimeter","Multimeter MM04","faulty",None,None,"Needs inspection"),
            ("ESP01","ESP32 Module","ESP32 ESP01","overdue","Demo Student",(datetime.now()-timedelta(days=2)).strftime("%Y-%m-%d"),"Demo record"),
            ("ESP02","ESP32 Module","ESP32 ESP02","available",None,None,""),
            ("ARD01","Arduino Board","Arduino ARD01","available",None,None,""),
            ("ARD02","Arduino Board","Arduino ARD02","available",None,None,""),
            ("USB01","USB Cable","USB USB01","available",None,None,""),
            ("USB02","USB Cable","USB USB02","available",None,None,""),
            ("SEN01","Sensor Kit","Sensor SEN01","available",None,None,""),
            ("SEN02","Sensor Kit","Sensor SEN02","available",None,None,""),
        ]
        for row in seed:
            execute(conn, dbtype, "INSERT INTO equipment (id,category,label,status,borrower,due_date,note) VALUES (?,?,?,?,?,?,?)", row)
    conn.commit()
    conn.close()

def query_all(sql, params=()):
    conn, dbtype = connect_db()
    rows = execute(conn, dbtype, sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def query_one(sql, params=()):
    rows = query_all(sql, params)
    return rows[0] if rows else None

def db_execute(sql, params=()):
    conn, dbtype = connect_db()
    cur = execute(conn, dbtype, sql, params)
    conn.commit()
    conn.close()
    return cur

def assistant_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("assistant_logged_in"):
            return redirect(url_for("assistant_login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper

def qr_data(url):
    img = qrcode.make(url)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

@app.route("/")
def home():
    return render_template("home.html")

@app.route("/request", methods=["GET", "POST"])
def new_request():
    categories = sorted({e["category"] for e in query_all("SELECT category FROM equipment")})
    if request.method == "POST":
        student_name = request.form.get("student_name", "").strip()[:80]
        if not student_name:
            flash("Please enter your name.", "error")
            return redirect(url_for("new_request"))
        requested = {}
        for category in categories:
            try:
                qty = int(request.form.get("qty_" + category, "0"))
            except ValueError:
                qty = 0
            if qty < 0: qty = 0
            requested[category] = min(qty, 20)
        requested = {k:v for k,v in requested.items() if v > 0}
        if not requested:
            flash("Choose at least one item quantity.", "error")
            return redirect(url_for("new_request"))
        import json
        rid = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:10].upper()
        db_execute("INSERT INTO requests (id,student_name,items_json,status,created_at) VALUES (?,?,?,?,?)",
                   (rid, student_name, json.dumps(requested), "pending", datetime.now().isoformat(timespec="minutes")))
        target = url_for("request_view", rid=rid, _external=True)
        if PUBLIC_BASE_URL:
            target = PUBLIC_BASE_URL + url_for("request_view", rid=rid)
        return render_template("request_created.html", rid=rid, student_name=student_name, requested=requested, qr=qr_data(target), target=target)
    return render_template("request_form.html", categories=categories)

@app.route("/request/<rid>")
def request_view(rid):
    import json
    r = query_one("SELECT * FROM requests WHERE id=?", (rid,))
    if not r: abort(404)
    r["items"] = json.loads(r["items_json"])
    r["allocated"] = json.loads(r["allocated_json"]) if r.get("allocated_json") else {}
    return render_template("request_view.html", r=r)

@app.route("/assistant/login", methods=["GET", "POST"])
def assistant_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if secrets.compare_digest(password, ASSISTANT_PASSWORD):
            session["assistant_logged_in"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Password incorrect.", "error")
    return render_template("login.html")

@app.route("/assistant/logout")
def assistant_logout():
    session.clear()
    return redirect(url_for("home"))

@app.route("/assistant")
@assistant_required
def dashboard():
    import json
    equipment = query_all("SELECT * FROM equipment ORDER BY category, id")
    requests_rows = query_all("SELECT * FROM requests ORDER BY created_at DESC")
    for r in requests_rows:
        r["items"] = json.loads(r["items_json"])
        r["allocated"] = json.loads(r["allocated_json"]) if r.get("allocated_json") else {}
    counts = {}
    for e in equipment:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    alerts = counts.get("overdue", 0) + counts.get("faulty", 0)
    led = "RED" if alerts else ("YELLOW" if counts.get("borrowed", 0) else "GREEN")
    return render_template("dashboard.html", equipment=equipment, requests_rows=requests_rows, counts=counts, led=led)

@app.route("/assistant/allocate/<rid>", methods=["GET", "POST"])
@assistant_required
def allocate(rid):
    import json
    r = query_one("SELECT * FROM requests WHERE id=?", (rid,))
    if not r: abort(404)
    requested = json.loads(r["items_json"])
    if request.method == "POST":
        student = r["student_name"]
        allocated = {}
        conn, dbtype = connect_db()
        try:
            # Recheck stock inside the allocation operation. Only available items can be assigned.
            for category, qty in requested.items():
                available = execute(conn, dbtype,
                    "SELECT id FROM equipment WHERE category=? AND status='available' ORDER BY id",
                    (category,)).fetchall()
                ids = [x["id"] for x in available]
                chosen = request.form.getlist("unit_" + category)
                allowed = set(ids)
                chosen = [x for x in chosen if x in allowed][:int(qty)]
                for unit_id in chosen:
                    execute(conn, dbtype,
                        "UPDATE equipment SET status='borrowed', borrower=?, due_date=?, note='' WHERE id=? AND status='available'",
                        (student, (datetime.now()+timedelta(days=7)).strftime("%Y-%m-%d"), unit_id))
                allocated[category] = chosen
            execute(conn, dbtype, "UPDATE requests SET status='allocated', allocated_json=? WHERE id=?",
                    (json.dumps(allocated), rid))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        flash("Allocation saved. Only selected available units were allocated.", "success")
        return redirect(url_for("dashboard"))
    options = {}
    for category, qty in requested.items():
        options[category] = query_all("SELECT id,label FROM equipment WHERE category=? AND status='available' ORDER BY id", (category,))
    return render_template("allocate.html", r=r, requested=requested, options=options)

@app.route("/assistant/return/<unit_id>", methods=["POST"])
@assistant_required
def return_unit(unit_id):
    e = query_one("SELECT * FROM equipment WHERE id=?", (unit_id,))
    if e and e["status"] in ("borrowed", "overdue"):
        db_execute("UPDATE equipment SET status='available', borrower=NULL, due_date=NULL WHERE id=?", (unit_id,))
        flash(f"{unit_id} marked as returned.", "success")
    return redirect(url_for("dashboard"))

@app.route("/assistant/fault/<unit_id>", methods=["POST"])
@assistant_required
def fault_unit(unit_id):
    e = query_one("SELECT * FROM equipment WHERE id=?", (unit_id,))
    if e:
        db_execute("UPDATE equipment SET status='faulty', note='Marked faulty by assistant' WHERE id=?", (unit_id,))
        flash(f"{unit_id} marked faulty.", "success")
    return redirect(url_for("dashboard"))

@app.route("/assistant/repair/<unit_id>", methods=["POST"])
@assistant_required
def repair_unit(unit_id):
    e = query_one("SELECT * FROM equipment WHERE id=?", (unit_id,))
    if e and e["status"] == "faulty":
        db_execute("UPDATE equipment SET status='available', note='' WHERE id=?", (unit_id,))
        flash(f"{unit_id} marked available after repair.", "success")
    return redirect(url_for("dashboard"))

@app.route("/health")
def health():
    return {"ok": True}

with app.app_context():
    init_db()

if __name__ == "__main__":
    # Local testing only. Render supplies PORT in production.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
