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
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, send_from_directory, session, url_for
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "ams.db")
KEY_FILE = os.path.join(HERE, "secret.key")

TAG_PREFIX = "AMS"  # permanent tags look like AMS-00042; the institution's final format is still to be agreed
ROLES = ["super_admin", "admin", "system_admin", "purchase", "hod", "lab_assistant", "custodian", "auditor", "technician"]
ROLE_LABEL = {"super_admin": "Super admin", "admin": "Admin", "system_admin": "System admin", "purchase": "Purchase",
              "hod": "HOD", "lab_assistant": "Lab assistant", "custodian": "Custodian / end user", "auditor": "Auditor (read only)",
              "technician": "IT technician"}
# a role sees every department's assets, or only its own department(s)
GLOBAL_ROLES = ("super_admin", "admin", "system_admin", "purchase", "auditor", "technician")
# what each kind of action needs. Templates and routes ask for the action ("edit"), not for a role name.
CAPS = {"edit": ("super_admin", "admin", "technician"),                      # add/edit asset records, service, resolve issues
        "move": ("super_admin", "admin", "technician", "lab_assistant", "hod"),  # issue / return / transfer, stock checks
        "approve": ("super_admin", "admin", "hod"),                           # approve a retirement
        "admin": ("super_admin", "admin"),                                    # masters, imports, tags, archive
        "purchase": ("super_admin", "admin", "purchase"),                     # purchase references and documents
        "users": ("super_admin", "admin"),                                    # accounts: an admin manages roles below admin; only a super admin touches admin-level ones
        "docs": ("super_admin", "admin", "purchase", "technician", "hod", "auditor", "system_admin")}  # may open purchase documents
STATUSES = ["Working", "Faulty", "Under service", "In store", "Scrapped", "Lost"]  # derived from stage + condition, kept for filters
LIFECYCLE = ["Requested", "Ordered", "Received (pending inspection)", "In stock", "Deployed", "Under maintenance", "Retirement proposed",
             "Retirement approved", "Awaiting disposal", "Disposed", "Lost / missing", "Returned to vendor"]
# where an asset may go next. Retirement needs a proposal, a different person to approve it, then evidence of disposal.
LIFECYCLE_NEXT = {
    "Requested": ["Ordered"], "Ordered": ["Received (pending inspection)"],
    "Received (pending inspection)": ["In stock", "Returned to vendor"],
    "In stock": ["Deployed", "Under maintenance", "Retirement proposed", "Lost / missing"],
    "Deployed": ["In stock", "Under maintenance", "Retirement proposed", "Lost / missing"],
    "Under maintenance": ["In stock", "Deployed", "Retirement proposed", "Returned to vendor"],
    "Retirement proposed": ["Retirement approved", "In stock", "Deployed"],
    "Retirement approved": ["Awaiting disposal"], "Awaiting disposal": ["Disposed"],
    "Lost / missing": ["In stock", "Deployed", "Disposed"], "Disposed": [], "Returned to vendor": []}
CONDITIONS = ["Working", "Faulty", "Obsolete", "Unknown"]
VERIFICATION = ["Verified", "Not found", "Wrong location", "Unresolved"]
ACQ_BASIS = ["Purchase", "Donation", "Transfer from another unit", "Lease / rent", "Opening stock (old record)", "Unknown"]
COST_BASIS = ["Per unit", "Line total", "Whole purchase", "Unknown"]
ID_KINDS = {"legacy_code": "Old asset code", "external": "Other system ID", "previous_tag": "Previous tag"}
HOLDERS = ["Stockroom", "Department", "Person"]
ROOM_TYPES = ["Lab", "Classroom", "Office", "Network room", "Library", "Seminar hall", "Other"]
ISSUE_TYPES = ["Hardware", "Network", "Software", "Power", "Other"]
PRIORITIES = ["Low", "Medium", "High"]
SERVICE_TYPES = ["Repair", "Preventive maintenance", "Upgrade", "Part replacement", "Inspection"]
DEFAULT_DEPARTMENTS = ["CSE", "CSE (AIML)", "CSE (Cyber Security)", "IT", "ECE", "EEE", "Mechanical", "Mechatronics", "Aeronautical", "Automobile",
                       "Civil", "Chemical", "Biotechnology", "Biomedical", "Food Technology", "Architecture", "Management Studies",
                       "Mathematics", "Science & Humanities", "Exam Cell", "Administration", "Library", "Central IT"]
DEFAULT_CATEGORIES = [
    "Desktop computer", "Laptop", "Monitor", "Keyboard", "Mouse", "RAM", "Hard disk / SSD",
    "Motherboard", "Power supply", "Printer", "Scanner", "Projector", "Smart board",
    "Network switch", "Router", "Wi-Fi access point", "Server", "Firewall", "UPS",
    "CCTV camera", "Air conditioner", "Lab instrument", "Other electronics",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  password_hash TEXT NOT NULL, role TEXT NOT NULL, extra_roles TEXT, department_id INTEGER);
CREATE TABLE IF NOT EXISTS departments (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, code TEXT);
CREATE TABLE IF NOT EXISTS user_scopes (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE, PRIMARY KEY (user_id, department_id));
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
CREATE TABLE IF NOT EXISTS asset_ids (
  id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, value TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
  added_at TEXT DEFAULT CURRENT_TIMESTAMP, added_by INTEGER REFERENCES users(id), UNIQUE (kind, value, source));
CREATE TABLE IF NOT EXISTS custody (
  id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  holder_type TEXT NOT NULL, department_id INTEGER REFERENCES departments(id), person TEXT, room_id INTEGER REFERENCES rooms(id),
  due_date TEXT, started_on TEXT NOT NULL, started_at TEXT DEFAULT CURRENT_TIMESTAMP, started_by INTEGER REFERENCES users(id),
  reason TEXT, approval_ref TEXT, ack_by TEXT, ack_at TEXT,
  ended_on TEXT, ended_at TEXT, ended_by INTEGER REFERENCES users(id), end_reason TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_custody ON custody(asset_id) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
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
CREATE TABLE IF NOT EXISTS import_batches (
  id INTEGER PRIMARY KEY, filename TEXT NOT NULL, stored_file TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER,
  uploaded_by INTEGER REFERENCES users(id), uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP, sheet TEXT, header_row INTEGER DEFAULT 1,
  settings_json TEXT, status TEXT NOT NULL DEFAULT 'Uploaded', total_rows INTEGER, created_n INTEGER, skipped_n INTEGER, error_n INTEGER,
  approved_by INTEGER REFERENCES users(id), approved_at TEXT, reversed_at TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS staged_rows (
  id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE, row_no INTEGER,
  raw_json TEXT, data_json TEXT, kind TEXT, errors_json TEXT, warnings_json TEXT, dup_asset_id INTEGER,
  decision TEXT DEFAULT 'Create', asset_id INTEGER, purchase_batch_id INTEGER);
CREATE TABLE IF NOT EXISTS import_mappings (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, signature TEXT NOT NULL, columns_json TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id), created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS value_maps (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL, UNIQUE (kind, source));
CREATE TABLE IF NOT EXISTS purchase_batches (
  id INTEGER PRIMARY KEY, import_batch_id INTEGER REFERENCES import_batches(id), description TEXT NOT NULL,
  category_id INTEGER REFERENCES categories(id), department_id INTEGER REFERENCES departments(id), quantity INTEGER NOT NULL,
  unit_cost REAL, total_cost REAL, cost_basis TEXT, tax_included INTEGER, vendor TEXT, po_number TEXT, invoice_number TEXT,
  purchase_date TEXT, status TEXT NOT NULL DEFAULT 'Unconfirmed', units_created INTEGER DEFAULT 0, notes TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY, file TEXT NOT NULL, original_name TEXT NOT NULL, ext TEXT NOT NULL, size INTEGER, sha256 TEXT NOT NULL,
  kind TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, supersedes_id INTEGER REFERENCES documents(id), description TEXT,
  reference TEXT, uploaded_by INTEGER REFERENCES users(id), uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS document_links (
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE, asset_id INTEGER REFERENCES assets(id) ON DELETE CASCADE,
  purchase_batch_id INTEGER REFERENCES purchase_batches(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS attribute_defs (
  id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE, name TEXT NOT NULL,
  datatype TEXT NOT NULL, unit TEXT, choices TEXT, required INTEGER DEFAULT 0, sort INTEGER DEFAULT 0, UNIQUE (category_id, name));
CREATE TABLE IF NOT EXISTS attribute_values (
  asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE, attr_id INTEGER NOT NULL REFERENCES attribute_defs(id) ON DELETE CASCADE,
  value TEXT, PRIMARY KEY (asset_id, attr_id));
CREATE TABLE IF NOT EXISTS stock_items (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, unit TEXT NOT NULL DEFAULT 'nos', department_id INTEGER REFERENCES departments(id),
  room_id INTEGER REFERENCES rooms(id), min_level REAL DEFAULT 0, notes TEXT, archived_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS stock_movements (
  id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES stock_items(id) ON DELETE CASCADE, qty REAL NOT NULL, kind TEXT NOT NULL,
  event_date TEXT NOT NULL, entered_at TEXT DEFAULT CURRENT_TIMESTAMP, user_id INTEGER REFERENCES users(id),
  department_id INTEGER REFERENCES departments(id), reason TEXT, reference TEXT);
CREATE TABLE IF NOT EXISTS licences (
  id INTEGER PRIMARY KEY, product TEXT NOT NULL, vendor TEXT, licence_type TEXT, metric TEXT, quantity INTEGER NOT NULL DEFAULT 1,
  in_use INTEGER, start_date TEXT, renewal_date TEXT, support_end TEXT, key_ref TEXT, cost REAL, po_number TEXT,
  department_id INTEGER REFERENCES departments(id), notes TEXT, archived_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS check_findings (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, room_id INTEGER REFERENCES rooms(id), asset_id INTEGER REFERENCES assets(id) ON DELETE CASCADE,
  description TEXT, found_on TEXT NOT NULL, found_by INTEGER REFERENCES users(id), status TEXT NOT NULL DEFAULT 'Open',
  resolution TEXT, resolved_by INTEGER REFERENCES users(id), resolved_at TEXT);
CREATE TABLE IF NOT EXISTS contracts (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, vendor TEXT, reference TEXT, start_date TEXT, end_date TEXT, cost REAL,
  department_id INTEGER REFERENCES departments(id), notes TEXT, archived_at TEXT, created_by INTEGER REFERENCES users(id),
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS contract_assets (
  contract_id INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE, asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  PRIMARY KEY (contract_id, asset_id));
CREATE TABLE IF NOT EXISTS asset_interfaces (
  id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE, name TEXT NOT NULL, mac TEXT, ip TEXT,
  vlan TEXT, connected_to TEXT, notes TEXT, added_by INTEGER REFERENCES users(id), added_at TEXT DEFAULT CURRENT_TIMESTAMP);
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
    "assets": [("last_audit", "TEXT"), ("assigned_to", "TEXT"), ("assigned_due", "TEXT"),
               ("lifecycle_stage", "TEXT"), ("condition", "TEXT DEFAULT 'Working'"), ("verification", "TEXT"),
               ("managing_dept_id", "INTEGER REFERENCES departments(id)"), ("owning_dept_id", "INTEGER REFERENCES departments(id)"),
               ("using_dept_id", "INTEGER REFERENCES departments(id)"), ("custodian", "TEXT"), ("location_unresolved", "INTEGER DEFAULT 0"),
               ("acquisition_basis", "TEXT"), ("po_number", "TEXT"), ("invoice_number", "TEXT"), ("indent_no", "TEXT"), ("cpr_no", "TEXT"),
               ("grn_no", "TEXT"), ("stock_register_ref", "TEXT"), ("cost_basis", "TEXT"), ("tax_included", "INTEGER"),
               ("legacy_code", "TEXT"), ("archived_at", "TEXT"), ("archive_reason", "TEXT"), ("import_batch_id", "INTEGER"),
               ("source_row", "INTEGER"), ("source_json", "TEXT"), ("rack", "TEXT")],
    "activity": [("event_type", "TEXT"), ("event_date", "TEXT"), ("reason", "TEXT"), ("approval_ref", "TEXT")],
    "users": [("extra_roles", "TEXT"), ("department_id", "INTEGER")],
    "buildings": [("campus", "TEXT")],
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
        if "CHECK" in c.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0]:
            # the first versions allowed three roles only; SQLite can't drop a CHECK, so rebuild the table
            c.executescript("""PRAGMA foreign_keys=OFF;
                CREATE TABLE users_new (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                  password_hash TEXT NOT NULL, role TEXT NOT NULL, extra_roles TEXT, department_id INTEGER);
                INSERT INTO users_new SELECT id, username, name, password_hash, CASE role WHEN 'staff' THEN 'custodian' ELSE role END,
                  extra_roles, department_id FROM users;
                DROP TABLE users; ALTER TABLE users_new RENAME TO users; PRAGMA foreign_keys=ON;""")
        c.execute("""UPDATE users SET role='super_admin' WHERE role='admin' AND id=(SELECT MIN(id) FROM users WHERE role='admin')
                     AND NOT EXISTS (SELECT 1 FROM users WHERE role='super_admin')""")
        # one-time: split the old single status into lifecycle stage + working condition
        c.execute("""UPDATE assets SET condition=CASE status WHEN 'Faulty' THEN 'Faulty' WHEN 'Under service' THEN 'Faulty'
                     WHEN 'Scrapped' THEN 'Obsolete' ELSE 'Working' END WHERE lifecycle_stage IS NULL""")
        c.execute("""UPDATE assets SET lifecycle_stage=CASE status WHEN 'Scrapped' THEN 'Disposed' WHEN 'In store' THEN 'In stock'
                     WHEN 'Under service' THEN 'Under maintenance' ELSE 'Deployed' END WHERE lifecycle_stage IS NULL""")
        c.execute("UPDATE assets SET verification='Verified' WHERE last_audit IS NOT NULL AND verification IS NULL")
        c.execute("UPDATE assets SET acquisition_basis='Unknown' WHERE acquisition_basis IS NULL")
        c.execute("INSERT OR IGNORE INTO counters(name, value) VALUES ('tag', (SELECT COALESCE(MAX(id), 0) FROM assets))")
        c.executemany("INSERT OR IGNORE INTO departments(name) VALUES (?)", [(n,) for n in DEFAULT_DEPARTMENTS])
        c.executemany("INSERT OR IGNORE INTO categories(name) VALUES (?)", [(n,) for n in DEFAULT_CATEGORIES])
        import ams_extras  # the starting specification fields per kind of asset
        ams_extras.seed_attributes(c)
        # lending used two plain columns; it is now a custody record like every other hand-over
        for a in c.execute("SELECT id, assigned_to, assigned_due FROM assets WHERE assigned_to IS NOT NULL").fetchall():
            if not c.execute("SELECT 1 FROM custody WHERE asset_id=?", (a[0],)).fetchone():
                c.execute("INSERT INTO custody(asset_id, holder_type, person, due_date, started_on, reason) VALUES (?,?,?,?,date('now'),'Lent out (moved from the old lending list)')",
                          (a[0], "Person", a[1], a[2]))
        c.execute("UPDATE assets SET assigned_to=NULL, assigned_due=NULL WHERE assigned_to IS NOT NULL")


def derive_status(a):
    """The one-word status other pages filter on, worked out from stage, condition and archive state."""
    if a["archived_at"] or a["lifecycle_stage"] in ("Disposed", "Returned to vendor"):
        return "Scrapped"
    if a["lifecycle_stage"] == "Lost / missing":
        return "Lost"
    if a["lifecycle_stage"] == "Under maintenance":
        return "Under service"
    if a["condition"] == "Faulty":
        return "Faulty"
    if a["lifecycle_stage"] in ("Requested", "Ordered", "Received (pending inspection)", "In stock"):
        return "In store"
    return "Working"


def sync_status(aid):
    a = q("SELECT * FROM assets WHERE id=?", (aid,), one=True)
    db().execute("UPDATE assets SET status=? WHERE id=?", (derive_status(a), aid))


def next_tag():
    """A permanent tag from a counter that only goes up, so a tag is never handed out twice (even after an archive or a correction).
    The UPDATE takes the database write lock first, so two people adding assets at once get different numbers."""
    while True:
        db().execute("UPDATE counters SET value=value+1 WHERE name='tag'")
        n = q("SELECT value FROM counters WHERE name='tag'", one=True)["value"]
        tag = f"{TAG_PREFIX}-{n:05d}"
        if not q("SELECT 1 FROM assets WHERE tag=? UNION SELECT 1 FROM asset_ids WHERE kind='previous_tag' AND value=?", (tag, tag), one=True):
            return tag


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


def log(asset_id, text, etype=None, event_date=None, reason=None, approval=None):
    """Add to an asset's history. event_date is when it really happened; the entry time is recorded automatically."""
    db().execute("INSERT INTO activity(asset_id, user_id, text, event_type, event_date, reason, approval_ref) VALUES (?,?,?,?,?,?,?)",
                 (asset_id, g.user["id"] if g.get("user") else None, text, etype, event_date or None, (reason or "").strip() or None,
                  (approval or "").strip() or None))


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
    if row := q("SELECT asset_id FROM photos WHERE file=?", (name,), one=True):
        get_asset(row["asset_id"])  # 404 unless this user may see that asset
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
API_POST = set()  # bearer-token endpoints that take a JSON body instead of a form, so no CSRF token


@app.before_request
def guard():
    if request.endpoint is None:
        return
    token = session.get("csrf")
    if request.method == "POST" and request.endpoint not in API_POST and (not token or not secrets.compare_digest(request.form.get("csrf", ""), token)):
        abort(400, "Form expired, go back and try again.")
    if request.endpoint != "static" and not q("SELECT 1 FROM users LIMIT 1", one=True):
        return None if request.endpoint == "setup" else redirect(url_for("setup"))
    if request.endpoint in PUBLIC:
        return
    g.user = q("SELECT * FROM users WHERE id=?", (session.get("uid"),), one=True)
    if not g.user:
        return redirect(url_for("login", next=request.full_path))
    if is_limited() and request.endpoint in LIMITED_BLOCKED:
        abort(403)


# a custodian (end user) works with their own assets and issues; the rest of the menu is for people who manage equipment
LIMITED_BLOCKED = {"services", "register", "labels", "reminders", "stock", "stock_item", "reports"}


def is_limited():
    return bool(g.get("user")) and roles_of(g.user) == {"custodian"}


def roles_of(u):
    return {u["role"], *filter(None, (u["extra_roles"] or "").split(","))}


def has_role(*names):
    return bool(g.get("user")) and bool(roles_of(g.user) & set(names))


def allowed(cap):
    """cap is an action from CAPS ('edit', 'move', 'admin'...) or a plain role name."""
    return has_role(*CAPS.get(cap, (cap,)))


def role(*caps):
    """Route guard. Checked on the server, so a hidden button is not the only protection."""
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if not any(allowed(c) for c in caps):
                abort(403)
            return f(*a, **kw)
        return wrapper
    return deco


def dept_ids():
    """None = every department. Otherwise the departments this user belongs to (plus any extra ones an admin gave them)."""
    u = g.user
    if roles_of(u) & set(GLOBAL_ROLES):
        return None
    ids = {r["department_id"] for r in q("SELECT department_id FROM user_scopes WHERE user_id=?", (u["id"],))}
    if u["department_id"]:
        ids.add(u["department_id"])
    return ids


def scope_sql(alias="a"):
    """SQL to append to an asset query so a department user only gets their own department's assets."""
    ids = dept_ids()
    if ids is None:
        return "", []
    if not ids:
        return " AND 0", []
    ph = ",".join("?" * len(ids))
    return (f" AND ({alias}.managing_dept_id IN ({ph}) OR {alias}.owning_dept_id IN ({ph}) OR {alias}.using_dept_id IN ({ph}))"), [*ids] * 3


def get_asset(aid, select=None):
    """One asset, or a 404 if it doesn't exist or isn't in this user's department."""
    where, args = scope_sql()
    a = q((select or ASSET_SELECT) + " WHERE a.id=?" + where, [aid, *args], one=True)
    if not a:
        abort(404)
    return a


@app.context_processor
def template_globals():
    token = session.setdefault("csrf", secrets.token_hex(16))
    user = g.get("user")
    return {
        "other_url": os.environ.get("OTHER_APP_URL"),
        "user": user, "limited": is_limited(),
        "csrf": Markup(f'<input type="hidden" name="csrf" value="{token}">'),
        "can": lambda *caps: any(allowed(c) for c in caps),
        "LIFECYCLE": LIFECYCLE, "CONDITIONS": CONDITIONS, "VERIFICATION": VERIFICATION, "ACQ_BASIS": ACQ_BASIS, "COST_BASIS": COST_BASIS,
        "ID_KINDS": ID_KINDS, "HOLDERS": HOLDERS, "ROLE_LABEL": ROLE_LABEL, "PRIVILEGED": PRIVILEGED,
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
            db().execute("INSERT INTO users(username, name, password_hash, role) VALUES (?,?,?, 'super_admin')",
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
    return render_template("login.html", demo=[(u, n, ROLE_LABEL[r]) for u, n, r, _ in DEMO_USERS] if app.config.get("DEMO") else [])


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
    sw, sa = scope_sql()  # a department user's numbers cover their own department only
    live = "a.status != 'Scrapped'"

    def n(sql, *args):
        return q(sql, [*args, *sa], one=True)["n"]

    audit_cut = (today - timedelta(days=AUDIT_DAYS)).isoformat()
    stats = {
        "total": n(f"SELECT COUNT(*) n FROM assets a WHERE {live}{sw}"),
        "working": n(f"SELECT COUNT(*) n FROM assets a WHERE a.status='Working'{sw}"),
        "down": n(f"SELECT COUNT(*) n FROM assets a WHERE a.status IN ('Faulty','Under service'){sw}"),
        "open_issues": n(f"SELECT COUNT(*) n FROM issues i JOIN assets a ON a.id=i.asset_id WHERE i.status='Open'{sw}"),
        "service_due": n(f"SELECT COUNT(*) n FROM assets a WHERE {live} AND a.next_service_due <= ?{sw}", (today + timedelta(days=30)).isoformat()),
        "warranty": n(f"SELECT COUNT(*) n FROM assets a WHERE {live} AND a.warranty_until BETWEEN ? AND ?{sw}",
                      today.isoformat(), (today + timedelta(days=60)).isoformat()),
        "audit_due": n(f"SELECT COUNT(*) n FROM assets a WHERE {live} AND a.parent_id IS NULL AND (a.last_audit IS NULL OR a.last_audit < ?){sw}", audit_cut),
        "loans": n(f"SELECT COUNT(*) n FROM custody cu JOIN assets a ON a.id=cu.asset_id WHERE cu.ended_at IS NULL AND cu.holder_type='Person'{sw}"),
        "loans_overdue": n(f"SELECT COUNT(*) n FROM custody cu JOIN assets a ON a.id=cu.asset_id WHERE cu.ended_at IS NULL AND cu.holder_type='Person' AND cu.due_date < ?{sw}", today.isoformat()),
        "retiring": n(f"SELECT COUNT(*) n FROM assets a WHERE a.lifecycle_stage IN ('Retirement proposed','Retirement approved','Awaiting disposal'){sw}"),
        "no_location": n(f"SELECT COUNT(*) n FROM assets a WHERE {live} AND (a.room_id IS NULL OR a.location_unresolved=1){sw}"),
        "no_dept": n(f"SELECT COUNT(*) n FROM assets a WHERE {live} AND a.managing_dept_id IS NULL{sw}"),
    }
    vals = q(f"""SELECT a.cost, a.purchase_date, c.life_years FROM assets a LEFT JOIN categories c ON c.id=a.category_id
                 WHERE {live} AND a.cost IS NOT NULL{sw}""", sa)
    stats["value"] = sum(r["cost"] for r in vals)
    stats["book"] = sum(book_value(r["cost"], r["purchase_date"], r["life_years"]) for r in vals)
    by_cat = bars(q(f"""SELECT c.name k, COUNT(*) n FROM assets a JOIN categories c ON c.id=a.category_id
                        WHERE {live} AND a.parent_id IS NULL{sw} GROUP BY c.id ORDER BY n DESC LIMIT 8""", sa))
    by_building = bars(q(f"""SELECT b.name k, COUNT(*) n FROM assets a JOIN rooms r ON r.id=a.room_id
                             JOIN buildings b ON b.id=r.building_id WHERE {live} AND a.parent_id IS NULL{sw}
                             GROUP BY b.id ORDER BY n DESC""", sa))
    by_dept = bars(q(f"""SELECT d.name k, COUNT(*) n FROM assets a JOIN departments d ON d.id=a.managing_dept_id
                         WHERE {live} AND a.parent_id IS NULL{sw} GROUP BY d.id ORDER BY n DESC LIMIT 8""", sa))
    by_status = q(f"SELECT a.status k, COUNT(*) n FROM assets a WHERE 1=1{sw} GROUP BY a.status", sa)
    open_issues = q(f"""SELECT i.*, a.tag, a.name asset_name, r.name room FROM issues i
                       JOIN assets a ON a.id=i.asset_id LEFT JOIN rooms r ON r.id=a.room_id
                       WHERE i.status='Open'{sw}
                       ORDER BY CASE i.priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, i.id
                       LIMIT 8""", sa)
    recent = q(f"""SELECT ac.*, a.tag, u.name who FROM activity ac JOIN assets a ON a.id=ac.asset_id
                  LEFT JOIN users u ON u.id=ac.user_id WHERE 1=1{sw} ORDER BY ac.id DESC LIMIT 10""", sa)
    return render_template("dashboard.html", stats=stats, by_cat=by_cat, by_building=by_building, by_dept=by_dept,
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
    sw, sa = scope_sql()
    assets = q(ASSET_SELECT + " WHERE a.room_id=? AND a.archived_at IS NULL" + sw + " ORDER BY cat, a.name", [rid, *sa])
    parts, groups = {}, {}
    for a in assets:
        if a["parent_id"]:
            parts.setdefault(a["parent_id"], []).append(a)
        else:
            groups.setdefault(a["cat"] or "Uncategorised", []).append(a)
    return render_template("room.html", room=rm, groups=groups, parts=parts,
                           total=sum(len(v) for v in groups.values()),
                           audit=request.args.get("audit") and allowed("move"),
                           audit_cutoff=(date.today() - timedelta(days=AUDIT_DAYS)).isoformat())


@app.route("/rooms/<int:rid>/audit", methods=["POST"])
@role("move")
def room_audit(rid):
    found = {int(x) for x in request.form.getlist("found") if x.isdigit()}
    sw, sa = scope_sql("assets")
    in_room = {r["id"]: r for r in q("SELECT id, tag, name, status FROM assets WHERE room_id=? AND status!='Scrapped'" + sw, [rid, *sa])}
    today = date.today().isoformat()
    for aid in found & in_room.keys():
        db().execute("UPDATE assets SET last_audit=?, verification='Verified' WHERE id=?", (today, aid))
        log(aid, "Verified in stock check", etype="Verified", event_date=today)
    missing = [a for aid, a in in_room.items() if aid not in found]
    for a in missing:
        db().execute("UPDATE assets SET verification='Not found' WHERE id=?", (a["id"],))
        log(a["id"], "Not found in stock check", etype="Not found", event_date=today)
    others = record_findings(rid, request.form.get("extra", ""), today)  # equipment seen here that the list doesn't have
    db().commit()
    msg = f"Stock check saved: {len(found & in_room.keys())} verified"
    flash(msg + (f", {len(missing)} not found (see each asset's activity)" if missing else "")
          + (f", {others} other find(s) waiting for a decision under Stock check findings" if others else "") + ".", "error" if (missing or others) else "ok")
    return redirect(url_for("room", rid=rid))


@app.route("/register")
def register():
    """Printable stock register (dead stock register) by building/room."""
    where, args = " WHERE a.status!='Scrapped' AND a.archived_at IS NULL" + scope_sql()[0], list(scope_sql()[1])
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
  md.name managing_dept, od.name owning_dept, ud.name using_dept,
  (SELECT COUNT(*) FROM issues i WHERE i.asset_id=a.id AND i.status='Open') open_issues,
  (SELECT COUNT(*) FROM assets ch WHERE ch.parent_id=a.id) part_count
FROM assets a LEFT JOIN categories c ON c.id=a.category_id LEFT JOIN rooms r ON r.id=a.room_id
LEFT JOIN buildings b ON b.id=r.building_id LEFT JOIN assets p ON p.id=a.parent_id
LEFT JOIN departments md ON md.id=a.managing_dept_id LEFT JOIN departments od ON od.id=a.owning_dept_id
LEFT JOIN departments ud ON ud.id=a.using_dept_id"""

FIELDS = ["tag", "name", "category_id", "room_id", "brand", "model", "serial_no", "ip_address", "mac_address",
          "purchase_date", "warranty_until", "cost", "vendor", "notes", "managing_dept_id", "owning_dept_id", "condition",
          "legacy_code", "rack", "location_unresolved", "acquisition_basis", "po_number", "invoice_number", "indent_no", "cpr_no",
          "grn_no", "stock_register_ref", "cost_basis", "tax_included"]
LABELS = {"tag": "Tag", "name": "Name", "category_id": "Category", "room_id": "Room", "brand": "Brand", "model": "Model",
          "serial_no": "Serial no", "ip_address": "IP", "mac_address": "MAC", "purchase_date": "Purchase date",
          "warranty_until": "Warranty", "cost": "Cost", "vendor": "Vendor", "notes": "Notes", "managing_dept_id": "Managing dept",
          "owning_dept_id": "Owning dept", "condition": "Condition", "legacy_code": "Old asset code", "rack": "Rack / position",
          "location_unresolved": "Location unresolved", "acquisition_basis": "Acquired by", "po_number": "PO no",
          "invoice_number": "Invoice no", "indent_no": "Indent no", "cpr_no": "CPR no (payment receipt)", "grn_no": "GRN no",
          "stock_register_ref": "Stock register ref", "cost_basis": "Cost is", "tax_included": "Tax included"}


def lookups():
    return {
        "categories": q("SELECT * FROM categories ORDER BY name"),
        "buildings": q("SELECT * FROM buildings ORDER BY name"),
        "rooms": q("SELECT r.*, b.name building FROM rooms r JOIN buildings b ON b.id=r.building_id ORDER BY b.name, r.name"),
        "departments": q("SELECT * FROM departments ORDER BY name"),
    }


def asset_filters():
    a = request.args
    where, args = scope_sql()
    sql, args = " WHERE 1=1" + where, list(args)
    term = a.get("q", "").strip()
    if term:
        sql += """ AND (a.tag LIKE ? OR a.name LIKE ? OR a.serial_no LIKE ? OR a.brand LIKE ? OR a.model LIKE ? OR a.ip_address LIKE ?
                   OR a.legacy_code LIKE ? OR a.custodian LIKE ? OR a.po_number LIKE ? OR a.invoice_number LIKE ?
                   OR EXISTS (SELECT 1 FROM asset_ids i WHERE i.asset_id=a.id AND i.value LIKE ?))"""
        args += [f"%{term}%"] * 11
    for key, col in (("category", "a.category_id"), ("room", "a.room_id"), ("building", "r.building_id"), ("dept", "a.managing_dept_id")):
        if opt_int(a.get(key)):
            sql += f" AND {col}=?"
            args.append(int(a[key]))
    if a.get("status") in STATUSES:
        sql += " AND a.status=?"
        args.append(a["status"])
    for key, col, opts in (("stage", "a.lifecycle_stage", LIFECYCLE), ("condition", "a.condition", CONDITIONS), ("verification", "a.verification", VERIFICATION)):
        if a.get(key) in opts:
            sql += f" AND {col}=?"
            args.append(a[key])
    if a.get("unverified"):
        sql += " AND a.parent_id IS NULL AND (a.last_audit IS NULL OR a.last_audit < ?)"
        args.append((date.today() - timedelta(days=AUDIT_DAYS)).isoformat())
    if not a.get("parts"):
        sql += " AND a.parent_id IS NULL"
    if not a.get("archived"):
        sql += " AND a.archived_at IS NULL"
    return sql, args


@app.route("/assets")
def assets():
    where, args = asset_filters()
    order = " ORDER BY a.id DESC"
    if request.args.get("export") == "csv":
        rows = q(ASSET_SELECT + where + order, args)
        # same columns as import, so an export can be edited and re-imported elsewhere
        src = {"category": "cat", "department": "managing_dept"}
        return csv_response(f"assets-{date.today()}.csv", IMPORT_COLS,
                            [[r[src.get(c, c)] for c in IMPORT_COLS] for r in rows])
    rows, pg = paginate(ASSET_SELECT + where + order, args)
    return render_template("assets.html", rows=rows, pg=pg, **lookups())


IMPORT_COLS = ["tag", "name", "category", "building", "room", "status", "brand", "model", "serial_no", "ip_address",
               "mac_address", "purchase_date", "warranty_until", "cost", "vendor", "notes", "parent_tag", "department"]


def csv_response(filename, header, rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    w.writerows(rows)
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


NEW_STAGES = ["Requested", "Ordered", "Received (pending inspection)", "In stock"]  # "Deployed" comes from issuing it to someone


def asset_values():
    f = request.form
    v = {k: (f.get(k) or "").strip() or None for k in FIELDS}
    if not v["name"]:
        raise ValueError("Name is required")
    v["condition"] = v["condition"] if v["condition"] in CONDITIONS else "Working"
    v["acquisition_basis"] = v["acquisition_basis"] if v["acquisition_basis"] in ACQ_BASIS else "Unknown"
    v["cost_basis"] = v["cost_basis"] if v["cost_basis"] in COST_BASIS else None
    for k in ("category_id", "room_id", "managing_dept_id", "owning_dept_id"):
        v[k] = opt_int(v[k])
    v["location_unresolved"] = 1 if f.get("location_unresolved") else 0
    v["tax_included"] = {"1": 1, "0": 0}.get(f.get("tax_included"))
    v["cost"] = opt_float(v["cost"], "Cost")
    if v["managing_dept_id"] is None and (ids := dept_ids()) and len(ids) == 1:
        v["managing_dept_id"] = next(iter(ids))  # a department user adds to their own department
    return v


def pretty(field, val):
    if val is None or val == "":
        return "—"
    if field in ("category_id", "room_id", "managing_dept_id", "owning_dept_id"):
        table = {"category_id": "categories", "room_id": "rooms"}.get(field, "departments")
        row = q(f"SELECT name FROM {table} WHERE id=?", (val,), one=True)
        return row["name"] if row else "—"
    return str(val)


def check_location(v, parent=None):
    if parent:
        v["room_id"], v["location_unresolved"] = parent["room_id"], parent["location_unresolved"]  # parts live where their parent lives
    elif not v["room_id"] and not v["location_unresolved"]:
        raise ValueError("Room is required (or tick 'Location unknown' to flag it for follow-up)")


@app.route("/assets/new", methods=["GET", "POST"])
@role("edit")
def asset_new():
    parent = q("SELECT * FROM assets WHERE id=?", (opt_int(request.values.get("parent")),), one=True)
    if parent:
        get_asset(parent["id"])
    if request.method == "POST":
        try:
            v = asset_values()
            check_location(v, parent)
            if parent:
                v["managing_dept_id"], v["owning_dept_id"] = parent["managing_dept_id"], parent["owning_dept_id"]
            elif v["managing_dept_id"] is None:
                raise ValueError("Managing department is required")
            stage = parent["lifecycle_stage"] if parent else request.form.get("lifecycle_stage")
            stage = stage if stage in NEW_STAGES or parent else "In stock"
            v["tag"] = v["tag"] or next_tag()
            cols = FIELDS + ["parent_id", "lifecycle_stage"]
            cur = db().execute(f"INSERT INTO assets({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                               [v[k] for k in FIELDS] + [parent["id"] if parent else None, stage])
            aid = cur.lastrowid
            sync_status(aid)
            save_attributes(aid, v["category_id"], request.form)
            log(aid, "Added" + (f" as part of {parent['tag']}" if parent else ""), etype="Created")
            if parent:
                log(parent["id"], f"Part added: {v['name']}", etype="Part added")
            src = opt_int(request.form.get("copy_parts_from"))
            if src and not parent:
                # duplicate the source's parts (serials/IPs left blank, they differ per unit)
                for p in q("SELECT * FROM assets WHERE parent_id=?", (src,)):
                    pc = db().execute("""INSERT INTO assets(tag, name, category_id, room_id, parent_id, brand, model, purchase_date, warranty_until,
                                         cost, vendor, lifecycle_stage, managing_dept_id, owning_dept_id, acquisition_basis)
                                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                      (next_tag(), p["name"], p["category_id"], v["room_id"], aid, p["brand"], p["model"], p["purchase_date"],
                                       p["warranty_until"], p["cost"], p["vendor"], stage, v["managing_dept_id"], v["owning_dept_id"], p["acquisition_basis"]))
                    sync_status(pc.lastrowid)
                    log(pc.lastrowid, "Added as part of new asset (copied)", etype="Created")
            db().commit()
            flash("Asset added.", "ok")
            return redirect(url_for("asset", aid=aid))
        except ValueError as e:
            db().rollback()
            flash(str(e), "error")
        except sqlite3.IntegrityError:
            db().rollback()
            flash("That tag is already used by another asset.", "error")
    copy = q("SELECT * FROM assets WHERE id=?", (opt_int(request.args.get("copy")),), one=True)
    if copy:
        get_asset(copy["id"])
        preset = {k: copy[k] for k in FIELDS if k not in ("tag", "serial_no", "ip_address", "mac_address", "notes", "legacy_code")}
        preset["copy_id"] = copy["id"]
        preset["copy_parts"] = q("SELECT COUNT(*) n FROM assets WHERE parent_id=?", (copy["id"],), one=True)["n"]
        if copy["parent_id"]:
            parent = q("SELECT * FROM assets WHERE id=?", (copy["parent_id"],), one=True)
    else:
        preset = {"room_id": opt_int(request.args.get("room")) or (parent["room_id"] if parent else None)}
    return render_template("asset_form.html", a=None, parent=parent, preset=preset, copy=copy, **lookups())


@app.route("/assets/<int:aid>")
def asset(aid):
    a = get_asset(aid)
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
                      WHERE ac.asset_id=?""" + ("" if allowed("docs") else " AND (ac.event_type IS NULL OR ac.event_type!='Document')")
                   + " ORDER BY ac.id DESC LIMIT 40", (aid,)),  # document names are only for people who may open the documents
        custody=current_custody(aid),
        custody_history=q("""SELECT c.*, d.name dept, u.name by_name FROM custody c LEFT JOIN departments d ON d.id=c.department_id
                             LEFT JOIN users u ON u.id=c.started_by WHERE c.asset_id=? ORDER BY c.id DESC""", (aid,)),
        identifiers=q("SELECT * FROM asset_ids WHERE asset_id=? ORDER BY kind, id", (aid,)),
        next_stages=LIFECYCLE_NEXT.get(a["lifecycle_stage"], []),
        **lookups())


@app.route("/assets/<int:aid>/edit", methods=["GET", "POST"])
@role("edit")
def asset_edit(aid):
    a = get_asset(aid)
    if request.method == "POST":
        try:
            v = asset_values()
            v["tag"] = v["tag"] or a["tag"]
            check_location(v, q("SELECT * FROM assets WHERE id=?", (a["parent_id"],), one=True) if a["parent_id"] else None)
            if a["parent_id"]:
                v["managing_dept_id"], v["owning_dept_id"] = a["managing_dept_id"], a["owning_dept_id"]
            elif v["managing_dept_id"] is None:
                raise ValueError("Managing department is required")
            tag_changed = v["tag"] != a["tag"]
            if tag_changed:
                if not allowed("admin"):
                    raise ValueError("Only an admin can correct a tag.")
                if not request.form.get("tag_reason", "").strip():
                    raise ValueError("Say why the tag is being corrected. The old tag stays searchable.")
            changes = [f"{LABELS[k]}: {pretty(k, a[k])} → {pretty(k, v[k])}"
                       for k in FIELDS if str(a[k] if a[k] is not None else "") != str(v[k] if v[k] is not None else "")]
            if tag_changed:
                db().execute("INSERT INTO asset_ids(asset_id, kind, value, source, added_by) VALUES (?,?,?,?,?)", (aid, "previous_tag", a["tag"], "", g.user["id"]))
            db().execute(f"UPDATE assets SET {','.join(k + '=?' for k in FIELDS)} WHERE id=?", [v[k] for k in FIELDS] + [aid])
            db().execute("""UPDATE assets SET room_id=?, location_unresolved=?, managing_dept_id=?, owning_dept_id=? WHERE parent_id=?""",
                         (v["room_id"], v["location_unresolved"], v["managing_dept_id"], v["owning_dept_id"], aid))
            sync_status(aid)
            before = {r["attr_id"]: r["value"] for r in q("SELECT attr_id, value FROM attribute_values WHERE asset_id=?", (aid,))}
            save_attributes(aid, v["category_id"], request.form)
            if before != {r["attr_id"]: r["value"] for r in q("SELECT attr_id, value FROM attribute_values WHERE asset_id=?", (aid,))}:
                changes.append("Specifications updated")
            if changes:
                log(aid, "; ".join(changes), etype="Corrected" if tag_changed else "Edited", reason=request.form.get("tag_reason") if tag_changed else None)
            db().commit()
            flash("Saved.", "ok")
            return redirect(url_for("asset", aid=aid))
        except ValueError as e:
            db().rollback()
            flash(str(e), "error")
        except sqlite3.IntegrityError:
            db().rollback()
            flash("That tag is already used by another asset.", "error")
    parent = q("SELECT * FROM assets WHERE id=?", (a["parent_id"],), one=True) if a["parent_id"] else None
    return render_template("asset_form.html", a=a, parent=parent, preset={}, **lookups())


def current_custody(aid):
    return q("SELECT * FROM custody WHERE asset_id=? AND ended_at IS NULL", (aid,), one=True)


def parse_date(v):
    """A date the user typed for when something really happened: today or earlier."""
    v = (v or "").strip() or date.today().isoformat()
    try:
        d = date.fromisoformat(v)
    except ValueError:
        raise ValueError("Enter the date as year-month-day.")
    if d > date.today():
        raise ValueError("That date is in the future.")
    return d.isoformat()


def change_custody(a, f, to=None):
    """Issue, return or transfer an asset. Closing the old holder and opening the new one happen together, and the form carries the
    custody record it was looking at, so two people issuing the same asset can't both succeed."""
    to = to or f.get("to")
    if to not in HOLDERS:
        raise ValueError("Choose who holds it now.")
    if a["lifecycle_stage"] not in ("In stock", "Deployed"):
        raise ValueError(f"It is '{a['lifecycle_stage']}'. Only assets in stock or deployed can be issued, returned or moved.")
    cur = current_custody(a["id"])
    if str(cur["id"] if cur else 0) != str(f.get("expected", "0")):
        raise ValueError("Someone else just changed who holds this asset. Check the page again before moving it.")
    when = parse_date(f.get("event_date"))
    dept_id, room_id = opt_int(f.get("department_id")), opt_int(f.get("room_id"))
    person, reason = (f.get("person") or "").strip() or None, (f.get("reason") or "").strip()
    if to == "Department" and not dept_id:
        raise ValueError("Choose the department.")
    if to == "Person" and not person:
        raise ValueError("Enter the person's name.")
    if not reason:
        raise ValueError("Say why (for example: new lab setup, repair finished, staff left).")
    same = (cur["holder_type"] == to and (to == "Stockroom" or (to == "Department" and cur["department_id"] == dept_id) or
                                          (to == "Person" and cur["person"] == person and cur["department_id"] == dept_id))) if cur else (
        to == "Stockroom" and a["lifecycle_stage"] == "In stock")
    if same and not room_id:
        raise ValueError("It is already there.")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    kind = "Return" if to == "Stockroom" else "Issue" if (not cur or cur["holder_type"] == "Stockroom") else "Transfer"
    old = "stockroom" if not cur or cur["holder_type"] == "Stockroom" else (cur["person"] or dept_name(cur["department_id"]))
    new = "the stockroom" if to == "Stockroom" else (person or dept_name(dept_id))
    try:
        if cur:
            db().execute("UPDATE custody SET ended_on=?, ended_at=?, ended_by=?, end_reason=? WHERE id=?", (when, stamp, g.user["id"], reason, cur["id"]))
        db().execute("""INSERT INTO custody(asset_id, holder_type, department_id, person, room_id, due_date, started_on, started_at, started_by,
                        reason, approval_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                     (a["id"], to, dept_id if to != "Stockroom" else None, person if to == "Person" else None, room_id,
                      f.get("due_date") or None, when, stamp, g.user["id"], reason, (f.get("approval_ref") or "").strip() or None))
    except sqlite3.IntegrityError:
        raise ValueError("Someone else just changed who holds this asset. Check the page again before moving it.")
    db().execute("""UPDATE assets SET lifecycle_stage=?, using_dept_id=?, custodian=?, room_id=COALESCE(?, room_id) WHERE id=?""",
                 ("In stock" if to == "Stockroom" else "Deployed", dept_id if to != "Stockroom" else None, person if to == "Person" else None,
                  room_id, a["id"]))
    if room_id:
        db().execute("UPDATE assets SET room_id=? WHERE parent_id=?", (room_id, a["id"]))
    sync_status(a["id"])
    log(a["id"], f"{kind}: {old} → {new}", etype=kind, event_date=when, reason=reason, approval=f.get("approval_ref"))
    return kind


def dept_name(did):
    row = q("SELECT name FROM departments WHERE id=?", (did,), one=True) if did else None
    return row["name"] if row else "—"


IT_DATA_CATEGORIES = {"Desktop computer", "Laptop", "Server", "Hard disk / SSD", "Firewall", "Router"}


def change_lifecycle(a, f):
    """Move an asset along its life: retirement needs a proposal, a different person to approve it, then evidence of disposal."""
    to, now_stage = f.get("to_stage"), a["lifecycle_stage"]
    if to not in LIFECYCLE_NEXT.get(now_stage, []):
        nxt = ", ".join(LIFECYCLE_NEXT.get(now_stage, [])) or "nothing (this is final)"
        raise ValueError(f"From '{now_stage}' it can only go to: {nxt}.")
    when = parse_date(f.get("event_date"))
    reason, evidence = (f.get("reason") or "").strip(), (f.get("evidence") or "").strip()
    if not reason:
        raise ValueError("Say why.")
    cur = current_custody(a["id"])
    held = bool(cur and cur["holder_type"] != "Stockroom")
    if to == "In stock" and held:
        raise ValueError("It is with someone. Use Return to take it back to the stockroom.")
    if to == "Deployed" and not held:
        raise ValueError("Use Issue to hand it to a department or person.")
    if to == "Retirement approved":
        if not allowed("approve"):
            raise ValueError("Only an HOD or admin can approve a retirement.")
        proposed = q("SELECT user_id FROM activity WHERE asset_id=? AND event_type='Retirement proposed' ORDER BY id DESC LIMIT 1", (a["id"],), one=True)
        if proposed and proposed["user_id"] == g.user["id"]:
            raise ValueError("A different person must approve a retirement proposal.")
    if to == "Disposed":
        if not evidence:
            raise ValueError("Give the disposal evidence (certificate, e-waste receipt or handover reference).")
        cat = q("SELECT name FROM categories WHERE id=?", (a["category_id"],), one=True)
        if cat and cat["name"] in IT_DATA_CATEGORIES and not f.get("data_erased"):
            raise ValueError("Confirm that stored data was erased before disposal.")
    if to in ("Disposed", "Returned to vendor") and cur:
        db().execute("UPDATE custody SET ended_on=?, ended_at=?, ended_by=?, end_reason=? WHERE id=?",
                     (when, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), g.user["id"], f"Asset {to.lower()}", cur["id"]))
        db().execute("UPDATE assets SET using_dept_id=NULL, custodian=NULL WHERE id=?", (a["id"],))
    db().execute("UPDATE assets SET lifecycle_stage=? WHERE id=?", (to, a["id"]))
    sync_status(a["id"])
    log(a["id"], f"Stage: {now_stage} → {to}", etype=to, event_date=when, reason=reason, approval=evidence or f.get("approval_ref"))


@app.route("/assets/<int:aid>/custody", methods=["POST"])
@role("move")
def asset_custody(aid):
    a = get_asset(aid)
    try:
        kind = change_custody(a, request.form)
        db().commit()
        flash(f"{kind} recorded.", "ok")
    except ValueError as e:
        db().rollback()
        flash(str(e), "error")
    return redirect(url_for("asset", aid=aid))


@app.route("/assets/<int:aid>/lifecycle", methods=["POST"])
@role("move")
def asset_lifecycle(aid):
    a = get_asset(aid)
    try:
        change_lifecycle(a, request.form)
        db().commit()
        flash("Stage updated.", "ok")
    except ValueError as e:
        db().rollback()
        flash(str(e), "error")
    return redirect(url_for("asset", aid=aid))


@app.route("/custody/<int:cid>/ack", methods=["POST"])
def custody_ack(cid):
    """The person holding an asset confirms they received it."""
    c = q("SELECT * FROM custody WHERE id=? AND ended_at IS NULL", (cid,), one=True)
    if not c:
        abort(404)
    a = get_asset(c["asset_id"])
    if not (allowed("move") or (c["person"] and c["person"].strip().lower() == g.user["name"].strip().lower())):
        abort(403)
    db().execute("UPDATE custody SET ack_by=?, ack_at=? WHERE id=?", (g.user["name"], datetime.now().strftime("%Y-%m-%d %H:%M"), cid))
    log(a["id"], f"Receipt acknowledged by {g.user['name']}", etype="Acknowledged")
    db().commit()
    flash("Receipt recorded.", "ok")
    return redirect(url_for("asset", aid=a["id"]))


@app.route("/assets/<int:aid>/lend", methods=["POST"])
@role("move")
def asset_lend(aid):
    """Quick lend / return (kept for the Lent out list): a custody record to a person with a due date."""
    a = get_asset(aid)
    f = request.form
    try:
        if f.get("action") == "return":
            if not current_custody(aid) or current_custody(aid)["holder_type"] == "Stockroom":
                raise ValueError("It isn't lent out.")
            change_custody(a, {**f, "expected": current_custody(aid)["id"], "reason": "Returned"}, to="Stockroom")
            flash("Marked as returned.", "ok")
        else:
            cur = current_custody(aid)
            change_custody(a, {**f, "person": f.get("assigned_to"), "due_date": f.get("assigned_due"), "reason": "Lent out",
                               "expected": cur["id"] if cur else 0}, to="Person")
            flash(f"Lent to {f.get('assigned_to', '').strip()}.", "ok")
        db().commit()
    except ValueError as e:
        db().rollback()
        flash(str(e), "error")
    return redirect(url_for("asset", aid=aid))


@app.route("/loans")
def loans():
    where, args = scope_sql()
    rows = q(ASSET_SELECT.replace("FROM assets a", ", cu.person lent_to, cu.due_date lent_due, cu.started_on lent_on FROM assets a JOIN custody cu ON cu.asset_id=a.id AND cu.ended_at IS NULL AND cu.holder_type='Person'", 1)
             + " WHERE 1=1" + where + " ORDER BY cu.due_date IS NULL, cu.due_date", args)
    return render_template("loans.html", rows=rows)


@app.route("/assets/<int:aid>/identifiers", methods=["POST"])
@role("edit")
def asset_identifier(aid):
    """Keep an old asset code or another system's ID, so staff can still search for it."""
    a = get_asset(aid)
    f = request.form
    kind, value, source = f.get("kind"), (f.get("value") or "").strip(), (f.get("source") or "").strip()
    if kind not in ("legacy_code", "external") or not value:
        flash("Enter the old code or ID.", "error")
    elif kind == "external" and not source:
        flash("Say which system the ID comes from.", "error")
    else:
        try:
            db().execute("INSERT INTO asset_ids(asset_id, kind, value, source, added_by) VALUES (?,?,?,?,?)", (aid, kind, value, source, g.user["id"]))
            log(aid, f"{ID_KINDS[kind]} added: {value}" + (f" ({source})" if source else ""), etype="Identifier")
            db().commit()
            flash("Saved.", "ok")
        except sqlite3.IntegrityError:
            other = q("SELECT a.id, a.tag FROM asset_ids i JOIN assets a ON a.id=i.asset_id WHERE i.kind=? AND i.value=? AND i.source=?", (kind, value, source), one=True)
            flash(f"{value} already belongs to {other['tag']}." if other else "Already recorded.", "error")
    return redirect(url_for("asset", aid=aid))


@app.route("/assets/<int:aid>/delete", methods=["POST"])
@role("admin")
def asset_delete(aid):
    """Approved assets are archived, never erased: the record, its history and its tag stay."""
    a = get_asset(aid)
    reason = request.form.get("reason", "").strip()
    if q("SELECT 1 FROM assets WHERE parent_id=? AND archived_at IS NULL LIMIT 1", (aid,), one=True):
        flash("Archive or move its parts first.", "error")
    elif not reason:
        flash("Say why it is being archived.", "error")
    else:
        cur = current_custody(aid)
        if cur:
            db().execute("UPDATE custody SET ended_on=date('now'), ended_at=datetime('now'), ended_by=?, end_reason='Archived' WHERE id=?", (g.user["id"], cur["id"]))
        db().execute("UPDATE assets SET archived_at=datetime('now'), archive_reason=?, using_dept_id=NULL, custodian=NULL WHERE id=?", (reason, aid))
        sync_status(aid)
        log(aid, f"Archived: {reason}", etype="Archived", reason=reason)
        db().commit()
        flash("Asset archived. Its history is kept.", "ok")
    return redirect(url_for("asset", aid=aid))


# ---------- issues ----------

@app.route("/assets/<int:aid>/issues", methods=["POST"])
def issue_new(aid):
    a = get_asset(aid)
    f = request.form
    if not f.get("title", "").strip():
        flash("Describe the issue.", "error")
        return redirect(url_for("asset", aid=aid))
    cur = db().execute("INSERT INTO issues(asset_id, title, type, priority, description, reported_by) VALUES (?,?,?,?,?,?)",
                 (aid, f["title"].strip(), f.get("type") if f.get("type") in ISSUE_TYPES else "Other",
                  f.get("priority") if f.get("priority") in PRIORITIES else "Medium", f.get("description"), g.user["id"]))
    log(aid, f"Issue reported: {f['title'].strip()}")
    if f.get("mark_faulty") and a["condition"] == "Working":
        db().execute("UPDATE assets SET condition='Faulty' WHERE id=?", (aid,))
        sync_status(aid)
        log(aid, "Condition: Working → Faulty", etype="Condition")
    attach_photos(aid, cur.lastrowid)
    db().commit()
    flash("Issue reported.", "ok")
    return redirect(url_for("asset", aid=aid))


@app.route("/assets/<int:aid>/photos", methods=["POST"])
@role("edit")
def asset_photos(aid):
    get_asset(aid)
    n = attach_photos(aid)
    db().commit()
    if n:
        flash(f"{n} photo(s) added.", "ok")
    return redirect(url_for("asset", aid=aid))


@app.route("/issues/<int:iid>/resolve", methods=["POST"])
@role("edit")
def issue_resolve(iid):
    i = q("SELECT * FROM issues WHERE id=?", (iid,), one=True)
    if not i:
        abort(404)
    get_asset(i["asset_id"])
    if i["status"] == "Open":
        db().execute("UPDATE issues SET status='Resolved', resolved_by=?, resolved_at=CURRENT_TIMESTAMP, resolution=? WHERE id=?",
                     (g.user["id"], request.form.get("resolution", "").strip(), iid))
        log(i["asset_id"], f"Issue resolved: {i['title']}")
        if request.form.get("mark_working"):
            db().execute("UPDATE assets SET condition='Working' WHERE id=?", (i["asset_id"],))
            sync_status(i["asset_id"])
            log(i["asset_id"], "Condition → Working", etype="Condition")
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
             LEFT JOIN buildings b ON b.id=r.building_id LEFT JOIN users u ON u.id=i.reported_by WHERE 1=1""" + scope_sql("x")[0]
    args = list(scope_sql("x")[1])
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
@role("edit")
def service_new(aid):
    a = get_asset(aid)
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
    if f.get("condition") in CONDITIONS and f["condition"] != a["condition"]:
        db().execute("UPDATE assets SET condition=? WHERE id=?", (f["condition"], aid))
        sync_status(aid)
        log(aid, f"Condition: {a['condition']} → {f['condition']}", etype="Condition")
    log(aid, f"Service logged: {stype}")
    db().commit()
    flash("Service logged.", "ok")
    return redirect(url_for("asset", aid=aid))


@app.route("/services")
def services():
    soon = (date.today() + timedelta(days=30)).isoformat()
    sw, sa = scope_sql()
    due = q(ASSET_SELECT + " WHERE a.next_service_due IS NOT NULL AND a.next_service_due <= ? AND a.status!='Scrapped'" + sw + " ORDER BY a.next_service_due", [soon, *sa])
    term = request.args.get("q", "").strip()
    rows, pg = paginate("""SELECT s.*, a.tag, a.name asset_name, r.name room FROM services s JOIN assets a ON a.id=s.asset_id
                LEFT JOIN rooms r ON r.id=a.room_id WHERE 1=1""" + sw
                + (" AND (a.tag LIKE ? OR a.name LIKE ? OR s.description LIKE ? OR s.done_by LIKE ? OR s.type LIKE ?)" if term else "")
                + " ORDER BY s.date DESC, s.id DESC", [*sa, *([f"%{term}%"] * 5 if term else [])])
    return render_template("services.html", due=due, rows=rows, pg=pg)


# ---------- labels ----------

@app.route("/labels")
def labels():
    rid = opt_int(request.args.get("room"))
    aid = opt_int(request.args.get("asset"))
    sw, sa = scope_sql()
    if aid:
        rows = q(ASSET_SELECT + " WHERE a.id=?" + sw, [aid, *sa])
    elif rid:
        rows = q(ASSET_SELECT + " WHERE a.room_id=? AND a.archived_at IS NULL" + sw + " ORDER BY a.tag", [rid, *sa])
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
        depts=q("SELECT d.*, (SELECT COUNT(*) FROM assets a WHERE a.managing_dept_id=d.id) n FROM departments d ORDER BY name"),
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
            db().execute("INSERT INTO buildings(name, code, campus) VALUES (?,?,?)", (name, f.get("code", "").strip().upper() or None, f.get("campus", "").strip() or None))
        elif kind == "rooms":
            if not opt_int(f.get("building_id")):
                raise ValueError("Pick a building")
            db().execute("INSERT INTO rooms(building_id, name, type, floor) VALUES (?,?,?,?)",
                         (int(f["building_id"]), name, f.get("type") if f.get("type") in ROOM_TYPES else "Other",
                          f.get("floor", "").strip() or None))
        elif kind == "categories":
            db().execute("INSERT INTO categories(name, life_years) VALUES (?,?)", (name, opt_int(f.get("life_years")) or 5))
        elif kind == "departments":
            db().execute("INSERT INTO departments(name, code) VALUES (?,?)", (name, f.get("code", "").strip().upper() or None))
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
              "categories": "SELECT 1 FROM assets WHERE category_id=?",
              "departments": "SELECT 1 FROM assets WHERE managing_dept_id=?1 OR owning_dept_id=?1 OR using_dept_id=?1 UNION SELECT 1 FROM users WHERE department_id=?1"}
    if kind not in in_use:
        abort(404)
    if q(in_use[kind] + " LIMIT 1", (item,), one=True):
        flash("Still in use, move or remove what's inside first.", "error")
    else:
        db().execute(f"DELETE FROM {kind} WHERE id=?", (item,))
        db().commit()
        flash("Deleted.", "ok")
    return redirect(url_for("settings") + "#" + kind)


# ---------- users ----------

PRIVILEGED = ("super_admin", "admin", "system_admin")  # only a super admin may give these out


def user_form_values(f):
    """(primary role, extra roles csv, department id, extra department ids) from a user form, or raises ValueError."""
    primary = f.get("role")
    extras = [r for r in f.getlist("extra_roles") if r in ROLES and r != primary]
    if primary not in ROLES:
        raise ValueError("Choose a role.")
    if (primary in PRIVILEGED or any(r in PRIVILEGED for r in extras)) and not has_role("super_admin"):
        raise ValueError("Only a super admin can give the super admin, admin or system admin role.")
    dept = opt_int(f.get("department_id"))
    scopes = {d for d in (opt_int(x) for x in f.getlist("scopes")) if d and d != dept}
    return primary, ",".join(extras) or None, dept, scopes


def save_scopes(uid, scopes):
    db().execute("DELETE FROM user_scopes WHERE user_id=?", (uid,))
    db().executemany("INSERT OR IGNORE INTO user_scopes(user_id, department_id) VALUES (?,?)", [(uid, d) for d in scopes])


@app.route("/users")
@role("users")
def users():
    term = request.args.get("q", "").strip()
    rows, pg = paginate("SELECT u.*, d.name dept FROM users u LEFT JOIN departments d ON d.id=u.department_id"
                        + (" WHERE u.name LIKE ? OR u.username LIKE ?" if term else "") + " ORDER BY u.role, u.name",
                        [f"%{term}%"] * 2 if term else [])
    scopes = {}
    for r in q("SELECT user_id, department_id FROM user_scopes"):
        scopes.setdefault(r["user_id"], set()).add(r["department_id"])
    return render_template("users.html", rows=rows, pg=pg, ROLE_LABEL=ROLE_LABEL, scopes=scopes, roles_of=roles_of, **lookups())


@app.route("/users/add", methods=["POST"])
@role("users")
def user_add():
    f = request.form
    try:
        primary, extras, dept, scopes = user_form_values(f)
        if len(f.get("password", "")) < 8 or not f.get("username", "").strip() or not f.get("name", "").strip():
            raise ValueError("Name, username and a password of 8+ characters are required.")
        cur = db().execute("INSERT INTO users(username, name, password_hash, role, extra_roles, department_id) VALUES (?,?,?,?,?,?)",
                           (f["username"].strip().lower(), f["name"].strip(), generate_password_hash(f["password"]), primary, extras, dept))
        save_scopes(cur.lastrowid, scopes)
        db().commit()
        flash("User added.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    except sqlite3.IntegrityError:
        flash("Username already taken.", "error")
    return redirect(url_for("users"))


@app.route("/users/<int:uid>/update", methods=["POST"])
@role("users")
def user_update(uid):
    f = request.form
    target = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not target:
        abort(404)
    try:
        if f.get("password") and len(f["password"]) < 8:
            raise ValueError("Password must be at least 8 characters.")
        if roles_of(target) & set(PRIVILEGED) and not has_role("super_admin"):
            raise ValueError("Only a super admin can change an admin account.")
        if uid == g.user["id"]:
            primary, extras = target["role"], target["extra_roles"]  # you can't change your own role
            dept, scopes = opt_int(f.get("department_id")), {d for d in (opt_int(x) for x in f.getlist("scopes")) if d}
        else:
            primary, extras, dept, scopes = user_form_values(f)
        db().execute("UPDATE users SET role=?, extra_roles=?, department_id=? WHERE id=?", (primary, extras, dept, uid))
        save_scopes(uid, scopes - {dept})
        if f.get("password"):
            db().execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(f["password"]), uid))
        db().commit()
        flash("User updated.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("users"))


@app.route("/users/<int:uid>/delete", methods=["POST"])
@role("users")
def user_delete(uid):
    target = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if uid == g.user["id"]:
        flash("You can't delete yourself.", "error")
    elif target and roles_of(target) & set(PRIVILEGED) and not has_role("super_admin"):
        flash("Only a super admin can delete an admin account.", "error")
    else:
        # keep history readable: records just lose the user link
        for t, c in (("issues", "reported_by"), ("issues", "resolved_by"), ("services", "logged_by"), ("activity", "user_id"),
                     ("custody", "started_by"), ("custody", "ended_by"), ("asset_ids", "added_by"), ("photos", "uploaded_by")):
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
# (username, name, role, department)
DEMO_USERS = [("super", "Demo Super Admin", "super_admin", None), ("admin", "Demo Admin", "admin", None),
              ("sysadmin", "System Admin", "system_admin", None), ("purchase", "Purchase Officer", "purchase", None),
              ("hod", "Dr. Mohan (HOD, CSE)", "hod", "CSE"), ("lab", "Lakshmi (Lab assistant, CSE)", "lab_assistant", "CSE"),
              ("staff", "Priya (Faculty, CSE)", "custodian", "CSE"), ("auditor", "Auditor", "auditor", None),
              ("tech", "Ravi (IT technician)", "technician", None)]


def seed_demo():
    with sqlite3.connect(DB) as c:
        if c.execute("SELECT 1 FROM buildings LIMIT 1").fetchone():
            return
        dept = {n: i for i, n in c.execute("SELECT id, name FROM departments")}
        for u, n, r, d in DEMO_USERS:  # demo accounts are added next to any account that already exists
            c.execute("INSERT OR IGNORE INTO users(username, name, password_hash, role, department_id) VALUES (?,?,?,?,?)",
                      (u, n, generate_password_hash(f"demo-{u}-2026"), r, dept.get(d)))
        uid = {u: i for i, u in c.execute("SELECT id, username FROM users")}
        cat = {n: i for i, n in c.execute("SELECT id, name FROM categories")}
        for n, code in {"Main Block": "MB", "CSE Block": "CSE", "ECE Block": "ECE"}.items():
            c.execute("INSERT INTO buildings(name, code) VALUES (?,?)", (n, code))
        bid = {n: i for i, n in c.execute("SELECT id, name FROM buildings")}
        for b, n, t, fl in [("CSE Block", "Computer Lab 1", "Lab", "1"), ("CSE Block", "Computer Lab 2", "Lab", "1"),
                            ("CSE Block", "Server Room", "Network room", "G"), ("CSE Block", "CS-201", "Classroom", "2"),
                            ("ECE Block", "Electronics Lab", "Lab", "1"), ("ECE Block", "EC-101", "Classroom", "1"),
                            ("Main Block", "Principal Office", "Office", "G"), ("Main Block", "Seminar Hall", "Seminar hall", "1"),
                            ("Main Block", "Library", "Library", "G"), ("Main Block", "Central Store", "Other", "G")]:
            c.execute("INSERT INTO rooms(building_id, name, type, floor) VALUES (?,?,?,?)", (bid[b], n, t, fl))
        rid = {n: i for i, n in c.execute("SELECT id, name FROM rooms")}
        today = date.today()
        aid = {}

        def add(name, category, room, owner, stage="Deployed", cond="Working", parent=None, holder=None, **kw):
            """owner = managing department; holder = department using it (None = stockroom / spare)."""
            kw = {"acquisition_basis": "Purchase", **kw}
            status = derive_status({"archived_at": None, "lifecycle_stage": stage, "condition": cond})
            cols = ["name", "category_id", "room_id", "status", "lifecycle_stage", "condition", "parent_id", "managing_dept_id", "owning_dept_id",
                    "using_dept_id"] + list(kw)
            cur = c.execute(f"INSERT INTO assets({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [name, cat[category], rid[room] if room else None, status, stage, cond, parent, dept[owner], dept[owner],
                             dept[holder] if holder else None] + list(kw.values()))
            n = cur.lastrowid
            c.execute("UPDATE assets SET tag=? WHERE id=?", (f"AMS-{n:05d}", n))
            c.execute("INSERT INTO activity(asset_id, text, event_type) VALUES (?, 'Added (demo data)', 'Created')", (n,))
            if holder and not parent:
                c.execute("INSERT INTO custody(asset_id, holder_type, department_id, started_on, reason, started_by) VALUES (?,?,?,?,?,?)",
                          (n, "Department", dept[holder], (today - timedelta(days=200)).isoformat(), "Initial setup", uid["admin"]))
            aid[(room, name)] = n
            return n

        for lab, short in (("Computer Lab 1", "L1"), ("Computer Lab 2", "L2")):
            for n in range(1, 7):
                pc = add(f"{short}-PC-{n:02d}", "Desktop computer", lab, "CSE", cond="Faulty" if (short, n) == ("L1", 4) else "Working", holder="CSE",
                         brand="Dell", model="OptiPlex 7090", serial_no=f"DL{short}{n:04d}", ip_address=f"10.10.{rid[lab]}.{10 + n}",
                         purchase_date="2023-07-15", cost=52000, vendor="Sri Lakshmi Computers", cost_basis="Per unit", tax_included=1,
                         po_number="PO/2023/0412", invoice_number="SLC-7781", legacy_code=f"REC/CSE/{short}/PC-{n:02d}",
                         warranty_until=(today + timedelta(days=40 if n == 1 else 400)).isoformat())
                add("8 GB DDR4 RAM", "RAM", lab, "CSE", parent=pc, brand="Crucial")
                add("512 GB SSD", "Hard disk / SSD", lab, "CSE", parent=pc, brand="WD")
                add('Dell 22" Monitor', "Monitor", lab, "CSE", parent=pc, brand="Dell", model="E2222H")
            add("Projector", "Projector", lab, "CSE", holder="CSE", brand="Epson", model="EB-X51", cost=38000,
                next_service_due=(today + timedelta(days=12)).isoformat())
            add("24-port Switch", "Network switch", lab, "CSE", holder="CSE", brand="TP-Link", model="TL-SG1024", ip_address=f"10.10.{rid[lab]}.2")
        for name, cname, brand, model, extra in [("Core Router", "Router", "Cisco", "ISR 4331", {"ip_address": "10.10.0.1", "cost": 145000}),
                                                 ("Core Switch", "Network switch", "Cisco", "Catalyst 9200", {"ip_address": "10.10.0.2"}),
                                                 ("Firewall", "Firewall", "Fortinet", "FortiGate 60F", {"ip_address": "10.10.0.254"}),
                                                 ("File Server", "Server", "HPE", "ProLiant DL380",
                                                  {"cost": 320000, "next_service_due": (today - timedelta(days=3)).isoformat()})]:
            add(name, cname, "Server Room", "Central IT", holder="Central IT", brand=brand, model=model, **extra)
        add("10 kVA UPS", "UPS", "Server Room", "Central IT", stage="Under maintenance", cond="Faulty", holder="Central IT", brand="APC", cost=95000)
        add("Classroom Projector", "Projector", "CS-201", "CSE", holder="CSE", brand="BenQ")
        add("Wi-Fi AP", "Wi-Fi access point", "CS-201", "Central IT", holder="Central IT", brand="Ubiquiti", model="U6 Lite", ip_address="10.10.20.5")
        add("Digital Storage Oscilloscope", "Lab instrument", "Electronics Lab", "ECE", holder="ECE", brand="Tektronix", model="TBS1052C", cost=48000)
        add("Function Generator", "Lab instrument", "Electronics Lab", "ECE", holder="ECE", brand="Keysight", cost=30000)
        add("Smart Board", "Smart board", "EC-101", "ECE", holder="ECE", brand="Samsung", model="Flip 3")
        add("Office Laptop", "Laptop", "Principal Office", "Administration", holder="Administration", brand="Lenovo", model="ThinkPad E14")
        printer = add("Laser Printer", "Printer", "Principal Office", "Administration", stage="Retirement proposed", cond="Faulty", holder="Administration",
                      brand="HP", model="LaserJet M1136")
        add("PA System", "Other electronics", "Seminar Hall", "Administration", holder="Administration", brand="Ahuja")
        add("Kiosk PC", "Desktop computer", "Library", "Library", holder="Library", brand="Acer")
        add("Wi-Fi AP", "Wi-Fi access point", "Library", "Central IT", holder="Central IT", brand="Ubiquiti", model="U6 Lite")
        for n in (1, 2):
            add("8 GB DDR4 RAM (spare)", "RAM", "Central Store", "Central IT", stage="In stock", brand="Crucial", acquisition_basis="Purchase")
        add("Spare 24-inch Monitor", "Monitor", "Central Store", "Central IT", stage="In stock", brand="LG")
        add("Donated Desktop", "Desktop computer", "Central Store", "Central IT", stage="In stock", acquisition_basis="Donation", brand="HP",
            notes="Donated by an alumnus, no purchase papers.")
        lost = add("Old network switch (record found in the legacy list)", "Network switch", None, "Central IT", stage="In stock", acquisition_basis="Opening stock (old record)",
                   brand="D-Link", location_unresolved=1, legacy_code="OLD/NET/044")
        for key, title, typ, pri in [(("Computer Lab 1", "L1-PC-04"), "System not booting", "Hardware", "High"),
                                     (("Principal Office", "Laser Printer"), "Paper jam on every print", "Hardware", "Medium"),
                                     (("Library", "Wi-Fi AP"), "Network weak at back of library", "Network", "Medium"),
                                     (("Computer Lab 2", "L2-PC-03"), "MATLAB licence error", "Software", "Low")]:
            c.execute("INSERT INTO issues(asset_id, title, type, priority) VALUES (?,?,?,?)", (aid[key], title, typ, pri))
        c.execute("UPDATE assets SET last_audit=?, verification='Verified' WHERE room_id=?", ((today - timedelta(days=40)).isoformat(), rid["Computer Lab 2"]))
        # a projector lent to a faculty member, overdue
        proj = aid[("CS-201", "Classroom Projector")]
        c.execute("UPDATE custody SET ended_on=?, ended_at=datetime('now'), end_reason='Lent out' WHERE asset_id=? AND ended_at IS NULL", (today.isoformat(), proj))
        c.execute("INSERT INTO custody(asset_id, holder_type, person, department_id, due_date, started_on, reason, started_by) VALUES (?,?,?,?,?,?,?,?)",
                  (proj, "Person", "Dr. Meena (ECE)", dept["ECE"], (today - timedelta(days=2)).isoformat(), (today - timedelta(days=12)).isoformat(), "Lent out", uid["lab"]))
        c.execute("UPDATE assets SET custodian='Dr. Meena (ECE)', using_dept_id=? WHERE id=?", (dept["ECE"], proj))
        # a corrected tag keeps the old one searchable
        pc1 = aid[("Computer Lab 1", "L1-PC-01")]
        c.execute("INSERT INTO asset_ids(asset_id, kind, value, source, added_by) VALUES (?,?,?,?,?)", (pc1, "previous_tag", "CSE-L1-0001", "", uid["admin"]))
        c.execute("INSERT INTO asset_ids(asset_id, kind, value, source, added_by) VALUES (?,?,?,?,?)", (pc1, "external", "PRD4XQKWQA", "College ERP", uid["admin"]))
        c.execute("INSERT INTO activity(asset_id, text, event_type, reason, user_id) VALUES (?,?,?,?,?)",
                  (printer, "Stage: Deployed → Retirement proposed", "Retirement proposed", "Beyond repair, parts not available", uid["lab"]))
        c.execute("INSERT INTO services(asset_id, date, type, description, done_by, cost) VALUES (?,?,?,?,?,?)",
                  (aid[("Server Room", "10 kVA UPS")], today.isoformat(), "Repair", "Battery bank replacement in progress", "APC service", 42000))
        c.execute("UPDATE counters SET value=(SELECT MAX(id) FROM assets) WHERE name='tag'")


import ams_import  # noqa: E402  (the import wizard and purchase batches live in their own file)

ams_import.init(sys.modules[__name__])
import ams_docs  # noqa: E402

ams_docs.init(sys.modules[__name__])
import ams_extras  # noqa: E402

ams_extras.init(sys.modules[__name__])
import ams_more  # noqa: E402

ams_more.init(sys.modules[__name__])
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
