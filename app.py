"""College Asset Management System (AMS).

Run:        python app.py           -> http://localhost:5000
Demo data:  python app.py --demo    (adds sample buildings, labs and assets, then runs)
"""
import csv
import io
import os
import re
import secrets
import sqlite3
import sys
from datetime import date, timedelta
from functools import wraps

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, send_from_directory, session, url_for
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "ams.db")
KEY_FILE = os.path.join(HERE, "secret.key")

ROLES = ["admin", "technician", "staff"]
STATUSES = ["Working", "Faulty", "Under service", "In store", "Scrapped"]
ROOM_TYPES = ["Lab", "Classroom", "Office", "Network room", "Library", "Seminar hall", "Other"]
ISSUE_TYPES = ["Hardware", "Network", "Software", "Power", "Other"]
PRIORITIES = ["Low", "Medium", "High"]
SERVICE_TYPES = ["Repair", "Preventive maintenance", "Upgrade", "Part replacement", "Inspection"]
DEFAULT_CATEGORIES = [
    "Desktop computer", "Laptop", "Monitor", "Keyboard", "Mouse", "RAM", "Hard disk / SSD",
    "Motherboard", "Power supply", "Printer", "Scanner", "Projector", "Smart board",
    "Network switch", "Router", "Wi-Fi access point", "Server", "Firewall", "UPS",
    "CCTV camera", "Air conditioner", "Lab instrument", "Other electronics",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('admin','technician','staff')));
CREATE TABLE IF NOT EXISTS buildings (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, code TEXT);
CREATE TABLE IF NOT EXISTS rooms (
  id INTEGER PRIMARY KEY, building_id INTEGER NOT NULL REFERENCES buildings(id),
  name TEXT NOT NULL, type TEXT, floor TEXT, UNIQUE (building_id, name));
CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS assets (
  id INTEGER PRIMARY KEY, tag TEXT UNIQUE, name TEXT NOT NULL,
  category_id INTEGER REFERENCES categories(id), room_id INTEGER REFERENCES rooms(id),
  parent_id INTEGER REFERENCES assets(id), status TEXT NOT NULL DEFAULT 'Working',
  brand TEXT, model TEXT, serial_no TEXT, ip_address TEXT, mac_address TEXT,
  purchase_date TEXT, warranty_until TEXT, cost REAL, vendor TEXT, notes TEXT,
  next_service_due TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS issues (
  id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  title TEXT NOT NULL, type TEXT, priority TEXT, description TEXT,
  status TEXT NOT NULL DEFAULT 'Open', reported_by INTEGER REFERENCES users(id),
  reported_at TEXT DEFAULT CURRENT_TIMESTAMP,
  resolved_by INTEGER REFERENCES users(id), resolved_at TEXT, resolution TEXT);
CREATE TABLE IF NOT EXISTS services (
  id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  date TEXT NOT NULL, type TEXT, description TEXT, done_by TEXT, cost REAL, next_due TEXT,
  logged_by INTEGER REFERENCES users(id));
CREATE TABLE IF NOT EXISTS activity (
  id INTEGER PRIMARY KEY, asset_id INTEGER REFERENCES assets(id) ON DELETE CASCADE,
  user_id INTEGER REFERENCES users(id), text TEXT NOT NULL, at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS photos (
  id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  issue_id INTEGER REFERENCES issues(id) ON DELETE SET NULL, file TEXT NOT NULL,
  uploaded_by INTEGER REFERENCES users(id), at TEXT DEFAULT CURRENT_TIMESTAMP);
"""

app = Flask(__name__)
if not os.path.exists(KEY_FILE):
    with open(KEY_FILE, "w") as fh:
        fh.write(secrets.token_hex(32))
with open(KEY_FILE) as fh:
    app.secret_key = fh.read().strip()
# own cookie name: browsers share cookies across ports on localhost, so apps on :5000/:5002 would overwrite each other
app.config.update(SESSION_COOKIE_NAME="ams_session", SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")), MAX_CONTENT_LENGTH=12 * 1024 * 1024, SEND_FILE_MAX_AGE_DEFAULT=86400)


# ---------- db helpers ----------

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


# columns added after v2; ALTERed into existing databases
MIGRATIONS = {
    "categories": [("life_years", "INTEGER DEFAULT 5")],
    "assets": [("last_audit", "TEXT"), ("assigned_to", "TEXT"), ("assigned_due", "TEXT")],
}
AUDIT_DAYS = 365  # stock must be physically verified at least yearly


def init_db():
    with sqlite3.connect(DB) as c:
        c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        c.executemany("INSERT OR IGNORE INTO categories(name) VALUES (?)", [(n,) for n in DEFAULT_CATEGORIES])


def book_value(cost, purchase_date, life_years):
    """Straight-line depreciation to zero over the category's useful life."""
    if cost is None or not purchase_date:
        return cost
    try:
        age = (date.today() - date.fromisoformat(purchase_date)).days / 365.25
    except ValueError:
        return cost
    life = life_years or 5
    return round(max(0.0, cost * (1 - max(age, 0) / life)), 2)


app.jinja_env.globals["book_value"] = book_value
app.jinja_env.globals["static_v"] = int(os.path.getmtime(os.path.join(HERE, "static", "style.css")))  # cache-bust: CSS/JS cached for a day


def log(asset_id, text):
    db().execute("INSERT INTO activity(asset_id, user_id, text) VALUES (?,?,?)",
                 (asset_id, g.user["id"] if g.get("user") else None, text))


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

def attach_photos(aid, issue_id=None):
    names, _ = save_photos()
    for n in names:
        db().execute("INSERT INTO photos(asset_id, issue_id, file, uploaded_by) VALUES (?,?,?,?)", (aid, issue_id, n, g.user["id"]))
    if names:
        log(aid, f"{len(names)} photo(s) added")
    return len(names)



def opt_float(v, label):
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        raise ValueError(f"{label} must be a number")


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


@app.context_processor
def template_globals():
    token = session.setdefault("csrf", secrets.token_hex(16))
    user = g.get("user")
    return {
        "other_url": os.environ.get("OTHER_APP_URL"),
        "user": user,
        "csrf": Markup(f'<input type="hidden" name="csrf" value="{token}">'),
        "can": lambda *roles: bool(user) and user["role"] in roles,
        "STATUSES": STATUSES, "ROOM_TYPES": ROOM_TYPES, "ISSUE_TYPES": ISSUE_TYPES,
        "PRIORITIES": PRIORITIES, "SERVICE_TYPES": SERVICE_TYPES, "ROLES": ROLES,
        "today": date.today().isoformat(),
    }


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


# ---------- dashboard ----------

def bars(rows):
    top = max([r["n"] for r in rows] or [1]) or 1
    return [{"label": r["k"], "n": r["n"], "pct": round(r["n"] * 100 / top)} for r in rows]


@app.route("/")
def dashboard():
    today = date.today()
    live = "a.status != 'Scrapped'"
    stats = {
        "total": q(f"SELECT COUNT(*) n FROM assets a WHERE {live}", one=True)["n"],
        "working": q("SELECT COUNT(*) n FROM assets WHERE status='Working'", one=True)["n"],
        "down": q("SELECT COUNT(*) n FROM assets WHERE status IN ('Faulty','Under service')", one=True)["n"],
        "open_issues": q("SELECT COUNT(*) n FROM issues WHERE status='Open'", one=True)["n"],
        "service_due": q(f"SELECT COUNT(*) n FROM assets a WHERE {live} AND next_service_due <= ?",
                         ((today + timedelta(days=30)).isoformat(),), one=True)["n"],
        "warranty": q(f"SELECT COUNT(*) n FROM assets a WHERE {live} AND warranty_until BETWEEN ? AND ?",
                      (today.isoformat(), (today + timedelta(days=60)).isoformat()), one=True)["n"],
        "audit_due": q(f"SELECT COUNT(*) n FROM assets a WHERE {live} AND a.parent_id IS NULL AND (last_audit IS NULL OR last_audit < ?)",
                       ((today - timedelta(days=AUDIT_DAYS)).isoformat(),), one=True)["n"],
        "loans": q("SELECT COUNT(*) n FROM assets WHERE assigned_to IS NOT NULL", one=True)["n"],
        "loans_overdue": q("SELECT COUNT(*) n FROM assets WHERE assigned_to IS NOT NULL AND assigned_due < ?",
                           (today.isoformat(),), one=True)["n"],
    }
    vals = q(f"""SELECT a.cost, a.purchase_date, c.life_years FROM assets a LEFT JOIN categories c ON c.id=a.category_id
                 WHERE {live} AND a.cost IS NOT NULL""")
    stats["value"] = sum(r["cost"] for r in vals)
    stats["book"] = sum(book_value(r["cost"], r["purchase_date"], r["life_years"]) for r in vals)
    by_cat = bars(q(f"""SELECT c.name k, COUNT(*) n FROM assets a JOIN categories c ON c.id=a.category_id
                        WHERE {live} AND a.parent_id IS NULL GROUP BY c.id ORDER BY n DESC LIMIT 8"""))
    by_building = bars(q(f"""SELECT b.name k, COUNT(*) n FROM assets a JOIN rooms r ON r.id=a.room_id
                             JOIN buildings b ON b.id=r.building_id WHERE {live} AND a.parent_id IS NULL
                             GROUP BY b.id ORDER BY n DESC"""))
    by_status = q("SELECT status k, COUNT(*) n FROM assets GROUP BY status")
    open_issues = q("""SELECT i.*, a.tag, a.name asset_name, r.name room FROM issues i
                       JOIN assets a ON a.id=i.asset_id LEFT JOIN rooms r ON r.id=a.room_id
                       WHERE i.status='Open'
                       ORDER BY CASE i.priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, i.id
                       LIMIT 8""")
    recent = q("""SELECT ac.*, a.tag, u.name who FROM activity ac LEFT JOIN assets a ON a.id=ac.asset_id
                  LEFT JOIN users u ON u.id=ac.user_id ORDER BY ac.id DESC LIMIT 10""")
    return render_template("dashboard.html", stats=stats, by_cat=by_cat, by_building=by_building,
                           by_status={r["k"]: r["n"] for r in by_status}, open_issues=open_issues, recent=recent)


# ---------- locations ----------

@app.route("/locations")
def locations():
    buildings = q("SELECT * FROM buildings ORDER BY name")
    rooms = q("""SELECT r.*,
                   (SELECT COUNT(*) FROM assets a WHERE a.room_id=r.id AND a.parent_id IS NULL AND a.status!='Scrapped') assets,
                   (SELECT COUNT(*) FROM assets a WHERE a.room_id=r.id AND a.status IN ('Faulty','Under service')) down,
                   (SELECT COUNT(*) FROM issues i JOIN assets a ON a.id=i.asset_id
                     WHERE a.room_id=r.id AND i.status='Open') issues
                 FROM rooms r ORDER BY r.floor, r.name""")
    by_b = {}
    for r in rooms:
        by_b.setdefault(r["building_id"], []).append(r)
    return render_template("locations.html", buildings=buildings, rooms=by_b)


@app.route("/rooms/<int:rid>")
def room(rid):
    rm = q("SELECT r.*, b.name building FROM rooms r JOIN buildings b ON b.id=r.building_id WHERE r.id=?", (rid,), one=True)
    if not rm:
        abort(404)
    assets = q(ASSET_SELECT + " WHERE a.room_id=? ORDER BY cat, a.name", (rid,))
    parts, groups = {}, {}
    for a in assets:
        if a["parent_id"]:
            parts.setdefault(a["parent_id"], []).append(a)
        else:
            groups.setdefault(a["cat"] or "Uncategorised", []).append(a)
    return render_template("room.html", room=rm, groups=groups, parts=parts,
                           total=sum(len(v) for v in groups.values()),
                           audit=request.args.get("audit") and g.user["role"] in ("admin", "technician"),
                           audit_cutoff=(date.today() - timedelta(days=AUDIT_DAYS)).isoformat())


@app.route("/rooms/<int:rid>/audit", methods=["POST"])
@role("admin", "technician")
def room_audit(rid):
    found = {int(x) for x in request.form.getlist("found") if x.isdigit()}
    in_room = {r["id"]: r for r in q("SELECT id, tag, name, status FROM assets WHERE room_id=? AND status!='Scrapped'", (rid,))}
    today = date.today().isoformat()
    for aid in found & in_room.keys():
        db().execute("UPDATE assets SET last_audit=? WHERE id=?", (today, aid))
        log(aid, "Verified in stock check")
    missing = [a for aid, a in in_room.items() if aid not in found]
    for a in missing:
        log(a["id"], "Not found in stock check")
    db().commit()
    msg = f"Stock check saved: {len(found & in_room.keys())} verified"
    flash(msg + (f", {len(missing)} not found (see each asset's activity)" if missing else "."), "error" if missing else "ok")
    return redirect(url_for("room", rid=rid))


@app.route("/register")
def register():
    """Printable stock register (dead stock register) by building/room."""
    where, args = " WHERE a.status!='Scrapped'", []
    for key, col in (("building", "r.building_id"), ("room", "a.room_id")):
        if opt_int(request.args.get(key)):
            where += f" AND {col}=?"
            args.append(int(request.args[key]))
    rows = q(ASSET_SELECT + where + " ORDER BY b.name, r.name, a.parent_id IS NOT NULL, cat, a.tag", args)
    groups = {}
    for a in rows:
        groups.setdefault((a["building"], a["room"]), []).append(a)
    return render_template("register.html", groups=groups, total=len(rows),
                           cost=sum(a["cost"] or 0 for a in rows),
                           book=sum(book_value(a["cost"], a["purchase_date"], a["life_years"]) or 0 for a in rows),
                           audit_cutoff=(date.today() - timedelta(days=AUDIT_DAYS)).isoformat(), **lookups())


# ---------- assets ----------

ASSET_SELECT = """SELECT a.*, c.name cat, c.life_years, r.name room, b.name building, p.tag parent_tag, p.name parent_name,
  (SELECT COUNT(*) FROM issues i WHERE i.asset_id=a.id AND i.status='Open') open_issues,
  (SELECT COUNT(*) FROM assets ch WHERE ch.parent_id=a.id) part_count
FROM assets a LEFT JOIN categories c ON c.id=a.category_id LEFT JOIN rooms r ON r.id=a.room_id
LEFT JOIN buildings b ON b.id=r.building_id LEFT JOIN assets p ON p.id=a.parent_id"""

FIELDS = ["tag", "name", "category_id", "room_id", "status", "brand", "model", "serial_no", "ip_address",
          "mac_address", "purchase_date", "warranty_until", "cost", "vendor", "notes"]
LABELS = {"tag": "Tag", "name": "Name", "category_id": "Category", "room_id": "Room", "status": "Status",
          "brand": "Brand", "model": "Model", "serial_no": "Serial no", "ip_address": "IP", "mac_address": "MAC",
          "purchase_date": "Purchase date", "warranty_until": "Warranty", "cost": "Cost", "vendor": "Vendor",
          "notes": "Notes"}


def lookups():
    return {
        "categories": q("SELECT * FROM categories ORDER BY name"),
        "buildings": q("SELECT * FROM buildings ORDER BY name"),
        "rooms": q("SELECT r.*, b.name building FROM rooms r JOIN buildings b ON b.id=r.building_id ORDER BY b.name, r.name"),
    }


def asset_filters():
    a = request.args
    sql, args = " WHERE 1=1", []
    if a.get("q"):
        sql += " AND (a.tag LIKE ? OR a.name LIKE ? OR a.serial_no LIKE ? OR a.brand LIKE ? OR a.model LIKE ? OR a.ip_address LIKE ?)"
        args += [f"%{a['q']}%"] * 6
    for key, col in (("category", "a.category_id"), ("room", "a.room_id"), ("building", "r.building_id")):
        if opt_int(a.get(key)):
            sql += f" AND {col}=?"
            args.append(int(a[key]))
    if a.get("status") in STATUSES:
        sql += " AND a.status=?"
        args.append(a["status"])
    if not a.get("parts"):
        sql += " AND a.parent_id IS NULL"
    return sql, args


@app.route("/assets")
def assets():
    where, args = asset_filters()
    order = " ORDER BY a.id DESC"
    if request.args.get("export") == "csv":
        rows = q(ASSET_SELECT + where + order, args)
        # same columns as import, so an export can be edited and re-imported elsewhere
        src = {"category": "cat"}
        return csv_response(f"assets-{date.today()}.csv", IMPORT_COLS,
                            [[r[src.get(c, c)] for c in IMPORT_COLS] for r in rows])
    rows, pg = paginate(ASSET_SELECT + where + order, args)
    return render_template("assets.html", rows=rows, pg=pg, **lookups())


IMPORT_COLS = ["tag", "name", "category", "building", "room", "status", "brand", "model", "serial_no", "ip_address",
               "mac_address", "purchase_date", "warranty_until", "cost", "vendor", "notes", "parent_tag"]


def csv_response(filename, header, rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    w.writerows(rows)
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


def import_rows(reader):
    """Insert CSV rows as assets. Missing buildings/rooms/categories are created.
    Returns (count, errors); caller commits only when errors is empty."""
    missing = {"name", "building", "room"} - set(reader.fieldnames or [])
    if missing:
        return 0, [f"Missing column(s): {', '.join(sorted(missing))}"]
    errors, count = [], 0
    for line, raw in enumerate(reader, start=2):
        r = {k: (raw.get(k) or "").strip() or None for k in IMPORT_COLS}
        if not any(r.values()):
            continue
        if not (r["name"] and r["building"] and r["room"]):
            errors.append(f"Row {line}: name, building and room are required")
            continue
        if r["status"] and r["status"] not in STATUSES:
            errors.append(f"Row {line}: status must be one of {', '.join(STATUSES)}")
            continue
        try:
            cost = opt_float(r["cost"], "cost")
        except ValueError as e:
            errors.append(f"Row {line}: {e}")
            continue
        parent_id = None
        if r["parent_tag"]:
            p = q("SELECT id, room_id FROM assets WHERE tag=?", (r["parent_tag"],), one=True)
            if not p:
                errors.append(f"Row {line}: parent_tag {r['parent_tag']} not found (put parents above their parts)")
                continue
            parent_id = p["id"]
        db().execute("INSERT OR IGNORE INTO buildings(name) VALUES (?)", (r["building"],))
        bid = q("SELECT id FROM buildings WHERE name=?", (r["building"],), one=True)["id"]
        db().execute("INSERT OR IGNORE INTO rooms(building_id, name, type) VALUES (?,?, 'Other')", (bid, r["room"]))
        rid = q("SELECT id FROM rooms WHERE building_id=? AND name=?", (bid, r["room"]), one=True)["id"]
        cid = None
        if r["category"]:
            db().execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (r["category"],))
            cid = q("SELECT id FROM categories WHERE name=?", (r["category"],), one=True)["id"]
        if parent_id:
            rid = p["room_id"]  # parts live where their parent lives
        try:
            cur = db().execute(
                """INSERT INTO assets(tag, name, category_id, room_id, parent_id, status, brand, model, serial_no,
                   ip_address, mac_address, purchase_date, warranty_until, cost, vendor, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["tag"], r["name"], cid, rid, parent_id, r["status"] or "Working", r["brand"], r["model"],
                 r["serial_no"], r["ip_address"], r["mac_address"], r["purchase_date"], r["warranty_until"],
                 cost, r["vendor"], r["notes"]))
        except sqlite3.IntegrityError:
            errors.append(f"Row {line}: tag {r['tag']} already exists")
            continue
        if not r["tag"]:
            db().execute("UPDATE assets SET tag=? WHERE id=?", (f"AMS-{cur.lastrowid:05d}", cur.lastrowid))
        log(cur.lastrowid, "Added (CSV import)")
        count += 1
    return count, errors


@app.route("/assets/import", methods=["GET", "POST"])
@role("admin")
def asset_import():
    if request.args.get("template"):
        return csv_response("asset-import-template.csv", IMPORT_COLS, [
            ["CSE-L3-001", "L3-PC-01", "Desktop computer", "CSE Block", "Computer Lab 3", "Working", "Dell", "OptiPlex 7090",
             "SN123", "10.10.3.11", "", "2024-06-01", "2027-06-01", "52000", "Vendor name", "", ""],
            ["", "8 GB RAM", "RAM", "CSE Block", "Computer Lab 3", "Working", "Crucial", "", "", "", "", "", "", "", "", "",
             "CSE-L3-001"]])
    errors = []
    if request.method == "POST":
        fh = request.files.get("file")
        if not fh or not fh.filename:
            errors = ["Choose a CSV file."]
        else:
            try:
                text = fh.read().decode("utf-8-sig")
            except UnicodeDecodeError:
                text = None
                errors = ["File must be UTF-8 CSV (Excel: Save As → CSV UTF-8)."]
            if text is not None:
                count, errors = import_rows(csv.DictReader(io.StringIO(text)))
                if errors:
                    db().rollback()
                elif not count:
                    errors = ["No rows found."]
                else:
                    db().commit()
                    flash(f"Imported {count} assets.", "ok")
                    return redirect(url_for("assets", parts=1))
    return render_template("import.html", errors=errors, cols=IMPORT_COLS)


def asset_values():
    f = request.form
    v = {k: (f.get(k) or "").strip() or None for k in FIELDS}
    if not v["name"]:
        raise ValueError("Name is required")
    if v["status"] not in STATUSES:
        v["status"] = "Working"
    v["category_id"], v["room_id"] = opt_int(v["category_id"]), opt_int(v["room_id"])
    v["cost"] = opt_float(v["cost"], "Cost")
    return v


def pretty(field, val):
    if val is None or val == "":
        return "—"
    if field in ("category_id", "room_id"):
        row = q(f"SELECT name FROM {'categories' if field == 'category_id' else 'rooms'} WHERE id=?", (val,), one=True)
        return row["name"] if row else "—"
    return str(val)


@app.route("/assets/new", methods=["GET", "POST"])
@role("admin", "technician")
def asset_new():
    parent = q("SELECT * FROM assets WHERE id=?", (opt_int(request.values.get("parent")),), one=True)
    if request.method == "POST":
        try:
            v = asset_values()
            if parent:
                v["room_id"] = parent["room_id"]  # parts live where their parent lives
            if not v["room_id"]:
                raise ValueError("Room is required")
            cols = FIELDS + ["parent_id"]
            cur = db().execute(f"INSERT INTO assets({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                               [v[k] for k in FIELDS] + [parent["id"] if parent else None])
            aid = cur.lastrowid
            if not v["tag"]:
                db().execute("UPDATE assets SET tag=? WHERE id=?", (f"AMS-{aid:05d}", aid))
            log(aid, "Added" + (f" as part of {parent['tag']}" if parent else ""))
            if parent:
                log(parent["id"], f"Part added: {v['name']}")
            src = opt_int(request.form.get("copy_parts_from"))
            if src and not parent:
                # duplicate the source's parts (serials/IPs left blank, they differ per unit)
                for p in q("SELECT * FROM assets WHERE parent_id=?", (src,)):
                    pc = db().execute("""INSERT INTO assets(name, category_id, room_id, parent_id, status, brand, model,
                                         purchase_date, warranty_until, cost, vendor) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                                      (p["name"], p["category_id"], v["room_id"], aid, "Working", p["brand"], p["model"],
                                       p["purchase_date"], p["warranty_until"], p["cost"], p["vendor"]))
                    db().execute("UPDATE assets SET tag=? WHERE id=?", (f"AMS-{pc.lastrowid:05d}", pc.lastrowid))
                    log(pc.lastrowid, f"Added as part of new asset (copied)")
            db().commit()
            flash("Asset added.", "ok")
            return redirect(url_for("asset", aid=aid))
        except ValueError as e:
            flash(str(e), "error")
        except sqlite3.IntegrityError:
            flash("That tag is already used by another asset.", "error")
    copy = q("SELECT * FROM assets WHERE id=?", (opt_int(request.args.get("copy")),), one=True)
    if copy:
        preset = {k: copy[k] for k in FIELDS if k not in ("tag", "serial_no", "ip_address", "mac_address", "notes")}
        preset["copy_id"] = copy["id"]
        preset["copy_parts"] = q("SELECT COUNT(*) n FROM assets WHERE parent_id=?", (copy["id"],), one=True)["n"]
        if copy["parent_id"]:
            parent = q("SELECT * FROM assets WHERE id=?", (copy["parent_id"],), one=True)
    else:
        preset = {"room_id": opt_int(request.args.get("room")) or (parent["room_id"] if parent else None)}
    return render_template("asset_form.html", a=None, parent=parent, preset=preset, copy=copy, **lookups())


@app.route("/assets/<int:aid>")
def asset(aid):
    a = q(ASSET_SELECT + " WHERE a.id=?", (aid,), one=True)
    if not a:
        abort(404)
    return render_template(
        "asset.html", a=a,
        parts=q(ASSET_SELECT + " WHERE a.parent_id=? ORDER BY cat", (aid,)),
        issues=q("""SELECT i.*, u.name reporter, ru.name resolver FROM issues i LEFT JOIN users u ON u.id=i.reported_by
                    LEFT JOIN users ru ON ru.id=i.resolved_by WHERE i.asset_id=?
                    ORDER BY i.status='Resolved', i.id DESC""", (aid,)),
        services=q("SELECT * FROM services WHERE asset_id=? ORDER BY date DESC, id DESC", (aid,)),
        photos=[(p["file"], f"Issue: {p['issue']}" if p["issue"] else p["at"][:10]) for p in q(
            "SELECT p.*, i.title issue FROM photos p LEFT JOIN issues i ON i.id=p.issue_id WHERE p.asset_id=? ORDER BY p.id DESC", (aid,))],
        activity=q("""SELECT ac.*, u.name who FROM activity ac LEFT JOIN users u ON u.id=ac.user_id
                      WHERE ac.asset_id=? ORDER BY ac.id DESC LIMIT 30""", (aid,)),
    )


@app.route("/assets/<int:aid>/edit", methods=["GET", "POST"])
@role("admin", "technician")
def asset_edit(aid):
    a = q("SELECT * FROM assets WHERE id=?", (aid,), one=True)
    if not a:
        abort(404)
    if request.method == "POST":
        try:
            v = asset_values()
            v["tag"] = v["tag"] or a["tag"]
            if a["parent_id"]:
                v["room_id"] = a["room_id"]
            if not v["room_id"]:
                raise ValueError("Room is required")
            changes = [f"{LABELS[k]}: {pretty(k, a[k])} → {pretty(k, v[k])}"
                       for k in FIELDS if str(a[k] if a[k] is not None else "") != str(v[k] if v[k] is not None else "")]
            db().execute(f"UPDATE assets SET {','.join(k + '=?' for k in FIELDS)} WHERE id=?", [v[k] for k in FIELDS] + [aid])
            if v["room_id"] != a["room_id"]:
                db().execute("UPDATE assets SET room_id=? WHERE parent_id=?", (v["room_id"], aid))
            if changes:
                log(aid, "; ".join(changes))
            db().commit()
            flash("Saved.", "ok")
            return redirect(url_for("asset", aid=aid))
        except ValueError as e:
            flash(str(e), "error")
        except sqlite3.IntegrityError:
            flash("That tag is already used by another asset.", "error")
    parent = q("SELECT * FROM assets WHERE id=?", (a["parent_id"],), one=True) if a["parent_id"] else None
    return render_template("asset_form.html", a=a, parent=parent, preset={}, **lookups())


@app.route("/assets/<int:aid>/lend", methods=["POST"])
@role("admin", "technician")
def asset_lend(aid):
    a = q("SELECT * FROM assets WHERE id=?", (aid,), one=True)
    if not a:
        abort(404)
    who = request.form.get("assigned_to", "").strip()
    if request.form.get("action") == "return":
        if a["assigned_to"]:
            db().execute("UPDATE assets SET assigned_to=NULL, assigned_due=NULL WHERE id=?", (aid,))
            log(aid, f"Returned by {a['assigned_to']}")
            flash("Marked as returned.", "ok")
    elif not who:
        flash("Enter who it's lent to.", "error")
    else:
        due = request.form.get("assigned_due") or None
        db().execute("UPDATE assets SET assigned_to=?, assigned_due=? WHERE id=?", (who, due, aid))
        log(aid, f"Lent to {who}" + (f", due {due}" if due else ""))
        flash(f"Lent to {who}.", "ok")
    db().commit()
    return redirect(url_for("asset", aid=aid))


@app.route("/loans")
def loans():
    rows = q(ASSET_SELECT + " WHERE a.assigned_to IS NOT NULL ORDER BY a.assigned_due IS NULL, a.assigned_due", ())
    return render_template("loans.html", rows=rows)


@app.route("/assets/<int:aid>/delete", methods=["POST"])
@role("admin")
def asset_delete(aid):
    if q("SELECT 1 FROM assets WHERE parent_id=? LIMIT 1", (aid,), one=True):
        flash("Remove or move its parts first.", "error")
        return redirect(url_for("asset", aid=aid))
    files = [r["file"] for r in q("SELECT file FROM photos WHERE asset_id=?", (aid,))]
    db().execute("DELETE FROM assets WHERE id=?", (aid,))
    db().commit()
    for name in files:
        try:
            os.remove(os.path.join(UPLOADS, name))
        except OSError:
            pass
    flash("Asset deleted.", "ok")
    return redirect(url_for("assets"))


# ---------- issues ----------

@app.route("/assets/<int:aid>/issues", methods=["POST"])
def issue_new(aid):
    a = q("SELECT * FROM assets WHERE id=?", (aid,), one=True)
    if not a:
        abort(404)
    f = request.form
    if not f.get("title", "").strip():
        flash("Describe the issue.", "error")
        return redirect(url_for("asset", aid=aid))
    cur = db().execute("INSERT INTO issues(asset_id, title, type, priority, description, reported_by) VALUES (?,?,?,?,?,?)",
                 (aid, f["title"].strip(), f.get("type") if f.get("type") in ISSUE_TYPES else "Other",
                  f.get("priority") if f.get("priority") in PRIORITIES else "Medium", f.get("description"), g.user["id"]))
    log(aid, f"Issue reported: {f['title'].strip()}")
    if f.get("mark_faulty") and a["status"] == "Working":
        db().execute("UPDATE assets SET status='Faulty' WHERE id=?", (aid,))
        log(aid, "Status: Working → Faulty")
    attach_photos(aid, cur.lastrowid)
    db().commit()
    flash("Issue reported.", "ok")
    return redirect(url_for("asset", aid=aid))


@app.route("/assets/<int:aid>/photos", methods=["POST"])
@role("admin", "technician")
def asset_photos(aid):
    if not q("SELECT 1 FROM assets WHERE id=?", (aid,), one=True):
        abort(404)
    n = attach_photos(aid)
    db().commit()
    if n:
        flash(f"{n} photo(s) added.", "ok")
    return redirect(url_for("asset", aid=aid))


@app.route("/issues/<int:iid>/resolve", methods=["POST"])
@role("admin", "technician")
def issue_resolve(iid):
    i = q("SELECT * FROM issues WHERE id=?", (iid,), one=True)
    if not i:
        abort(404)
    if i["status"] == "Open":
        db().execute("UPDATE issues SET status='Resolved', resolved_by=?, resolved_at=CURRENT_TIMESTAMP, resolution=? WHERE id=?",
                     (g.user["id"], request.form.get("resolution", "").strip(), iid))
        log(i["asset_id"], f"Issue resolved: {i['title']}")
        if request.form.get("mark_working"):
            db().execute("UPDATE assets SET status='Working' WHERE id=?", (i["asset_id"],))
            log(i["asset_id"], "Status → Working")
        db().commit()
        flash("Issue resolved.", "ok")
    back = request.form.get("back", "")
    return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("asset", aid=i["asset_id"]))


@app.route("/issues")
def issues():
    a = request.args
    status = a.get("status", "Open")
    sql = """SELECT i.*, x.tag, x.name asset_name, r.name room, b.name building, u.name reporter
             FROM issues i JOIN assets x ON x.id=i.asset_id LEFT JOIN rooms r ON r.id=x.room_id
             LEFT JOIN buildings b ON b.id=r.building_id LEFT JOIN users u ON u.id=i.reported_by WHERE 1=1"""
    args = []
    if status in ("Open", "Resolved"):
        sql += " AND i.status=?"
        args.append(status)
    if a.get("type") in ISSUE_TYPES:
        sql += " AND i.type=?"
        args.append(a["type"])
    if opt_int(a.get("building")):
        sql += " AND r.building_id=?"
        args.append(int(a["building"]))
    if a.get("q", "").strip():
        sql += " AND (i.title LIKE ? OR i.description LIKE ? OR i.resolution LIKE ? OR x.tag LIKE ? OR x.name LIKE ?)"
        args += [f"%{a['q'].strip()}%"] * 5
    sql += " ORDER BY i.status='Resolved', CASE i.priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, i.id DESC"
    rows, pg = paginate(sql, args)
    return render_template("issues.html", rows=rows, pg=pg, status=status,
                           buildings=q("SELECT * FROM buildings ORDER BY name"))


# ---------- services ----------

@app.route("/assets/<int:aid>/services", methods=["POST"])
@role("admin", "technician")
def service_new(aid):
    a = q("SELECT * FROM assets WHERE id=?", (aid,), one=True)
    if not a:
        abort(404)
    f = request.form
    try:
        cost = opt_float(f.get("cost"), "Cost")
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("asset", aid=aid))
    stype = f.get("type") if f.get("type") in SERVICE_TYPES else "Repair"
    db().execute("""INSERT INTO services(asset_id, date, type, description, done_by, cost, next_due, logged_by)
                    VALUES (?,?,?,?,?,?,?,?)""",
                 (aid, f.get("date") or date.today().isoformat(), stype, f.get("description"),
                  f.get("done_by") or g.user["name"], cost, f.get("next_due") or None, g.user["id"]))
    if f.get("next_due"):
        db().execute("UPDATE assets SET next_service_due=? WHERE id=?", (f["next_due"], aid))
    if f.get("status") in STATUSES and f["status"] != a["status"]:
        db().execute("UPDATE assets SET status=? WHERE id=?", (f["status"], aid))
        log(aid, f"Status: {a['status']} → {f['status']}")
    log(aid, f"Service logged: {stype}")
    db().commit()
    flash("Service logged.", "ok")
    return redirect(url_for("asset", aid=aid))


@app.route("/services")
def services():
    soon = (date.today() + timedelta(days=30)).isoformat()
    due = q(ASSET_SELECT + " WHERE a.next_service_due IS NOT NULL AND a.next_service_due <= ? AND a.status!='Scrapped' ORDER BY a.next_service_due", (soon,))
    term = request.args.get("q", "").strip()
    rows, pg = paginate("""SELECT s.*, a.tag, a.name asset_name, r.name room FROM services s JOIN assets a ON a.id=s.asset_id
                LEFT JOIN rooms r ON r.id=a.room_id"""
                + (" WHERE a.tag LIKE ? OR a.name LIKE ? OR s.description LIKE ? OR s.done_by LIKE ? OR s.type LIKE ?" if term else "")
                + " ORDER BY s.date DESC, s.id DESC", [f"%{term}%"] * 5 if term else [])
    return render_template("services.html", due=due, rows=rows, pg=pg)


# ---------- labels ----------

@app.route("/labels")
def labels():
    rid = opt_int(request.args.get("room"))
    aid = opt_int(request.args.get("asset"))
    if aid:
        rows = q(ASSET_SELECT + " WHERE a.id=?", (aid,))
    elif rid:
        rows = q(ASSET_SELECT + " WHERE a.room_id=? ORDER BY a.tag", (rid,))
    else:
        rows = []
    return render_template("labels.html", rows=rows, rooms=lookups()["rooms"], rid=rid)


# ---------- settings (admin) ----------

@app.route("/settings")
@role("admin")
def settings():
    return render_template(
        "settings.html",
        buildings=q("SELECT b.*, (SELECT COUNT(*) FROM rooms r WHERE r.building_id=b.id) n FROM buildings b ORDER BY name"),
        rooms=q("""SELECT r.*, b.name building, (SELECT COUNT(*) FROM assets a WHERE a.room_id=r.id) n
                   FROM rooms r JOIN buildings b ON b.id=r.building_id ORDER BY b.name, r.name"""),
        categories=q("SELECT c.*, (SELECT COUNT(*) FROM assets a WHERE a.category_id=c.id) n FROM categories c ORDER BY name"),
    )


@app.route("/settings/<kind>/add", methods=["POST"])
@role("admin")
def settings_add(kind):
    f = request.form
    name = f.get("name", "").strip()
    try:
        if not name:
            raise ValueError("Name is required")
        if kind == "buildings":
            db().execute("INSERT INTO buildings(name, code) VALUES (?,?)", (name, f.get("code", "").strip().upper() or None))
        elif kind == "rooms":
            if not opt_int(f.get("building_id")):
                raise ValueError("Pick a building")
            db().execute("INSERT INTO rooms(building_id, name, type, floor) VALUES (?,?,?,?)",
                         (int(f["building_id"]), name, f.get("type") if f.get("type") in ROOM_TYPES else "Other",
                          f.get("floor", "").strip() or None))
        elif kind == "categories":
            db().execute("INSERT INTO categories(name, life_years) VALUES (?,?)", (name, opt_int(f.get("life_years")) or 5))
        else:
            abort(404)
        db().commit()
        flash(f"Added {name}.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    except sqlite3.IntegrityError:
        flash(f"{name} already exists.", "error")
    return redirect(url_for("settings") + "#" + kind)


@app.route("/settings/categories/<int:item>/life", methods=["POST"])
@role("admin")
def category_life(item):
    years = opt_int(request.form.get("life_years"))
    if not years or years > 50:
        flash("Useful life must be 1-50 years.", "error")
    else:
        db().execute("UPDATE categories SET life_years=? WHERE id=?", (years, item))
        db().commit()
        flash("Useful life updated.", "ok")
    return redirect(url_for("settings") + "#categories")


@app.route("/settings/<kind>/<int:item>/delete", methods=["POST"])
@role("admin")
def settings_delete(kind, item):
    in_use = {"buildings": "SELECT 1 FROM rooms WHERE building_id=?",
              "rooms": "SELECT 1 FROM assets WHERE room_id=?",
              "categories": "SELECT 1 FROM assets WHERE category_id=?"}
    if kind not in in_use:
        abort(404)
    if q(in_use[kind] + " LIMIT 1", (item,), one=True):
        flash("Still in use, move or remove what's inside first.", "error")
    else:
        db().execute(f"DELETE FROM {kind} WHERE id=?", (item,))
        db().commit()
        flash("Deleted.", "ok")
    return redirect(url_for("settings") + "#" + kind)


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
        # keep history readable: issues/services/activity just lose the user link
        for t, c in (("issues", "reported_by"), ("issues", "resolved_by"), ("services", "logged_by"), ("activity", "user_id")):
            db().execute(f"UPDATE {t} SET {c}=NULL WHERE {c}=?", (uid,))
        db().execute("DELETE FROM users WHERE id=?", (uid,))
        db().commit()
        flash("User deleted.", "ok")
    return redirect(url_for("users"))


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-Robots-Tag", "noindex")  # internal tool, keep out of search engines
    return resp


@app.errorhandler(500)
def server_error(_):
    db().rollback()
    return render_template("error.html", msg="Something went wrong. Try again, or tell the admin."), 500


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


# ---------- demo data ----------

# demo-only accounts (password demo-<username>-2026), shown as buttons on the login page with --demo
DEMO_USERS = [("admin", "Demo Admin", "admin"), ("tech", "Ravi (Technician)", "technician"),
              ("staff", "Priya (Staff)", "staff")]

def seed_demo():
    with sqlite3.connect(DB) as c:
        if c.execute("SELECT 1 FROM buildings LIMIT 1").fetchone():
            return
        if not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            for u, n, r in DEMO_USERS:
                c.execute("INSERT INTO users(username, name, password_hash, role) VALUES (?,?,?,?)",
                          (u, n, generate_password_hash(f"demo-{u}-2026"), r))
        cat = {n: i for i, n in c.execute("SELECT id, name FROM categories")}
        for n, code in {"Main Block": "MB", "CSE Block": "CSE", "ECE Block": "ECE"}.items():
            c.execute("INSERT INTO buildings(name, code) VALUES (?,?)", (n, code))
        bid = {n: i for i, n in c.execute("SELECT id, name FROM buildings")}
        for b, n, t, fl in [("CSE Block", "Computer Lab 1", "Lab", "1"), ("CSE Block", "Computer Lab 2", "Lab", "1"),
                            ("CSE Block", "Server Room", "Network room", "G"), ("CSE Block", "CS-201", "Classroom", "2"),
                            ("ECE Block", "Electronics Lab", "Lab", "1"), ("ECE Block", "EC-101", "Classroom", "1"),
                            ("Main Block", "Principal Office", "Office", "G"), ("Main Block", "Seminar Hall", "Seminar hall", "1"),
                            ("Main Block", "Library", "Library", "G")]:
            c.execute("INSERT INTO rooms(building_id, name, type, floor) VALUES (?,?,?,?)", (bid[b], n, t, fl))
        rid = {n: i for i, n in c.execute("SELECT id, name FROM rooms")}
        today = date.today()
        aid = {}

        def add(name, category, room, status="Working", parent=None, **kw):
            cols = ["name", "category_id", "room_id", "status", "parent_id"] + list(kw)
            cur = c.execute(f"INSERT INTO assets({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [name, cat[category], rid[room], status, parent] + list(kw.values()))
            c.execute("UPDATE assets SET tag=? WHERE id=?", (f"AMS-{cur.lastrowid:05d}", cur.lastrowid))
            c.execute("INSERT INTO activity(asset_id, text) VALUES (?, 'Added (demo data)')", (cur.lastrowid,))
            aid[(room, name)] = cur.lastrowid
            return cur.lastrowid

        for lab, short in (("Computer Lab 1", "L1"), ("Computer Lab 2", "L2")):
            for n in range(1, 7):
                pc = add(f"{short}-PC-{n:02d}", "Desktop computer", lab,
                         "Faulty" if (short, n) == ("L1", 4) else "Working", brand="Dell", model="OptiPlex 7090",
                         serial_no=f"DL{short}{n:04d}", ip_address=f"10.10.{rid[lab]}.{10 + n}",
                         purchase_date="2023-07-15", cost=52000, vendor="Sri Lakshmi Computers",
                         warranty_until=(today + timedelta(days=40 if n == 1 else 400)).isoformat())
                add("8 GB DDR4 RAM", "RAM", lab, parent=pc, brand="Crucial")
                add("512 GB SSD", "Hard disk / SSD", lab, parent=pc, brand="WD")
                add('Dell 22" Monitor', "Monitor", lab, parent=pc, brand="Dell", model="E2222H")
            add("Projector", "Projector", lab, brand="Epson", model="EB-X51", cost=38000,
                next_service_due=(today + timedelta(days=12)).isoformat())
            add("24-port Switch", "Network switch", lab, brand="TP-Link", model="TL-SG1024", ip_address=f"10.10.{rid[lab]}.2")
        add("Core Router", "Router", "Server Room", brand="Cisco", model="ISR 4331", ip_address="10.10.0.1", cost=145000)
        add("Core Switch", "Network switch", "Server Room", brand="Cisco", model="Catalyst 9200", ip_address="10.10.0.2")
        add("Firewall", "Firewall", "Server Room", brand="Fortinet", model="FortiGate 60F", ip_address="10.10.0.254")
        add("File Server", "Server", "Server Room", brand="HPE", model="ProLiant DL380", cost=320000,
            next_service_due=(today - timedelta(days=3)).isoformat())
        add("10 kVA UPS", "UPS", "Server Room", "Under service", brand="APC", cost=95000)
        add("Classroom Projector", "Projector", "CS-201", brand="BenQ")
        add("Wi-Fi AP", "Wi-Fi access point", "CS-201", brand="Ubiquiti", model="U6 Lite", ip_address="10.10.20.5")
        add("Digital Storage Oscilloscope", "Lab instrument", "Electronics Lab", brand="Tektronix", model="TBS1052C", cost=48000)
        add("Function Generator", "Lab instrument", "Electronics Lab", brand="Keysight", cost=30000)
        add("Smart Board", "Smart board", "EC-101", brand="Samsung", model="Flip 3")
        add("Office Laptop", "Laptop", "Principal Office", brand="Lenovo", model="ThinkPad E14")
        add("Laser Printer", "Printer", "Principal Office", "Faulty", brand="HP", model="LaserJet M1136")
        add("PA System", "Other electronics", "Seminar Hall", brand="Ahuja")
        add("Kiosk PC", "Desktop computer", "Library", brand="Acer")
        add("Wi-Fi AP", "Wi-Fi access point", "Library", brand="Ubiquiti", model="U6 Lite")
        for key, title, typ, pri in [(("Computer Lab 1", "L1-PC-04"), "System not booting", "Hardware", "High"),
                                     (("Principal Office", "Laser Printer"), "Paper jam on every print", "Hardware", "Medium"),
                                     (("Library", "Wi-Fi AP"), "Network weak at back of library", "Network", "Medium"),
                                     (("Computer Lab 2", "L2-PC-03"), "MATLAB licence error", "Software", "Low")]:
            c.execute("INSERT INTO issues(asset_id, title, type, priority) VALUES (?,?,?,?)", (aid[key], title, typ, pri))
        c.execute("UPDATE assets SET last_audit=? WHERE room_id=?", ((today - timedelta(days=40)).isoformat(), rid["Computer Lab 2"]))
        c.execute("UPDATE assets SET assigned_to='Dr. Meena (ECE)', assigned_due=? WHERE id=?",
                  ((today - timedelta(days=2)).isoformat(), aid[("CS-201", "Classroom Projector")]))
        c.execute("INSERT INTO services(asset_id, date, type, description, done_by, cost) VALUES (?,?,?,?,?,?)",
                  (aid[("Server Room", "10 kVA UPS")], today.isoformat(), "Repair",
                   "Battery bank replacement in progress", "APC service", 42000))


init_db()
if os.environ.get("DEMO"):  # hosted demo (render.yaml): same as --demo
    seed_demo()
    app.config["DEMO"] = True
if __name__ == "__main__":
    if "--demo" in sys.argv:
        seed_demo()
        app.config["DEMO"] = True
    # --lan: reachable from other campus PCs; --debug: auto-reload + debugger (never together on a shared network)
    app.run(host="0.0.0.0" if "--lan" in sys.argv else "127.0.0.1", debug="--debug" in sys.argv)
