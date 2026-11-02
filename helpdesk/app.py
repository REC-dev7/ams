"""REC Helpdesk: report and fix problems with campus electronics.

Run:        python app.py           -> http://localhost:5002
Demo data:  python app.py --demo
Separate system from the AMS (own database). Optional link: if the AMS database is found,
tickets can point at an AMS asset tag and show its name and location (read-only).
"""
import csv
import io
import os
import re
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, send_from_directory, session, url_for
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "helpdesk.db")
KEY_FILE = os.path.join(HERE, "secret.key")
# ponytail: AMS link is a read-only peek at its sqlite file; swap for an HTTP API if they move to different servers
AMS_DB = os.environ.get("AMS_DB", os.path.join(HERE, "..", "ams.db"))

ROLES = ["admin", "technician", "staff", "coordinator", "hod", "auditor"]
ROLE_LABEL = {"admin": "Admin", "technician": "IT technician", "staff": "Staff / faculty", "coordinator": "Dept coordinator",
              "hod": "HOD", "auditor": "Auditor (read only)"}
MANAGE = ("admin", "technician")  # roles that work tickets: assign, resolve, internal notes
# Who fixes what. A category decides the team; staff can still re-route a ticket posted in the wrong place.
TEAMS = ["IT Network", "IT Systems", "Printers & AV", "Electrical (EB)", "AC", "Civil", "Other"]
CATEGORY_TEAM = {"Wi-Fi / Internet": "IT Network", "LAN / cabling": "IT Network", "Computer / hardware": "IT Systems",
                 "Software / install": "IT Systems", "Printer / scanner": "Printers & AV", "Smart board / projector / AV": "Printers & AV",
                 "Power / UPS": "Electrical (EB)", "Air conditioning": "AC", "Civil / furniture": "Civil", "Other": "Other"}
CATEGORIES = list(CATEGORY_TEAM)
OLD_CATEGORIES = {"Hardware": "Computer / hardware", "Network": "Wi-Fi / Internet", "Software": "Software / install", "Power": "Power / UPS"}
REQUEST_TYPES = ["Fault", "Service request", "New requirement", "Question"]
# why a ticket can't wait: bumps the priority so it is not buried
URGENT_REASONS = {"Exam in progress": "Critical", "Event running": "High", "Class in session": "High", "Management request": "High"}
PRIORITIES = ["Low", "Medium", "High", "Critical"]
STATUSES = ["Open", "In progress", "Waiting on requester", "Resolved", "Closed", "Planned"]
LIVE = ("Open", "In progress", "Waiting on requester")  # not finished
# fix-by time from the moment a ticket is raised; the clock pauses while we wait for the requester
SLA_HOURS = {"Critical": 4, "High": 24, "Medium": 72, "Low": 168}
STALE_HOURS = 24  # an open ticket with no update for this long is flagged
DEPARTMENTS = ["CSE", "CSE (AIML)", "CSE (Cyber Security)", "IT", "ECE", "EEE", "Mechanical", "Mechatronics", "Aeronautical", "Automobile",
               "Civil", "Chemical", "Biotechnology", "Biomedical", "Food Technology", "Architecture", "Management Studies", "Mathematics",
               "Science & Humanities", "Exam Cell", "Administration", "Library", "Other"]
FMT = "%Y-%m-%d %H:%M:%S"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  password_hash TEXT NOT NULL, role TEXT NOT NULL, department TEXT, team TEXT);
CREATE TABLE IF NOT EXISTS departments (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS locations (
  id INTEGER PRIMARY KEY, block TEXT NOT NULL, floor TEXT NOT NULL, room TEXT NOT NULL, UNIQUE (block, floor, room));
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT, category TEXT, priority TEXT,
  status TEXT NOT NULL DEFAULT 'Open', location TEXT, asset_tag TEXT,
  raised_by INTEGER REFERENCES users(id), assigned_to INTEGER REFERENCES users(id),
  created_at TEXT, due_at TEXT, resolved_at TEXT, resolution TEXT,
  request_type TEXT DEFAULT 'Fault', team TEXT, department TEXT, location_id INTEGER REFERENCES locations(id),
  urgent_reason TEXT, first_response_at TEXT, updated_at TEXT, parent_id INTEGER REFERENCES tickets(id),
  parts_used TEXT, source TEXT DEFAULT 'Web', requester_name TEXT);
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  user_id INTEGER REFERENCES users(id), body TEXT NOT NULL, system INTEGER DEFAULT 0, at TEXT, internal INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS status_log (
  id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  user_id INTEGER REFERENCES users(id), from_status TEXT, to_status TEXT NOT NULL, at TEXT);
CREATE TABLE IF NOT EXISTS attachments (
  id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  file TEXT NOT NULL, user_id INTEGER REFERENCES users(id), at TEXT);
"""

# columns added after the first release: applied to existing databases at startup
MIGRATIONS = {
    "users": [("department", "TEXT"), ("team", "TEXT")],
    "tickets": [("request_type", "TEXT DEFAULT 'Fault'"), ("team", "TEXT"), ("department", "TEXT"),
                ("location_id", "INTEGER REFERENCES locations(id)"), ("urgent_reason", "TEXT"), ("first_response_at", "TEXT"),
                ("updated_at", "TEXT"), ("parent_id", "INTEGER REFERENCES tickets(id)"), ("parts_used", "TEXT"),
                ("source", "TEXT DEFAULT 'Web'"), ("requester_name", "TEXT")],
    "comments": [("internal", "INTEGER DEFAULT 0")],
}

app = Flask(__name__)
if not os.path.exists(KEY_FILE):
    with open(KEY_FILE, "w") as fh:
        fh.write(secrets.token_hex(32))
with open(KEY_FILE) as fh:
    app.secret_key = fh.read().strip()
# own cookie name: browsers share cookies across ports on localhost, so apps on :5000/:5002 would overwrite each other
app.config.update(SESSION_COOKIE_NAME="helpdesk_session", SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")), MAX_CONTENT_LENGTH=12 * 1024 * 1024, SEND_FILE_MAX_AGE_DEFAULT=86400)


def now():
    return datetime.now().strftime(FMT)


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_):
    if (conn := g.pop("db", None)) is not None:
        conn.close()


def q(sql, args=(), one=False):
    rows = db().execute(sql, args).fetchall()
    return (rows[0] if rows else None) if one else rows


def init_db():
    with sqlite3.connect(DB) as c:
        c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        if "CHECK" in c.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0]:
            # first release allowed three roles only; SQLite can't drop a CHECK, so rebuild the table
            c.executescript("""PRAGMA foreign_keys=OFF;
                CREATE TABLE users_new (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                  password_hash TEXT NOT NULL, role TEXT NOT NULL, department TEXT, team TEXT);
                INSERT INTO users_new SELECT id, username, name, password_hash, role, department, team FROM users;
                DROP TABLE users; ALTER TABLE users_new RENAME TO users; PRAGMA foreign_keys=ON;""")
        for old, new in OLD_CATEGORIES.items():
            c.execute("UPDATE tickets SET category=? WHERE category=?", (new, old))
        for cat, team in CATEGORY_TEAM.items():
            c.execute("UPDATE tickets SET team=? WHERE team IS NULL AND category=?", (team, cat))
        c.execute("UPDATE tickets SET team='Other' WHERE team IS NULL")
        c.execute("UPDATE tickets SET updated_at=COALESCE(resolved_at, created_at) WHERE updated_at IS NULL")
        c.execute("UPDATE tickets SET request_type='Fault' WHERE request_type IS NULL")
        c.executemany("INSERT OR IGNORE INTO departments(name) VALUES (?)", [(d,) for d in DEPARTMENTS])


def opt_int(v):
    return int(v) if v and str(v).isdigit() else None


# ---------- photos + pagination ----------

UPLOADS = os.path.join(HERE, "uploads")
MAX_PHOTO, MAX_PHOTOS, PER_PAGE = 4 * 1024 * 1024, 5, 50
# real file type comes from the first bytes, never from the uploaded name or content-type (SVG/HTML are refused)
IMAGE_MAGIC = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png", b"GIF87a": ".gif", b"GIF89a": ".gif"}


def save_photos(field="photos"):
    """Store up to 5 images from the form under random names. Returns (saved filenames, number refused)."""
    saved, refused = [], 0
    for f in request.files.getlist(field)[:MAX_PHOTOS]:
        if not f.filename:
            continue
        data = f.read(MAX_PHOTO + 1)
        ext = next((e for m, e in IMAGE_MAGIC.items() if data.startswith(m)), None)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = ".webp"
        if not ext or len(data) > MAX_PHOTO:
            refused += 1
            continue
        os.makedirs(UPLOADS, exist_ok=True)
        name = secrets.token_hex(16) + ext
        with open(os.path.join(UPLOADS, name), "wb") as fh:
            fh.write(data)
        saved.append(name)
    if refused:
        flash(f"{refused} file(s) skipped: use JPG, PNG, GIF or WebP up to 4 MB.", "error")
    return saved, refused


@app.route("/photos/<name>")
def photo(name):  # behind the login guard like every page
    if not re.fullmatch(r"[0-9a-f]{32}\.(jpg|png|gif|webp)", name):
        abort(404)
    if row := q("SELECT ticket_id FROM attachments WHERE file=?", (name,), one=True):
        get_ticket(row["ticket_id"])  # 404 unless this user may see that ticket
    return send_from_directory(UPLOADS, name)


def paginate(sql, args=()):
    """Run a SELECT one page at a time (?page=N). Returns (rows, {page, pages, total})."""
    page = max(opt_int(request.args.get("page")) or 1, 1)
    total = q(f"SELECT COUNT(*) n FROM ({sql})", args, one=True)["n"]
    pages = max(-(-total // PER_PAGE), 1)
    page = min(page, pages)
    return q(sql + " LIMIT ? OFFSET ?", [*args, PER_PAGE, (page - 1) * PER_PAGE]), {"page": page, "pages": pages, "total": total}

def attach(tid):
    names, _ = save_photos()
    for n in names:
        db().execute("INSERT INTO attachments(ticket_id, file, user_id, at) VALUES (?,?,?,?)", (tid, n, g.user["id"], now()))
    return len(names)



# ---------- AMS link (read-only, optional) ----------

def ams(sql, args=()):
    """Query the AMS database read-only. Returns [] if the AMS isn't there."""
    path = os.path.abspath(AMS_DB)
    if not os.path.exists(path):
        return []
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        try:
            return c.execute(sql, args).fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        return []


ASSET_SQL = """SELECT a.id, a.tag, a.name, a.status, r.name room, b.name building FROM assets a
               LEFT JOIN rooms r ON r.id=a.room_id LEFT JOIN buildings b ON b.id=r.building_id"""


def asset_for(tag):
    rows = ams(ASSET_SQL + " WHERE a.tag=?", (tag,)) if tag else []
    return rows[0] if rows else None


def asset_choices():
    return ams(ASSET_SQL + " WHERE a.parent_id IS NULL AND a.status!='Scrapped' ORDER BY b.name, r.name, a.tag LIMIT 3000")


# ---------- auth ----------

PUBLIC = {"login", "setup", "static"}


@app.before_request
def guard():
    if request.endpoint is None:
        return
    token = session.get("csrf")
    if request.method == "POST" and (not token or not secrets.compare_digest(request.form.get("csrf", ""), token)):
        abort(400, "Form expired, go back and try again.")
    if request.endpoint != "static" and not q("SELECT 1 FROM users LIMIT 1", one=True):
        return None if request.endpoint == "setup" else redirect(url_for("setup"))
    if request.endpoint in PUBLIC:
        return
    g.user = q("SELECT * FROM users WHERE id=?", (session.get("uid"),), one=True)
    if not g.user:
        return redirect(url_for("login", next=request.full_path))
    if request.method == "POST" and g.user["role"] == "auditor" and request.endpoint not in ("logout", "account"):
        abort(403)


def role(*allowed):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if g.user["role"] not in allowed:
                abort(403)
            return f(*a, **kw)
        return wrapper
    return deco


def overdue(t):
    return t["status"] in ("Open", "In progress") and t["due_at"] and t["due_at"] < now()


def due_text(t):
    """'overdue 3h' / 'due in 2d' for a live ticket; the clock stops while we wait for the requester."""
    if t["status"] == "Waiting on requester":
        return "clock paused"
    if t["status"] not in ("Open", "In progress") or not t["due_at"]:
        return ""
    secs = (datetime.strptime(t["due_at"], FMT) - datetime.now()).total_seconds()
    mins = int(abs(secs) // 60)
    span = f"{mins // 1440}d" if mins >= 2880 else f"{mins // 60}h" if mins >= 120 else f"{mins}m"
    return f"overdue {span}" if secs < 0 else f"due in {span}"


def is_stale(t):
    return t["status"] in ("Open", "In progress") and bool(t["updated_at"]) and (
        datetime.now() - datetime.strptime(t["updated_at"], FMT)).total_seconds() > STALE_HOURS * 3600


def ago(ts):
    if not ts:
        return ""
    mins = int((datetime.now() - datetime.strptime(ts, FMT)).total_seconds() // 60)
    return "just now" if mins < 2 else f"{mins}m ago" if mins < 120 else f"{mins // 60}h ago" if mins < 2880 else f"{mins // 1440}d ago"


app.jinja_env.globals.update(overdue=overdue, due_text=due_text, is_stale=is_stale, ago=ago)
app.jinja_env.globals["static_v"] = int(os.path.getmtime(os.path.join(HERE, "static", "style.css")))  # cache-bust: CSS/JS cached for a day


@app.context_processor
def template_globals():
    token = session.setdefault("csrf", secrets.token_hex(16))
    user = g.get("user")
    return {
        "other_url": os.environ.get("OTHER_APP_URL"),
        "user": user, "csrf": Markup(f'<input type="hidden" name="csrf" value="{token}">'),
        "can": lambda *roles: bool(user) and user["role"] in roles,
        "CATEGORIES": CATEGORIES, "PRIORITIES": PRIORITIES, "STATUSES": STATUSES, "ROLES": ROLES, "ROLE_LABEL": ROLE_LABEL,
        "MANAGE": MANAGE, "TEAMS": TEAMS, "REQUEST_TYPES": REQUEST_TYPES, "STALE_HOURS": STALE_HOURS,
        "ams_linked": os.path.exists(os.path.abspath(AMS_DB)),
    }


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-Robots-Tag", "noindex")
    return resp


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if q("SELECT 1 FROM users LIMIT 1", one=True):
        return redirect(url_for("login"))
    if request.method == "POST":
        f = request.form
        if len(f.get("password", "")) < 8:
            flash("Password must be at least 8 characters.", "error")
        elif not f.get("username", "").strip() or not f.get("name", "").strip():
            flash("Name and username are required.", "error")
        else:
            db().execute("INSERT INTO users(username, name, password_hash, role) VALUES (?,?,?, 'admin')",
                         (f["username"].strip().lower(), f["name"].strip(), generate_password_hash(f["password"])))
            db().commit()
            flash("Admin account created. Sign in.", "ok")
            return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = q("SELECT * FROM users WHERE username=?", (request.form.get("username", "").strip().lower(),), one=True)
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session.clear()
            session["uid"] = u["id"]
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        flash("Wrong username or password.", "error")
    return render_template("login.html", demo=[(u, n, ROLE_LABEL[r]) for u, n, r, _, _ in DEMO_USERS] if app.config.get("DEMO") else [])


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
def account():
    if request.method == "POST":
        f = request.form
        if not check_password_hash(g.user["password_hash"], f.get("current", "")):
            flash("Current password is wrong.", "error")
        elif len(f.get("new", "")) < 8:
            flash("New password must be at least 8 characters.", "error")
        else:
            db().execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(f["new"]), g.user["id"]))
            db().commit()
            flash("Password changed.", "ok")
            return redirect(url_for("account"))
    return render_template("account.html")


# ---------- tickets ----------

# repeat_n: how many tickets of this kind came from this place in the last 30 days (this one included)
TICKET_SQL = """SELECT t.*, u.name raiser, a.name assignee,
  (SELECT COUNT(*) FROM tickets x WHERE x.location IS NOT NULL AND x.location=t.location AND x.category=t.category
     AND x.id<=t.id AND x.created_at >= datetime(t.created_at, '-30 days')) repeat_n,
  (SELECT COUNT(*) FROM tickets k WHERE k.parent_id=t.id) child_n
  FROM tickets t LEFT JOIN users u ON u.id=t.raised_by LEFT JOIN users a ON a.id=t.assigned_to"""
PRIO_ORDER = "CASE t.priority WHEN 'Critical' THEN 0 WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END"
DUE_ORDER = "t.due_at IS NULL, t.due_at"
OPEN = "t.status IN ('Open','In progress')"  # SLA clock running
LIVE_SQL = "t.status IN ('Open','In progress','Waiting on requester')"


def loc_label(r):
    return f"{r['block']} · {r['floor']} · {r['room']}"


def scope():
    """SQL limiting which tickets this user may see. Enforced here, not by hiding buttons."""
    u = g.user
    if u["role"] in ("admin", "technician", "auditor"):
        return "", []
    if u["role"] in ("hod", "coordinator") and u["department"]:
        return " AND (t.department=? OR t.raised_by=?)", [u["department"], u["id"]]
    return " AND t.raised_by=?", [u["id"]]


def get_ticket(tid):
    where, args = scope()
    t = q(TICKET_SQL + " WHERE t.id=?" + where, [tid, *args], one=True)
    if not t:
        abort(404)  # 404, not 403: don't reveal that the ticket exists
    return t


def can_confirm(t):
    """Who may confirm a fix or reopen: the requester, their department's HOD/coordinator, and the IT team."""
    u = g.user
    return u["role"] in MANAGE or t["raised_by"] == u["id"] or (
        u["role"] in ("hod", "coordinator") and u["department"] and u["department"] == t["department"])


def note(tid, text, system=True, internal=False, response=True):
    stamp = now()
    db().execute("INSERT INTO comments(ticket_id, user_id, body, system, at, internal) VALUES (?,?,?,?,?,?)",
                 (tid, g.user["id"], text, int(system), stamp, int(internal)))
    answered = response and not internal and g.user["role"] in MANAGE  # first reply from IT = first response
    db().execute("""UPDATE tickets SET updated_at=?,
                    first_response_at=CASE WHEN ? AND first_response_at IS NULL THEN ? ELSE first_response_at END WHERE id=?""",
                 (stamp, int(answered), stamp, tid))


def set_status(t, new, text=None, **cols):
    """Move a ticket to a new status: update it, add to its history and leave a note. cols are trusted column names."""
    sets = "".join(f", {k}=?" for k in cols)
    stamp = now()
    db().execute(f"UPDATE tickets SET status=?, updated_at=?{sets} WHERE id=?", [new, stamp, *cols.values(), t["id"]])
    db().execute("INSERT INTO status_log(ticket_id, user_id, from_status, to_status, at) VALUES (?,?,?,?,?)",
                 (t["id"], g.user["id"], t["status"], new, stamp))
    note(t["id"], text or f"{t['status']} → {new}")


def resume_clock(t):
    """Waiting for the requester pauses the fix-by clock; push the deadline out by the time spent waiting."""
    if t["status"] != "Waiting on requester" or not t["due_at"]:
        return {}
    since = q("SELECT at FROM status_log WHERE ticket_id=? AND to_status='Waiting on requester' ORDER BY id DESC LIMIT 1",
              (t["id"],), one=True)
    if not since:
        return {}
    waited = datetime.now() - datetime.strptime(since["at"], FMT)
    return {"due_at": (datetime.strptime(t["due_at"], FMT) + waited).strftime(FMT)}


def location_from_form(f):
    row = q("SELECT * FROM locations WHERE id=?", (opt_int(f.get("location_id")),), one=True)
    if row:
        return row["id"], loc_label(row)
    return None, (f.get("location") or "").strip() or None


def new_ticket(f, logged=False):
    """Create a ticket from a form. logged=True: an IT technician records work already done (created as Closed).
    Returns (ticket id, tickets of this kind from this place in 30 days) or None after flashing the problem."""
    u = g.user
    title = f.get("title", "").strip()
    if not title:
        flash("Describe the problem in the title.", "error")
        return None
    fix = f.get("resolution", "").strip()
    if logged and not fix:
        flash("Write what was done.", "error")
        return None
    rtype = f.get("request_type") if f.get("request_type") in REQUEST_TYPES else "Fault"
    cat = f.get("category") if f.get("category") in CATEGORIES else "Other"
    prio = f.get("priority") if f.get("priority") in PRIORITIES else "Medium"
    reason = f.get("urgent_reason") if f.get("urgent_reason") in URGENT_REASONS else None
    if reason and PRIORITIES.index(URGENT_REASONS[reason]) > PRIORITIES.index(prio):
        prio = URGENT_REASONS[reason]
    tag = (f.get("asset_tag") or "").strip() or None
    loc_id, loc = location_from_form(f)
    if tag and not loc and (found := asset_for(tag)):
        loc = f"{found['building']} · {found['room']}"
    if not loc and rtype != "Question":
        flash("Say where it is: pick the block, floor and room, or type the place.", "error")
        return None
    depts = {r["name"] for r in q("SELECT name FROM departments")}
    dept = f.get("department") if f.get("department") in depts and u["role"] in ("admin", "technician", "coordinator", "hod") else None
    dept = dept or u["department"]
    created = datetime.now()
    stamp = created.strftime(FMT)
    due = None if (logged or rtype == "New requirement") else (created + timedelta(hours=SLA_HOURS[prio])).strftime(FMT)
    cur = db().execute(
        """INSERT INTO tickets(title, description, category, priority, status, location, location_id, asset_tag, raised_by, assigned_to,
             created_at, due_at, resolved_at, resolution, parts_used, request_type, team, department, urgent_reason, first_response_at,
             updated_at, source, requester_name) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (title, f.get("description", "").strip(), cat, "Low" if logged else prio, "Closed" if logged else "Open", loc, loc_id, tag, u["id"],
         u["id"] if logged else None, stamp, due, stamp if logged else None, fix or None, f.get("parts_used", "").strip() or None,
         rtype, CATEGORY_TEAM[cat], dept, reason, stamp if logged else None, stamp,
         "Logged by technician" if logged else "Web", f.get("requester_name", "").strip() or None))
    tid = cur.lastrowid
    db().execute("INSERT INTO status_log(ticket_id, user_id, from_status, to_status, at) VALUES (?,?,NULL,?,?)",
                 (tid, u["id"], "Closed" if logged else "Open", stamp))
    note(tid, f"Work logged: {fix}" if logged else "Ticket raised", response=False)
    if reason:
        note(tid, f"Urgent: {reason}. Priority set to {prio}.", response=False)
    attach(tid)
    n = q("SELECT COUNT(*) n FROM tickets WHERE location=? AND category=? AND created_at >= datetime(?, '-30 days')",
          (loc, cat, stamp), one=True)["n"] if loc else 1
    db().commit()
    return tid, n


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


@app.route("/")
def dashboard():
    where, sargs = scope()
    cut = (datetime.now() - timedelta(hours=STALE_HOURS)).strftime(FMT)

    def count(cond, *extra):
        return q(f"SELECT COUNT(*) n FROM tickets t WHERE {cond}{where}", [*extra, *sargs], one=True)["n"]

    stats = {
        "open": count(LIVE_SQL),
        "unassigned": count(f"{LIVE_SQL} AND t.assigned_to IS NULL"),
        "overdue": count(f"{OPEN} AND t.due_at < ?", now()),
        "stale": count(f"{OPEN} AND t.updated_at < ?", cut),
        "urgent": count(f"{LIVE_SQL} AND t.priority IN ('Critical','High')"),
        "resolved_week": count("t.status IN ('Resolved','Closed') AND t.resolved_at >= ?", (datetime.now() - timedelta(days=7)).strftime(FMT)),
        "planned": count("t.status='Planned'"),
    }
    work = q(TICKET_SQL + f" WHERE {LIVE_SQL}{where} ORDER BY {PRIO_ORDER}, {DUE_ORDER} LIMIT 8", sargs)
    if g.user["role"] in MANAGE:
        mine = q(TICKET_SQL + f" WHERE {LIVE_SQL} AND t.assigned_to=? ORDER BY {PRIO_ORDER}, {DUE_ORDER}", (g.user["id"],))
        team_q = q(TICKET_SQL + f" WHERE {LIVE_SQL} AND t.assigned_to IS NULL AND t.team=? ORDER BY {PRIO_ORDER}, {DUE_ORDER}",
                   (g.user["team"],)) if g.user["team"] else []
    else:
        mine = q(TICKET_SQL + f" WHERE {LIVE_SQL} AND t.raised_by=? ORDER BY {PRIO_ORDER}, {DUE_ORDER}", (g.user["id"],))
        team_q = []
    needs_you = q(TICKET_SQL + " WHERE t.raised_by=? AND t.status IN ('Waiting on requester','Resolved') ORDER BY t.updated_at", (g.user["id"],))
    stale = q(TICKET_SQL + f" WHERE {OPEN} AND t.updated_at < ?{where} ORDER BY t.updated_at LIMIT 6", [cut, *sargs])
    by_cat = q(f"SELECT category k, COUNT(*) n FROM tickets t WHERE {LIVE_SQL}{where} GROUP BY category ORDER BY n DESC", sargs)
    top = max([r["n"] for r in by_cat] or [1])
    insights = None
    if g.user["role"] in (*MANAGE, "auditor"):
        since = (datetime.now() - timedelta(days=30)).strftime(FMT)
        rows = q("SELECT created_at, first_response_at, resolved_at, due_at FROM tickets WHERE created_at >= ?", (since,))

        def hours(a, b):
            return (datetime.strptime(b, FMT) - datetime.strptime(a, FMT)).total_seconds() / 3600

        resp = [hours(r["created_at"], r["first_response_at"]) for r in rows if r["first_response_at"]]
        fix = [hours(r["created_at"], r["resolved_at"]) for r in rows if r["resolved_at"]]
        sla = [r["resolved_at"] <= r["due_at"] for r in rows if r["resolved_at"] and r["due_at"]]
        insights = {
            "raised": len(rows), "fixed": len(fix),
            "response": round(sum(resp) / len(resp), 1) if resp else None, "fix": round(sum(fix) / len(fix), 1) if fix else None,
            "sla": round(100 * sum(sla) / len(sla)) if sla else None,
            "places": q("""SELECT location, category, COUNT(*) n FROM tickets WHERE location IS NOT NULL AND created_at >= ?
                           GROUP BY location, category HAVING n >= 2 ORDER BY n DESC, location LIMIT 5""", (since,)),
            "teams": q(f"SELECT team k, COUNT(*) n FROM tickets t WHERE {LIVE_SQL} GROUP BY team ORDER BY n DESC"),
            "outages": q(TICKET_SQL + f" WHERE {LIVE_SQL} AND (SELECT COUNT(*) FROM tickets k WHERE k.parent_id=t.id) >= 1 ORDER BY t.id DESC LIMIT 5"),
        }
    return render_template("dashboard.html", stats=stats, work=work, mine=mine, team_q=team_q, needs_you=needs_you, stale=stale,
                           insights=insights, by_cat=[{"label": r["k"], "n": r["n"], "pct": round(r["n"] * 100 / top)} for r in by_cat])


def ticket_filters():
    a = request.args
    where, args = scope()
    sql = " WHERE 1=1" + where
    scope_arg = a.get("scope", "open")
    if scope_arg == "open":
        sql += f" AND {LIVE_SQL}"
    elif scope_arg == "closed":
        sql += " AND t.status IN ('Resolved','Closed')"
    elif scope_arg == "planned":
        sql += " AND t.status='Planned'"
    if a.get("mine"):
        sql += " AND (t.assigned_to=? OR t.raised_by=?)"
        args += [g.user["id"]] * 2
    if a.get("myteam") and g.user["team"]:
        sql += " AND t.team=?"
        args.append(g.user["team"])
    if a.get("unassigned"):
        sql += f" AND t.assigned_to IS NULL AND {LIVE_SQL}"
    if a.get("overdue"):
        sql += f" AND {OPEN} AND t.due_at < ?"
        args.append(now())
    if a.get("stale"):
        sql += f" AND {OPEN} AND t.updated_at < ?"
        args.append((datetime.now() - timedelta(hours=STALE_HOURS)).strftime(FMT))
    for key, col, allowed in (("category", "t.category", CATEGORIES), ("priority", "t.priority", PRIORITIES), ("status", "t.status", STATUSES),
                              ("team", "t.team", TEAMS), ("type", "t.request_type", REQUEST_TYPES)):
        if a.get(key) in allowed:
            sql += f" AND {col}=?"
            args.append(a[key])
    if a.get("department"):
        sql += " AND t.department=?"
        args.append(a["department"])
    term = a.get("q", "").strip()
    if term:
        sql += " AND (t.title LIKE ? OR t.description LIKE ? OR t.location LIKE ? OR t.asset_tag LIKE ? OR t.requester_name LIKE ?"
        args += [f"%{term}%"] * 5
        if m := re.fullmatch(r"(?:hd-?)?0*(\d+)", term, re.I):
            sql += " OR t.id=?"
            args.append(int(m.group(1)))
        sql += ")"
    return sql, args, scope_arg


@app.route("/tickets")
def tickets():
    sql, args, scope_arg = ticket_filters()
    order = f" ORDER BY t.status IN ('Resolved','Closed'), {PRIO_ORDER}, {DUE_ORDER}, t.id DESC"
    if request.args.get("export") == "csv":
        if g.user["role"] not in ("admin", "auditor"):
            abort(403)
        rows = q(TICKET_SQL + sql + order, args)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ticket", "type", "title", "category", "team", "priority", "status", "department", "location", "asset_tag",
                    "raised_by", "assigned_to", "created", "first_response", "due", "resolved", "resolution", "parts_used", "source"])
        for t in rows:
            w.writerow([f"HD-{t['id']:04d}", t["request_type"], t["title"], t["category"], t["team"], t["priority"], t["status"],
                        t["department"] or "", t["location"] or "", t["asset_tag"] or "", t["raiser"] or "", t["assignee"] or "",
                        t["created_at"], t["first_response_at"] or "", t["due_at"] or "", t["resolved_at"] or "", t["resolution"] or "",
                        t["parts_used"] or "", t["source"]])
        return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=tickets-{now()[:10]}.csv"})
    rows, pg = paginate(TICKET_SQL + sql + order, args)
    return render_template("tickets.html", rows=rows, pg=pg, scope=scope_arg,
                           departments=[r["name"] for r in q("SELECT name FROM departments ORDER BY name")])


def location_tree():
    """{block: {floor: [[id, room], ...]}} for the Block > Floor > Room pickers."""
    tree = {}
    for r in q("SELECT * FROM locations ORDER BY block, floor, room"):
        tree.setdefault(r["block"], {}).setdefault(r["floor"], []).append([r["id"], r["room"]])
    return tree


def form_context():
    return {"assets": asset_choices(), "location_tree": location_tree(), "TEAMS": TEAMS, "REQUEST_TYPES": REQUEST_TYPES,
            "URGENT_REASONS": URGENT_REASONS, "CATEGORY_TEAM": CATEGORY_TEAM,
            "departments": [r["name"] for r in q("SELECT name FROM departments ORDER BY name")]}


@app.route("/tickets/new", methods=["GET", "POST"])
def ticket_new():
    if g.user["role"] == "auditor":
        abort(403)
    if request.method == "POST":
        if made := new_ticket(request.form):
            tid, n = made
            msg = f"Ticket HD-{tid:04d} raised. A technician will pick it up."
            if n >= 2:
                msg += f" Note: this is the {ordinal(n)} {request.form.get('category', '')} report from this place in 30 days."
            flash(msg, "ok")
            return redirect(url_for("ticket", tid=tid))
    return render_template("ticket_form.html", preset=request.form or request.args, **form_context())


@app.route("/tickets/log", methods=["GET", "POST"])
@role(*MANAGE)
def ticket_log():
    """An IT technician records work already done (walk-up, phone or self-found), so it still counts in the history."""
    if request.method == "POST":
        if made := new_ticket(request.form, logged=True):
            flash(f"Work logged as HD-{made[0]:04d}.", "ok")
            return redirect(url_for("ticket", tid=made[0]))
    return render_template("log_work.html", preset=request.form or request.args, **form_context())


@app.route("/tickets/<int:tid>")
def ticket(tid):
    t = get_ticket(tid)
    seen_internal = g.user["role"] in (*MANAGE, "auditor")
    similar = []
    if g.user["role"] in MANAGE and t["status"] in LIVE and not t["parent_id"] and not t["child_n"] and t["location_id"]:
        # other open tickets of the same team in the same block within 6 hours: probably one outage
        similar = q(TICKET_SQL + f""" JOIN locations l ON l.id=t.location_id WHERE {LIVE_SQL} AND t.id!=? AND t.parent_id IS NULL
                    AND t.team=? AND l.block=(SELECT block FROM locations WHERE id=?)
                    AND ABS(julianday(t.created_at) - julianday(?)) < 0.25 ORDER BY t.id""", (tid, t["team"], t["location_id"], t["created_at"]))
    return render_template(
        "ticket.html", t=t, asset=asset_for(t["asset_tag"]), similar=similar,
        photos=[(p["file"], f"{p['who'] or ''} · {p['at'][:10]}") for p in q(
            "SELECT a.*, u.name who FROM attachments a LEFT JOIN users u ON u.id=a.user_id WHERE a.ticket_id=? ORDER BY a.id", (tid,))],
        comments=q("SELECT c.*, u.name who FROM comments c LEFT JOIN users u ON u.id=c.user_id WHERE ticket_id=?"
                   + ("" if seen_internal else " AND c.internal=0") + " ORDER BY c.id", (tid,)),
        history=q("SELECT s.*, u.name who FROM status_log s LEFT JOIN users u ON u.id=s.user_id WHERE ticket_id=? ORDER BY s.id", (tid,)),
        parent=q("SELECT id, title, status FROM tickets WHERE id=?", (t["parent_id"],), one=True) if t["parent_id"] else None,
        children=q("SELECT id, title, status, location FROM tickets WHERE parent_id=? ORDER BY id", (tid,)),
        techs=q("SELECT id, name, role, team FROM users WHERE role IN ('technician','admin') ORDER BY name"),
        TEAMS=TEAMS, can_confirm=can_confirm(t), sla=SLA_HOURS)


@app.route("/tickets/<int:tid>/comment", methods=["POST"])
def ticket_comment(tid):
    t = get_ticket(tid)
    body = request.form.get("body", "").strip()
    n = attach(tid)
    internal = bool(request.form.get("internal")) and g.user["role"] in MANAGE
    if body:
        note(tid, body, system=False, internal=internal)
    if n:
        note(tid, f"Added {n} photo(s)", internal=internal)
    if (body or n) and t["status"] == "Waiting on requester" and g.user["role"] not in MANAGE and not internal:
        # the requester answered: back to the technician, and the fix-by clock restarts
        set_status(t, "In progress", "Requester replied", **resume_clock(t))
    if body or n:
        db().commit()
    return redirect(url_for("ticket", tid=tid) + "#end")


@app.route("/tickets/<int:tid>/assign", methods=["POST"])
@role(*MANAGE)
def ticket_assign(tid):
    t = get_ticket(tid)
    uid = g.user["id"] if request.form.get("take") else request.form.get("assigned_to")
    tech = q("SELECT * FROM users WHERE id=? AND role IN ('technician','admin')", (uid,), one=True)
    if not tech:
        flash("Pick a technician.", "error")
    else:
        db().execute("UPDATE tickets SET assigned_to=? WHERE id=?", (tech["id"], tid))
        if t["status"] == "Open":
            set_status(t, "In progress", f"Assigned to {tech['name']}")
        else:
            note(tid, f"Assigned to {tech['name']}")
        db().commit()
        flash(f"Assigned to {tech['name']}.", "ok")
    return redirect(url_for("ticket", tid=tid))


@app.route("/tickets/<int:tid>/team", methods=["POST"])
@role(*MANAGE)
def ticket_team(tid):
    """Re-route: a power cut posted as a network ticket should go to Electrical."""
    t = get_ticket(tid)
    team = request.form.get("team")
    if team in TEAMS and team != t["team"]:
        db().execute("UPDATE tickets SET team=? WHERE id=?", (team, tid))
        note(tid, f"Routed to {team} (was {t['team']})")
        db().commit()
        flash(f"Routed to {team}.", "ok")
    return redirect(url_for("ticket", tid=tid))


@app.route("/tickets/<int:tid>/status", methods=["POST"])
@role(*MANAGE)
def ticket_status(tid):
    t = get_ticket(tid)
    to, reason = request.form.get("to"), request.form.get("reason", "").strip()
    allowed = {"In progress": ("Open", "Waiting on requester", "Planned"),
               "Waiting on requester": ("Open", "In progress"), "Planned": ("Open", "In progress", "Waiting on requester")}
    if to not in allowed or t["status"] not in allowed[to]:
        flash("That change isn't allowed from the current status.", "error")
    elif to in ("Waiting on requester", "Planned") and not reason:
        flash("Say what you need from the requester." if to == "Waiting on requester" else "Add a short note for the procurement list.", "error")
    else:
        extra = {"due_at": None} if to == "Planned" else resume_clock(t) if to == "In progress" else {}
        text = {"Waiting on requester": f"Waiting on requester: {reason}", "Planned": f"Added to procurement list: {reason}"}.get(to)
        set_status(t, to, text, **({"assigned_to": g.user["id"]} if to == "In progress" and not t["assigned_to"] else {}), **extra)
        db().commit()
        flash(f"Status: {to}.", "ok")
    return redirect(url_for("ticket", tid=tid))


@app.route("/tickets/<int:tid>/priority", methods=["POST"])
@role(*MANAGE)
def ticket_priority(tid):
    t = get_ticket(tid)
    prio = request.form.get("priority")
    if prio in PRIORITIES and prio != t["priority"]:
        due = None if not t["due_at"] else (datetime.strptime(t["created_at"], FMT) + timedelta(hours=SLA_HOURS[prio])).strftime(FMT)
        db().execute("UPDATE tickets SET priority=?, due_at=? WHERE id=?", (prio, due, tid))
        note(tid, f"Priority: {t['priority']} → {prio}")
        db().commit()
    return redirect(url_for("ticket", tid=tid))


@app.route("/tickets/<int:tid>/resolve", methods=["POST"])
@role(*MANAGE)
def ticket_resolve(tid):
    t = get_ticket(tid)
    fix = request.form.get("resolution", "").strip()
    if t["status"] in LIVE:
        if not fix:
            flash("Write what was done to fix it.", "error")
            return redirect(url_for("ticket", tid=tid))
        parts = request.form.get("parts_used", "").strip() or None
        set_status(t, "Resolved", f"Resolved: {fix}", resolved_at=now(), resolution=fix, parts_used=parts,
                   assigned_to=t["assigned_to"] or g.user["id"])
        if request.form.get("also_linked"):
            for k in q(TICKET_SQL + f" WHERE t.parent_id=? AND {LIVE_SQL}", (tid,)):
                set_status(k, "Resolved", f"Resolved with HD-{tid:04d}: {fix}", resolved_at=now(), resolution=fix,
                           assigned_to=k["assigned_to"] or g.user["id"])
        db().commit()
        flash("Marked resolved.", "ok")
    return redirect(url_for("ticket", tid=tid))


@app.route("/tickets/<int:tid>/close", methods=["POST"])
def ticket_close(tid):
    """The requester confirms the fix (or IT closes it). IT also closes a procurement-list item once delivered."""
    t = get_ticket(tid)
    if t["status"] == "Resolved" and can_confirm(t):
        set_status(t, "Closed", "Confirmed fixed and closed")
        db().commit()
        flash("Ticket closed.", "ok")
    elif t["status"] == "Planned" and g.user["role"] in MANAGE:
        set_status(t, "Closed", "Done: delivered or completed", resolved_at=now())
        db().commit()
        flash("Ticket closed.", "ok")
    return redirect(url_for("ticket", tid=tid))


@app.route("/tickets/<int:tid>/reopen", methods=["POST"])
def ticket_reopen(tid):
    t = get_ticket(tid)
    if t["status"] in ("Resolved", "Closed") and can_confirm(t):
        due = None if not t["due_at"] and t["request_type"] == "New requirement" else (
            datetime.now() + timedelta(hours=SLA_HOURS[t["priority"]])).strftime(FMT)
        set_status(t, "Open", "Reopened: problem not fixed", resolved_at=None, due_at=due)
        db().commit()
        flash("Ticket reopened.", "ok")
    return redirect(url_for("ticket", tid=tid))


@app.route("/tickets/<int:tid>/link", methods=["POST"])
@role(*MANAGE)
def ticket_link(tid):
    """Tie a ticket to the one that covers the same outage, so it is fixed once and closed together."""
    t = get_ticket(tid)
    m = re.fullmatch(r"(?:hd-?)?0*(\d+)", request.form.get("parent", "").strip(), re.I)
    parent = q(TICKET_SQL + " WHERE t.id=?", (int(m.group(1)),), one=True) if m else None
    if not parent or parent["id"] == tid or parent["parent_id"] or t["child_n"]:
        flash("Pick another ticket that isn't itself linked to one.", "error")
    else:
        db().execute("UPDATE tickets SET parent_id=? WHERE id=?", (parent["id"], tid))
        note(tid, f"Linked to HD-{parent['id']:04d} (same outage)")
        note(parent["id"], f"HD-{tid:04d} linked to this outage")
        db().commit()
        flash(f"Linked to HD-{parent['id']:04d}.", "ok")
    return redirect(url_for("ticket", tid=tid))


@app.route("/tickets/<int:tid>/unlink", methods=["POST"])
@role(*MANAGE)
def ticket_unlink(tid):
    t = get_ticket(tid)
    if t["parent_id"]:
        db().execute("UPDATE tickets SET parent_id=NULL WHERE id=?", (tid,))
        note(tid, f"Unlinked from HD-{t['parent_id']:04d}")
        db().commit()
    return redirect(url_for("ticket", tid=tid))


# ---------- users (admin) ----------

@app.route("/users")
@role("admin")
def users():
    term = request.args.get("q", "").strip()
    rows, pg = paginate("SELECT * FROM users" + (" WHERE name LIKE ? OR username LIKE ?" if term else "") + " ORDER BY role, name",
                        [f"%{term}%"] * 2 if term else [])
    return render_template("users.html", rows=rows, pg=pg, TEAMS=TEAMS, ROLE_LABEL=ROLE_LABEL,
                           departments=[r["name"] for r in q("SELECT name FROM departments ORDER BY name")])


def dept_team(f):
    dept = f.get("department") if q("SELECT 1 FROM departments WHERE name=?", (f.get("department"),), one=True) else None
    return dept, f.get("team") if f.get("team") in TEAMS else None


@app.route("/users/add", methods=["POST"])
@role("admin")
def user_add():
    f = request.form
    if len(f.get("password", "")) < 8 or not f.get("username", "").strip() or not f.get("name", "").strip() or f.get("role") not in ROLES:
        flash("Name, username, role and a password of 8+ characters are required.", "error")
    else:
        try:
            db().execute("INSERT INTO users(username, name, password_hash, role, department, team) VALUES (?,?,?,?,?,?)",
                         (f["username"].strip().lower(), f["name"].strip(), generate_password_hash(f["password"]), f["role"], *dept_team(f)))
            db().commit()
            flash("User added.", "ok")
        except sqlite3.IntegrityError:
            flash("Username already taken.", "error")
    return redirect(url_for("users"))


@app.route("/users/<int:uid>/update", methods=["POST"])
@role("admin")
def user_update(uid):
    f = request.form
    if f.get("password") and len(f["password"]) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("users"))
    if f.get("role") in ROLES and uid != g.user["id"]:
        db().execute("UPDATE users SET role=? WHERE id=?", (f["role"], uid))
    db().execute("UPDATE users SET department=?, team=? WHERE id=?", (*dept_team(f), uid))
    if f.get("password"):
        db().execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(f["password"]), uid))
    db().commit()
    flash("User updated.", "ok")
    return redirect(url_for("users"))


@app.route("/users/<int:uid>/delete", methods=["POST"])
@role("admin")
def user_delete(uid):
    if uid == g.user["id"]:
        flash("You can't delete yourself.", "error")
    else:
        for t, c in (("tickets", "raised_by"), ("tickets", "assigned_to"), ("comments", "user_id"), ("status_log", "user_id"), ("attachments", "user_id")):
            db().execute(f"UPDATE {t} SET {c}=NULL WHERE {c}=?", (uid,))
        db().execute("DELETE FROM users WHERE id=?", (uid,))
        db().commit()
        flash("User deleted.", "ok")
    return redirect(url_for("users"))


# ---------- settings: places and departments (admin) ----------

@app.route("/settings")
@role("admin")
def settings():
    return render_template("settings.html", locations=q("""SELECT l.*, (SELECT COUNT(*) FROM tickets t WHERE t.location_id=l.id) used
                                                           FROM locations l ORDER BY block, floor, room"""),
                           departments=q("SELECT d.*, (SELECT COUNT(*) FROM tickets t WHERE t.department=d.name) used FROM departments d ORDER BY name"))


@app.route("/settings/locations/add", methods=["POST"])
@role("admin")
def location_add():
    """One place, or many pasted as lines of 'Block, Floor, Room'."""
    f = request.form
    lines = [f"{f.get('block', '')},{f.get('floor', '')},{f.get('room', '')}"] if f.get("room") else f.get("bulk", "").splitlines()
    added = skipped = 0
    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3 or not all(parts[:3]):
            skipped += bool(line.strip())
            continue
        added += db().execute("INSERT OR IGNORE INTO locations(block, floor, room) VALUES (?,?,?)", parts[:3]).rowcount
    db().commit()
    flash(f"{added} place(s) added." + (f" {skipped} line(s) skipped: use Block, Floor, Room." if skipped else ""), "ok" if added else "error")
    return redirect(url_for("settings"))


@app.route("/settings/locations/<int:lid>/delete", methods=["POST"])
@role("admin")
def location_delete(lid):
    if q("SELECT 1 FROM tickets WHERE location_id=? LIMIT 1", (lid,), one=True):
        flash("Tickets use this place, so it stays.", "error")
    else:
        db().execute("DELETE FROM locations WHERE id=?", (lid,))
        db().commit()
    return redirect(url_for("settings"))


@app.route("/settings/departments/add", methods=["POST"])
@role("admin")
def department_add():
    name = request.form.get("name", "").strip()
    if name:
        db().execute("INSERT OR IGNORE INTO departments(name) VALUES (?)", (name,))
        db().commit()
    return redirect(url_for("settings"))


@app.route("/settings/departments/<int:did>/delete", methods=["POST"])
@role("admin")
def department_delete(did):
    d = q("SELECT * FROM departments WHERE id=?", (did,), one=True)
    if d and (q("SELECT 1 FROM tickets WHERE department=? LIMIT 1", (d["name"],), one=True)
              or q("SELECT 1 FROM users WHERE department=? LIMIT 1", (d["name"],), one=True)):
        flash("Tickets or users use this department, so it stays.", "error")
    elif d:
        db().execute("DELETE FROM departments WHERE id=?", (did,))
        db().commit()
    return redirect(url_for("settings"))


@app.errorhandler(400)
def bad_request(e):
    msg = "That form expired. Go back, refresh the page and try again." if "expired" in str(e.description) else "Bad request."
    return render_template("error.html", msg=msg), 400


@app.errorhandler(413)
def too_large(_):
    return render_template("error.html", msg="That upload is too big. Photos can be 4 MB each, up to 5 at a time."), 413


@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", msg="Your role can't do that."), 403


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", msg="Not found."), 404


@app.errorhandler(500)
def server_error(_):
    db().rollback()
    return render_template("error.html", msg="Something went wrong. Try again, or tell the admin."), 500


# ---------- demo data ----------

# demo-only accounts (password demo-<username>-2026), shown as buttons on the login page with --demo
# (username, name, role, department, team)
DEMO_USERS = [("admin", "Demo Admin", "admin", None, None),
              ("tech", "Ravi (IT Network)", "technician", None, "IT Network"),
              ("tech2", "Kumar (IT Systems)", "technician", None, "IT Systems"),
              ("staff", "Priya (Faculty, CSE)", "staff", "CSE", None),
              ("staff2", "Arun (Faculty, Mechanical)", "staff", "Mechanical", None),
              ("coord", "Anitha (CSE coordinator)", "coordinator", "CSE", None),
              ("hod", "Dr. Mohan (HOD, CSE)", "hod", "CSE", None),
              ("auditor", "Auditor", "auditor", None, None)]
DEMO_PLACES = [("A Block", "Ground floor", "A001 Server room"), ("A Block", "1st floor", "A106 HOD room"), ("A Block", "3rd floor", "A301 Faculty room"),
               ("B Block", "1st floor", "B101"), ("B Block", "1st floor", "B113"), ("B Block", "2nd floor", "B205 Microprocessor lab"),
               ("B Block", "2nd floor", "B206 Microcontroller lab"), ("B Block", "3rd floor", "B306 Mechatronics staff room"),
               ("B Block", "4th floor", "B407 Mechatronics library"), ("C Block", "3rd floor", "C306"), ("C Block", "4th floor", "C401 CAD lab"),
               ("D Block", "1st floor", "D112"), ("D Block", "5th floor", "D513 Staff room"), ("New Academic Block", "1st floor", "ANew101"),
               ("New Academic Block", "2nd floor", "ANew202"), ("Main Block", "Ground floor", "Principal office"),
               ("Main Block", "1st floor", "Library"), ("Exam Cell", "Ground floor", "Exam Cell office")]


def seed_demo():
    with sqlite3.connect(DB) as c:
        if c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return
        for u, n, r, d, team in DEMO_USERS:
            c.execute("INSERT INTO users(username, name, password_hash, role, department, team) VALUES (?,?,?,?,?,?)",
                      (u, n, generate_password_hash(f"demo-{u}-2026"), r, d, team))
        c.executemany("INSERT OR IGNORE INTO locations(block, floor, room) VALUES (?,?,?)", DEMO_PLACES)
        uid = {u: i for i, u in c.execute("SELECT id, username FROM users")}
        place = {room.split()[0]: (i, f"{b} · {f} · {room}") for i, b, f, room in c.execute("SELECT id, block, floor, room FROM locations")}
        n = datetime.now()

        def add(title, desc, cat, prio, status, where, raiser, tech=None, hours_ago=1, rtype="Fault", dept=None, fix=None, fixed_after_h=None,
                reason=None, ask=None, tag=None, parent=None, source="Web", parts=None, quiet=False):
            created = n - timedelta(hours=hours_ago)
            stamp = created.strftime(FMT)
            loc_id, loc = place.get(where, (None, where))
            due = None if rtype == "New requirement" or status == "Planned" else (created + timedelta(hours=SLA_HOURS[prio])).strftime(FMT)
            resolved = (created + timedelta(hours=fixed_after_h)).strftime(FMT) if fixed_after_h is not None else None
            first = (created + timedelta(minutes=20)).strftime(FMT) if (tech or source != "Web") else None
            touched = stamp if quiet else (resolved or first or stamp)
            cur = c.execute("""INSERT INTO tickets(title, description, category, priority, status, location, location_id, asset_tag, raised_by,
                               assigned_to, created_at, due_at, resolved_at, resolution, parts_used, request_type, team, department,
                               urgent_reason, first_response_at, updated_at, parent_id, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (title, desc, cat, prio, status, loc, loc_id, tag, uid[raiser], uid[tech] if tech else None, stamp, due, resolved,
                             fix if resolved else None, parts, rtype, CATEGORY_TEAM[cat], dept or next((d for u_, _, _, d, _ in DEMO_USERS if u_ == raiser), None),
                             reason, first, touched, parent, source))
            tid = cur.lastrowid

            def log(text, who, at, system=1, internal=0):
                c.execute("INSERT INTO comments(ticket_id, user_id, body, system, at, internal) VALUES (?,?,?,?,?,?)", (tid, uid[who], text, system, at, internal))

            def step(a, b, at, who):
                c.execute("INSERT INTO status_log(ticket_id, user_id, from_status, to_status, at) VALUES (?,?,?,?,?)", (tid, uid[who], a, b, at))

            step(None, "Open", stamp, raiser)
            log("Ticket raised", raiser, stamp)
            if reason:
                log(f"Urgent: {reason}. Priority set to {prio}.", raiser, stamp)
            if tech:
                log(f"Assigned to {tech}", "admin", first)
                if status != "Open":
                    step("Open", "In progress", first, "admin")
            if status == "Waiting on requester":
                log("Waiting on requester: " + ask, tech, first)
                step("In progress", "Waiting on requester", first, tech)
            if fix and resolved:
                log(f"Resolved: {fix}", tech or "tech", resolved)
                step("In progress", "Resolved", resolved, tech or "tech")
                if status == "Closed":
                    log("Confirmed fixed and closed", raiser, resolved)
                    step("Resolved", "Closed", resolved, raiser)
            if status == "Planned":
                log("Added to procurement list: smart panel to be considered in the next purchase", "admin", first or stamp)
                step("Open", "Planned", first or stamp, "admin")
            return tid

        # a room that keeps having the same problem (repeat detection)
        add("Wi-Fi not working in HOD room", "No connection since morning.", "Wi-Fi / Internet", "Medium", "Closed", "A106", "coord", "tech",
            hours_ago=24 * 12, fix="Replaced the access point patch cable.", fixed_after_h=3)
        add("Wi-Fi drops in HOD room", "Disconnects every few minutes.", "Wi-Fi / Internet", "Medium", "Closed", "A106", "coord", "tech",
            hours_ago=24 * 6, fix="Moved the access point to a clear channel.", fixed_after_h=5)
        add("Wi-Fi not working in HOD room again", "Third time this month.", "Wi-Fi / Internet", "Medium", "In progress", "A106", "coord", "tech", hours_ago=4)
        # one outage reported by several rooms
        parent = add("Internet not working in B306 staff room", "Whole Mechatronics floor offline.", "Wi-Fi / Internet", "High", "In progress", "B306",
                     "staff2", "tech", hours_ago=3)
        add("Internet not working in B407 library room", "", "Wi-Fi / Internet", "Medium", "Open", "B407", "staff2", hours_ago=2.5, parent=parent)
        add("No internet in B113", "", "Wi-Fi / Internet", "Medium", "Open", "B113", "staff2", hours_ago=2, parent=parent)
        add("Network slow in CAD lab during exam", "End semester practical running, students cannot load the exam page.", "Wi-Fi / Internet", "Medium",
            "In progress", "C401", "staff", "tech", hours_ago=1, reason="Exam in progress")
        add("System very slow, Word takes minutes to open", "Slow since MS Office was reinstalled.", "Computer / hardware", "Medium", "In progress", "C306",
            "staff2", "tech2", hours_ago=30)
        add("Scanning not working on office printer", "Scanner light blinks, no scan to PC.", "Printer / scanner", "Medium", "Waiting on requester",
            "A301", "staff", "tech2", hours_ago=20, ask="Which printer model and PC name is it?")
        add("Please install a smart board in the CAD lab", "Teaching needs a smart panel; about 60 students per batch.", "Smart board / projector / AV",
            "Medium", "Planned", "C401", "hod", rtype="New requirement", hours_ago=60)
        add("No power in ANew101 and ANew102", "Lights and fans off, AC working.", "Power / UPS", "High", "Resolved", "ANew101", "staff", "tech2",
            hours_ago=26, fix="Tripped MCB reset; loose neutral tightened.", fixed_after_h=1)
        add("UPS beeping continuously", "Battery warning since morning.", "Power / UPS", "High", "Closed", "A001", "admin", "tech2", hours_ago=90,
            fix="Replaced two failed batteries.", fixed_after_h=6, parts="2 x 12V 9Ah battery")
        add("Which printer can I use on the B Block 1st floor?", "", "Printer / scanner", "Low", "Closed", "B101", "staff2", "tech2", rtype="Question",
            hours_ago=50, fix="No printer on that floor; use the one in room B115.", fixed_after_h=1)
        add("Replaced motherboard in lab system", "Walk-up fix, no ticket was raised.", "Computer / hardware", "Low", "Closed", "B205", "tech2", "tech2",
            hours_ago=40, fix="Replaced the failed motherboard from stock.", fixed_after_h=0, source="Logged by technician", parts="1 x motherboard")
        add("Projector shows no signal", "HDMI connected, class waiting.", "Smart board / projector / AV", "High", "Open", "ANew202", "staff", hours_ago=40, quiet=True)
        c.commit()


init_db()
if os.environ.get("DEMO"):  # hosted demo (render.yaml): same as --demo
    seed_demo()
    app.config["DEMO"] = True
if __name__ == "__main__":
    if "--demo" in sys.argv:
        seed_demo()
        app.config["DEMO"] = True
    app.run(host="0.0.0.0" if "--lan" in sys.argv else "127.0.0.1", port=5002, debug="--debug" in sys.argv)
