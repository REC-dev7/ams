"""REC Helpdesk: report and fix problems with campus electronics.

Run:        python app.py           -> http://localhost:5002
Demo data:  python app.py --demo
Separate system from the AMS (own database). Optional link: if the AMS database is found,
tickets can point at an AMS asset tag and show its name and location (read-only).
"""
import os
import re
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, abort, flash, g, redirect, render_template, request, send_from_directory, session, url_for
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "helpdesk.db")
KEY_FILE = os.path.join(HERE, "secret.key")
# ponytail: AMS link is a read-only peek at its sqlite file; swap for an HTTP API if they move to different servers
AMS_DB = os.environ.get("AMS_DB", os.path.join(HERE, "..", "ams.db"))

ROLES = ["admin", "technician", "staff"]
CATEGORIES = ["Hardware", "Network", "Software", "Power", "Other"]
PRIORITIES = ["Low", "Medium", "High", "Critical"]
STATUSES = ["Open", "In progress", "Resolved", "Closed"]
# fix-by time from the moment a ticket is raised
SLA_HOURS = {"Critical": 4, "High": 24, "Medium": 72, "Low": 168}
FMT = "%Y-%m-%d %H:%M:%S"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('admin','technician','staff')));
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT, category TEXT, priority TEXT,
  status TEXT NOT NULL DEFAULT 'Open', location TEXT, asset_tag TEXT,
  raised_by INTEGER REFERENCES users(id), assigned_to INTEGER REFERENCES users(id),
  created_at TEXT, due_at TEXT, resolved_at TEXT, resolution TEXT);
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  user_id INTEGER REFERENCES users(id), body TEXT NOT NULL, system INTEGER DEFAULT 0, at TEXT);
CREATE TABLE IF NOT EXISTS attachments (
  id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  file TEXT NOT NULL, user_id INTEGER REFERENCES users(id), at TEXT);
"""

app = Flask(__name__)
if not os.path.exists(KEY_FILE):
    with open(KEY_FILE, "w") as fh:
        fh.write(secrets.token_hex(32))
with open(KEY_FILE) as fh:
    app.secret_key = fh.read().strip()
# own cookie name: browsers share cookies across ports on localhost, so apps on :5000/:5002 would overwrite each other
app.config.update(SESSION_COOKIE_NAME="helpdesk_session", SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")), MAX_CONTENT_LENGTH=12 * 1024 * 1024)


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
    """'overdue 3h' / 'due in 2d' for an unresolved ticket."""
    if t["status"] not in ("Open", "In progress") or not t["due_at"]:
        return ""
    secs = (datetime.strptime(t["due_at"], FMT) - datetime.now()).total_seconds()
    mins = int(abs(secs) // 60)
    span = f"{mins // 1440}d" if mins >= 2880 else f"{mins // 60}h" if mins >= 120 else f"{mins}m"
    return f"overdue {span}" if secs < 0 else f"due in {span}"


app.jinja_env.globals.update(overdue=overdue, due_text=due_text)


@app.context_processor
def template_globals():
    token = session.setdefault("csrf", secrets.token_hex(16))
    user = g.get("user")
    return {
        "other_url": os.environ.get("OTHER_APP_URL"),
        "user": user, "csrf": Markup(f'<input type="hidden" name="csrf" value="{token}">'),
        "can": lambda *roles: bool(user) and user["role"] in roles,
        "CATEGORIES": CATEGORIES, "PRIORITIES": PRIORITIES, "STATUSES": STATUSES, "ROLES": ROLES,
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
    return render_template("login.html", demo=DEMO_USERS if app.config.get("DEMO") else [])


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

TICKET_SQL = """SELECT t.*, u.name raiser, a.name assignee FROM tickets t
                LEFT JOIN users u ON u.id=t.raised_by LEFT JOIN users a ON a.id=t.assigned_to"""
PRIO_ORDER = "CASE t.priority WHEN 'Critical' THEN 0 WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END"
OPEN = "t.status IN ('Open','In progress')"


def note(tid, text, system=True):
    db().execute("INSERT INTO comments(ticket_id, user_id, body, system, at) VALUES (?,?,?,?,?)",
                 (tid, g.user["id"], text, int(system), now()))


@app.route("/")
def dashboard():
    stats = {
        "open": q(f"SELECT COUNT(*) n FROM tickets t WHERE {OPEN}", one=True)["n"],
        "unassigned": q(f"SELECT COUNT(*) n FROM tickets t WHERE {OPEN} AND assigned_to IS NULL", one=True)["n"],
        "overdue": q(f"SELECT COUNT(*) n FROM tickets t WHERE {OPEN} AND due_at < ?", (now(),), one=True)["n"],
        "urgent": q(f"SELECT COUNT(*) n FROM tickets t WHERE {OPEN} AND priority IN ('Critical','High')", one=True)["n"],
        "resolved_week": q("SELECT COUNT(*) n FROM tickets WHERE status IN ('Resolved','Closed') AND resolved_at >= ?",
                           ((datetime.now() - timedelta(days=7)).strftime(FMT),), one=True)["n"],
    }
    fixed = q("SELECT created_at, resolved_at FROM tickets WHERE resolved_at IS NOT NULL")
    hrs = [(datetime.strptime(r["resolved_at"], FMT) - datetime.strptime(r["created_at"], FMT)).total_seconds() / 3600 for r in fixed]
    stats["avg_fix"] = round(sum(hrs) / len(hrs), 1) if hrs else None
    work = q(TICKET_SQL + f" WHERE {OPEN} ORDER BY {PRIO_ORDER}, t.due_at LIMIT 8")
    col = "raised_by" if g.user["role"] == "staff" else "assigned_to"
    mine = q(TICKET_SQL + f" WHERE {OPEN} AND t.{col}=? ORDER BY {PRIO_ORDER}, t.due_at", (g.user["id"],))
    by_cat = q(f"SELECT category k, COUNT(*) n FROM tickets t WHERE {OPEN} GROUP BY category ORDER BY n DESC")
    top = max([r["n"] for r in by_cat] or [1])
    return render_template("dashboard.html", stats=stats, work=work, mine=mine,
                           by_cat=[{"label": r["k"], "n": r["n"], "pct": round(r["n"] * 100 / top)} for r in by_cat])


@app.route("/tickets")
def tickets():
    a = request.args
    sql, args = " WHERE 1=1", []
    scope = a.get("scope", "open")
    if scope == "open":
        sql += f" AND {OPEN}"
    elif scope == "closed":
        sql += " AND t.status IN ('Resolved','Closed')"
    if a.get("mine"):
        sql += " AND (t.assigned_to=? OR t.raised_by=?)"
        args += [g.user["id"]] * 2
    if a.get("unassigned"):
        sql += f" AND t.assigned_to IS NULL AND {OPEN}"
    if a.get("overdue"):
        sql += f" AND {OPEN} AND t.due_at < ?"
        args.append(now())
    for key, col, allowed in (("category", "t.category", CATEGORIES), ("priority", "t.priority", PRIORITIES),
                              ("status", "t.status", STATUSES)):
        if a.get(key) in allowed:
            sql += f" AND {col}=?"
            args.append(a[key])
    if a.get("q"):
        sql += " AND (t.title LIKE ? OR t.description LIKE ? OR t.location LIKE ? OR t.asset_tag LIKE ?)"
        args += [f"%{a['q']}%"] * 4
    rows, pg = paginate(TICKET_SQL + sql + f" ORDER BY t.status IN ('Resolved','Closed'), {PRIO_ORDER}, t.due_at, t.id DESC", args)
    return render_template("tickets.html", rows=rows, pg=pg, scope=scope)


@app.route("/tickets/new", methods=["GET", "POST"])
def ticket_new():
    if request.method == "POST":
        f = request.form
        title = f.get("title", "").strip()
        if not title:
            flash("Describe the problem in the title.", "error")
        else:
            prio = f.get("priority") if f.get("priority") in PRIORITIES else "Medium"
            tag = (f.get("asset_tag") or "").strip() or None
            loc = (f.get("location") or "").strip() or None
            if tag and (found := asset_for(tag)) and not loc:
                loc = f"{found['building']} · {found['room']}"
            created = datetime.now()
            cur = db().execute(
                """INSERT INTO tickets(title, description, category, priority, location, asset_tag, raised_by, created_at, due_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (title, f.get("description", "").strip(), f.get("category") if f.get("category") in CATEGORIES else "Other",
                 prio, loc, tag, g.user["id"], created.strftime(FMT),
                 (created + timedelta(hours=SLA_HOURS[prio])).strftime(FMT)))
            note(cur.lastrowid, "Ticket raised")
            attach(cur.lastrowid)
            db().commit()
            flash(f"Ticket HD-{cur.lastrowid:04d} raised. A technician will pick it up.", "ok")
            return redirect(url_for("ticket", tid=cur.lastrowid))
    return render_template("ticket_form.html", assets=asset_choices(), preset=request.args)


@app.route("/tickets/<int:tid>")
def ticket(tid):
    t = q(TICKET_SQL + " WHERE t.id=?", (tid,), one=True)
    if not t:
        abort(404)
    return render_template(
        "ticket.html", t=t, asset=asset_for(t["asset_tag"]),
        photos=[(p["file"], f"{p['who'] or ''} · {p['at'][:10]}") for p in q(
            "SELECT a.*, u.name who FROM attachments a LEFT JOIN users u ON u.id=a.user_id WHERE a.ticket_id=? ORDER BY a.id", (tid,))],
        comments=q("SELECT c.*, u.name who FROM comments c LEFT JOIN users u ON u.id=c.user_id WHERE ticket_id=? ORDER BY c.id", (tid,)),
        techs=q("SELECT id, name, role FROM users WHERE role IN ('technician','admin') ORDER BY name"),
        sla=SLA_HOURS)


def get_ticket(tid):
    t = q("SELECT * FROM tickets WHERE id=?", (tid,), one=True)
    if not t:
        abort(404)
    return t


@app.route("/tickets/<int:tid>/comment", methods=["POST"])
def ticket_comment(tid):
    get_ticket(tid)
    body = request.form.get("body", "").strip()
    n = attach(tid)
    if body:
        note(tid, body, system=False)
    if n:
        note(tid, f"Added {n} photo(s)")
    if body or n:
        db().commit()
    return redirect(url_for("ticket", tid=tid) + "#end")


@app.route("/tickets/<int:tid>/assign", methods=["POST"])
@role("admin", "technician")
def ticket_assign(tid):
    t = get_ticket(tid)
    uid = g.user["id"] if request.form.get("take") else request.form.get("assigned_to")
    tech = q("SELECT * FROM users WHERE id=? AND role IN ('technician','admin')", (uid,), one=True)
    if not tech:
        flash("Pick a technician.", "error")
    else:
        status = "In progress" if t["status"] == "Open" else t["status"]
        db().execute("UPDATE tickets SET assigned_to=?, status=? WHERE id=?", (tech["id"], status, tid))
        note(tid, f"Assigned to {tech['name']}")
        db().commit()
        flash(f"Assigned to {tech['name']}.", "ok")
    return redirect(url_for("ticket", tid=tid))


@app.route("/tickets/<int:tid>/priority", methods=["POST"])
@role("admin", "technician")
def ticket_priority(tid):
    t = get_ticket(tid)
    prio = request.form.get("priority")
    if prio in PRIORITIES and prio != t["priority"]:
        due = datetime.strptime(t["created_at"], FMT) + timedelta(hours=SLA_HOURS[prio])
        db().execute("UPDATE tickets SET priority=?, due_at=? WHERE id=?", (prio, due.strftime(FMT), tid))
        note(tid, f"Priority: {t['priority']} → {prio}")
        db().commit()
    return redirect(url_for("ticket", tid=tid))


@app.route("/tickets/<int:tid>/resolve", methods=["POST"])
@role("admin", "technician")
def ticket_resolve(tid):
    t = get_ticket(tid)
    fix = request.form.get("resolution", "").strip()
    if t["status"] in ("Open", "In progress"):
        if not fix:
            flash("Write what was done to fix it.", "error")
            return redirect(url_for("ticket", tid=tid))
        db().execute("UPDATE tickets SET status='Resolved', resolved_at=?, resolution=?, assigned_to=COALESCE(assigned_to, ?) WHERE id=?",
                     (now(), fix, g.user["id"], tid))
        note(tid, f"Resolved: {fix}")
        db().commit()
        flash("Marked resolved.", "ok")
    return redirect(url_for("ticket", tid=tid))


@app.route("/tickets/<int:tid>/close", methods=["POST"])
def ticket_close(tid):
    """Raiser confirms the fix (or a technician/admin closes it)."""
    t = get_ticket(tid)
    if t["status"] == "Resolved" and (t["raised_by"] == g.user["id"] or g.user["role"] in ("admin", "technician")):
        db().execute("UPDATE tickets SET status='Closed' WHERE id=?", (tid,))
        note(tid, "Confirmed fixed and closed")
        db().commit()
        flash("Ticket closed.", "ok")
    return redirect(url_for("ticket", tid=tid))


@app.route("/tickets/<int:tid>/reopen", methods=["POST"])
def ticket_reopen(tid):
    t = get_ticket(tid)
    if t["status"] in ("Resolved", "Closed") and (t["raised_by"] == g.user["id"] or g.user["role"] in ("admin", "technician")):
        due = datetime.now() + timedelta(hours=SLA_HOURS[t["priority"]])
        db().execute("UPDATE tickets SET status='Open', resolved_at=NULL, due_at=? WHERE id=?", (due.strftime(FMT), tid))
        note(tid, "Reopened: problem not fixed")
        db().commit()
        flash("Ticket reopened.", "ok")
    return redirect(url_for("ticket", tid=tid))


# ---------- users (admin) ----------

@app.route("/users")
@role("admin")
def users():
    term = request.args.get("q", "").strip()
    rows, pg = paginate("SELECT * FROM users" + (" WHERE name LIKE ? OR username LIKE ?" if term else "") + " ORDER BY role, name",
                        [f"%{term}%"] * 2 if term else [])
    return render_template("users.html", rows=rows, pg=pg)


@app.route("/users/add", methods=["POST"])
@role("admin")
def user_add():
    f = request.form
    if len(f.get("password", "")) < 8 or not f.get("username", "").strip() or not f.get("name", "").strip() or f.get("role") not in ROLES:
        flash("Name, username, role and a password of 8+ characters are required.", "error")
    else:
        try:
            db().execute("INSERT INTO users(username, name, password_hash, role) VALUES (?,?,?,?)",
                         (f["username"].strip().lower(), f["name"].strip(), generate_password_hash(f["password"]), f["role"]))
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
        for t, c in (("tickets", "raised_by"), ("tickets", "assigned_to"), ("comments", "user_id")):
            db().execute(f"UPDATE {t} SET {c}=NULL WHERE {c}=?", (uid,))
        db().execute("DELETE FROM users WHERE id=?", (uid,))
        db().commit()
        flash("User deleted.", "ok")
    return redirect(url_for("users"))


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
DEMO_USERS = [("admin", "Demo Admin", "admin"), ("tech", "Ravi (Technician)", "technician"),
              ("staff", "Priya (Staff)", "staff")]


def seed_demo():
    with sqlite3.connect(DB) as c:
        if c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return
        for u, n, r in DEMO_USERS + [("tech2", "Kumar (Technician)", "technician")]:
            c.execute("INSERT INTO users(username, name, password_hash, role) VALUES (?,?,?,?)",
                      (u, n, generate_password_hash(f"demo-{u}-2026"), r))
        uid = {u: i for i, u in c.execute("SELECT id, username FROM users")}
        n = datetime.now()

        def add(title, desc, cat, prio, status, loc, tag, raiser, tech, hours_ago, fix=None, fixed_after_h=None):
            created = n - timedelta(hours=hours_ago)
            due = created + timedelta(hours=SLA_HOURS[prio])
            resolved = (created + timedelta(hours=fixed_after_h)).strftime(FMT) if fixed_after_h is not None else None
            cur = c.execute("""INSERT INTO tickets(title, description, category, priority, status, location, asset_tag,
                               raised_by, assigned_to, created_at, due_at, resolved_at, resolution) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (title, desc, cat, prio, status, loc, tag, uid[raiser], uid[tech] if tech else None,
                             created.strftime(FMT), due.strftime(FMT), resolved, fix))
            c.execute("INSERT INTO comments(ticket_id, user_id, body, system, at) VALUES (?,?,?,1,?)",
                      (cur.lastrowid, uid[raiser], "Ticket raised", created.strftime(FMT)))
            if tech:
                c.execute("INSERT INTO comments(ticket_id, user_id, body, system, at) VALUES (?,?,?,1,?)",
                          (cur.lastrowid, uid["admin"], f"Assigned to {tech}", (created + timedelta(minutes=20)).strftime(FMT)))
            if fix:
                c.execute("INSERT INTO comments(ticket_id, user_id, body, system, at) VALUES (?,?,?,1,?)",
                          (cur.lastrowid, uid[tech or "tech"], f"Resolved: {fix}", resolved))

        add("System not booting", "Black screen after the Dell logo. Tried power cycle twice.", "Hardware", "High",
            "In progress", "CSE Block · Computer Lab 1", "AMS-00013", "staff", "tech", 20)
        add("Wi-Fi very weak at back of library", "Students at the last tables cannot load pages.", "Network", "Medium",
            "Open", "Main Block · Library", None, "staff", None, 30)
        add("Projector shows no signal", "HDMI cable connected, projector says no signal. Class waiting.", "Hardware", "Critical",
            "Open", "CSE Block · CS-201", None, "staff", None, 5)
        add("MATLAB licence error on lab PCs", "License manager cannot be reached from PC-03.", "Software", "Low",
            "In progress", "CSE Block · Computer Lab 2", None, "staff", "tech2", 60)
        add("Printer paper jam every page", "Principal office laser printer jams on every print.", "Hardware", "Medium",
            "Open", "Main Block · Principal Office", None, "staff", None, 100)
        add("UPS beeping in server room", "Continuous beep since morning, battery warning.", "Power", "High",
            "Resolved", "CSE Block · Server Room", None, "admin", "tech", 26,
            "Replaced two failed batteries, load test passed.", 6)
        add("Keyboard keys not working", "Several keys dead on lab 2 PC-05.", "Hardware", "Low",
            "Closed", "CSE Block · Computer Lab 2", None, "staff", "tech2", 200, "Replaced keyboard from store.", 20)


init_db()
if os.environ.get("DEMO"):  # hosted demo (render.yaml): same as --demo
    seed_demo()
    app.config["DEMO"] = True
if __name__ == "__main__":
    if "--demo" in sys.argv:
        seed_demo()
        app.config["DEMO"] = True
    app.run(host="0.0.0.0" if "--lan" in sys.argv else "127.0.0.1", port=5002, debug="--debug" in sys.argv)
