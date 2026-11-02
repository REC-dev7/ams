"""Smoke test: python test_app.py"""
import io
import json
import os
import re
import sqlite3
import tempfile
import threading
from datetime import date, timedelta

import app as m


def csrf(c, path):
    return re.search(r'name="csrf" value="(\w+)"', c.get(path).get_data(as_text=True)).group(1)


def post(c, path, data, page="/"):
    return c.post(path, data={**data, "csrf": csrf(c, page)})


def login(c, user, pw=None):
    c.post("/logout", data={"csrf": csrf(c, "/login")})
    return post(c, "/login", {"username": user, "password": pw or f"demo-{user}-2026"}, "/login")


def row(sql, *args):
    with m.app.app_context():
        return m.q(sql, args, one=True)


def dept(name):
    return str(row("SELECT id FROM departments WHERE name=?", name)["id"])


def asset_id(name):
    return row("SELECT id FROM assets WHERE name=?", name)["id"]


def new_asset(c, name="Test PC", **kw):
    r = post(c, "/assets/new", {"name": name, "managing_dept_id": dept("CSE"), "room_id": "1", **kw}, "/assets/new")
    return int(r.location.rsplit("/", 1)[1]) if r.status_code == 302 else r


def test_flow():
    m.DB = os.path.join(tempfile.mkdtemp(), "t.db")
    m.UPLOADS = os.path.join(os.path.dirname(m.DB), "uploads")
    m.init_db()
    c = m.app.test_client()

    assert c.get("/").location.endswith("/setup")
    assert m.app.config["SESSION_COOKIE_NAME"] == "ams_session"
    post(c, "/setup", {"name": "Boss", "username": "boss", "password": "bosspass12"}, "/setup")
    assert row("SELECT role FROM users WHERE username='boss'")["role"] == "super_admin"
    assert c.get("/").location.startswith("/login")
    r = c.post("/login", data={"username": "boss", "password": "bosspass12"})  # no csrf
    assert r.status_code == 400 and b"form expired" in r.data.lower() and b"REC" in r.data
    assert login(c, "boss", "wrongpass1").status_code == 200
    assert login(c, "boss", "bosspass12").status_code == 302

    m.seed_demo()
    for page in ["/", "/locations", "/assets", "/assets?parts=1", "/issues", "/issues?status=All", "/services", "/labels?room=1", "/settings",
                 "/users", "/rooms/1", "/assets/1", "/assets/new", "/assets/1/edit", "/account", "/loans", "/register"]:
        assert c.get(page).status_code == 200, page
    assert b"System not booting" in c.get("/issues").data
    assert c.get("/assets?export=csv").data.startswith(b"tag,name")

    # permanent tags come from a counter that only goes up; every asset needs a managing department and a place
    assert b"Managing department is required" in post(c, "/assets/new", {"name": "x", "room_id": "1"}, "/assets/new").data
    assert b"Room is required" in post(c, "/assets/new", {"name": "x", "managing_dept_id": dept("CSE")}, "/assets/new").data
    assert b"must be a number" in post(c, "/assets/new", {"name": "x", "managing_dept_id": dept("CSE"), "room_id": "1", "cost": "abc"}, "/assets/new").data
    pc = new_asset(c, "Test PC", cost="1000")
    ram = int(post(c, f"/assets/new?parent={pc}", {"name": "Test RAM"}, "/assets/new").location.rsplit("/", 1)[1])
    t1, t2 = row("SELECT tag FROM assets WHERE id=?", pc)["tag"], row("SELECT tag FROM assets WHERE id=?", ram)["tag"]
    assert t1.startswith("AMS-") and t2 > t1
    assert row("SELECT lifecycle_stage, status, managing_dept_id FROM assets WHERE id=?", pc)["lifecycle_stage"] == "In stock"
    assert row("SELECT managing_dept_id FROM assets WHERE id=?", ram)["managing_dept_id"] == row("SELECT managing_dept_id FROM assets WHERE id=?", pc)["managing_dept_id"]
    assert b"already used" in post(c, "/assets/new", {"name": "dup", "tag": t1, "managing_dept_id": dept("CSE"), "room_id": "1"}, "/assets/new").data
    # unknown location is allowed when flagged
    flagged = new_asset(c, "Found in old list", location_unresolved="1", room_id="")
    assert row("SELECT room_id, location_unresolved FROM assets WHERE id=?", flagged)["location_unresolved"] == 1 and c.get(f"/assets/{flagged}").status_code == 200

    # issue -> condition Faulty; resolve -> Working
    post(c, f"/assets/{pc}/issues", {"title": "Network weak", "type": "Network", "priority": "High", "mark_faulty": "1"})
    a = row("SELECT condition, status FROM assets WHERE id=?", pc)
    assert (a["condition"], a["status"]) == ("Faulty", "Faulty")
    iid = row("SELECT id FROM issues WHERE asset_id=?", pc)["id"]
    post(c, f"/issues/{iid}/resolve", {"resolution": "Replaced cable", "mark_working": "1"})
    post(c, f"/assets/{pc}/services", {"date": "2026-01-01", "type": "Repair", "next_due": "2026-06-01", "condition": "Working"})
    a = row("SELECT condition, status, next_service_due FROM assets WHERE id=?", pc)
    assert (a["condition"], a["status"], a["next_service_due"]) == ("Working", "In store", "2026-06-01")

    # duplicate with parts: new PC gets its own copy of the RAM and its own tag
    r = post(c, f"/assets/new?copy={pc}", {"name": "Test PC 2", "managing_dept_id": dept("CSE"), "room_id": "1", "copy_parts_from": str(pc)}, "/assets/new")
    pc2 = int(r.location.rsplit("/", 1)[1])
    assert [p["name"] for p in row_all("SELECT name FROM assets WHERE parent_id=?", pc2)] == ["Test RAM"]
    assert len({t["tag"] for t in row_all("SELECT tag FROM assets")}) == row("SELECT COUNT(*) n FROM assets")["n"]

    # depreciation
    half = (date.today() - timedelta(days=round(365.25 * 2.5))).isoformat()
    assert abs(m.book_value(1000, half, 5) - 500) < 1 and m.book_value(1000, None, 5) == 1000 and m.book_value(1000, "2000-01-01", 5) == 0

    # stock check sets the verification result
    post(c, "/rooms/1/audit", {"found": str(pc)})
    assert row("SELECT verification, last_audit FROM assets WHERE id=?", pc)["verification"] == "Verified"
    assert row("SELECT verification FROM assets WHERE id=?", ram)["verification"] == "Not found"
    assert row("SELECT 1 FROM activity WHERE asset_id=? AND text='Not found in stock check' AND event_type='Not found'", ram)

    # custody: issue to a department, a second person with a stale page is refused, return to stock
    page = c.get(f"/assets/{pc}").get_data(as_text=True)
    assert 'name="expected" value="0"' in page
    post(c, f"/assets/{pc}/custody", {"to": "Department", "department_id": dept("ECE"), "reason": "New lab", "expected": "0", "event_date": date.today().isoformat()}, f"/assets/{pc}")
    a = row("SELECT lifecycle_stage, using_dept_id, status FROM assets WHERE id=?", pc)
    assert (a["lifecycle_stage"], a["using_dept_id"], a["status"]) == ("Deployed", int(dept("ECE")), "Working")
    assert row("SELECT COUNT(*) n FROM custody WHERE asset_id=? AND ended_at IS NULL", pc)["n"] == 1
    r = post(c, f"/assets/{pc}/custody", {"to": "Department", "department_id": dept("Mechanical"), "reason": "Mine", "expected": "0"}, f"/assets/{pc}")
    assert b"Someone else" in c.get(r.location, follow_redirects=True).data and row("SELECT using_dept_id FROM assets WHERE id=?", pc)["using_dept_id"] == int(dept("ECE"))
    assert b"Choose the department" in c.get(post(c, f"/assets/{pc}/custody", {"to": "Department", "reason": "x", "expected": str(row("SELECT id FROM custody WHERE asset_id=? AND ended_at IS NULL", pc)["id"])}, f"/assets/{pc}").location, follow_redirects=True).data
    future = (date.today() + timedelta(days=3)).isoformat()
    assert b"in the future" in c.get(post(c, f"/assets/{pc}/custody", {"to": "Stockroom", "reason": "x", "event_date": future, "expected": str(row("SELECT id FROM custody WHERE asset_id=? AND ended_at IS NULL", pc)["id"])}, f"/assets/{pc}").location, follow_redirects=True).data
    cur = row("SELECT id FROM custody WHERE asset_id=? AND ended_at IS NULL", pc)["id"]
    post(c, f"/assets/{pc}/custody", {"to": "Stockroom", "reason": "Lab closed", "expected": str(cur), "event_date": (date.today() - timedelta(days=2)).isoformat()}, f"/assets/{pc}")
    assert row("SELECT lifecycle_stage, using_dept_id FROM assets WHERE id=?", pc)["lifecycle_stage"] == "In stock"
    ev = row("SELECT event_type, event_date, at FROM activity WHERE asset_id=? AND event_type='Return'", pc)
    assert ev["event_date"] == (date.today() - timedelta(days=2)).isoformat() and ev["event_date"] != ev["at"][:10]  # real date vs entry date

    # lend and return through the quick form
    post(c, f"/assets/{pc2}/lend", {"assigned_to": "Dr. X", "assigned_due": "2020-01-01"})
    assert b"Dr. X" in c.get("/loans").data and b"overdue" in c.get("/loans").data
    post(c, f"/assets/{pc2}/lend", {"action": "return"})
    assert b"Dr. X" not in c.get("/loans").data and row("SELECT lifecycle_stage FROM assets WHERE id=?", pc2)["lifecycle_stage"] == "In stock"

    # retirement: proposal, a different approver, disposal evidence, data erased for computers
    victim = asset_id("L2-PC-06")
    login(c, "lab")
    post(c, f"/assets/{victim}/lifecycle", {"to_stage": "Retirement proposed", "reason": "Beyond repair"}, f"/assets/{victim}")
    assert row("SELECT lifecycle_stage FROM assets WHERE id=?", victim)["lifecycle_stage"] == "Retirement proposed"
    r = post(c, f"/assets/{victim}/lifecycle", {"to_stage": "Retirement approved", "reason": "ok"}, f"/assets/{victim}")
    assert b"Only an HOD or admin" in c.get(r.location, follow_redirects=True).data
    assert c.get(f"/assets/{asset_id('Kiosk PC')}").status_code == 404  # Library's asset: not in the CSE lab assistant's department
    login(c, "hod")
    post(c, f"/assets/{victim}/lifecycle", {"to_stage": "Retirement approved", "reason": "Approved by committee"}, f"/assets/{victim}")
    assert row("SELECT lifecycle_stage FROM assets WHERE id=?", victim)["lifecycle_stage"] == "Retirement approved"
    login(c, "admin")
    post(c, f"/assets/{victim}/lifecycle", {"to_stage": "Awaiting disposal", "reason": "Listed for e-waste"}, f"/assets/{victim}")
    r = post(c, f"/assets/{victim}/lifecycle", {"to_stage": "Disposed", "reason": "Collected"}, f"/assets/{victim}")
    assert b"disposal evidence" in c.get(r.location, follow_redirects=True).data
    r = post(c, f"/assets/{victim}/lifecycle", {"to_stage": "Disposed", "reason": "Collected", "evidence": "EW-1042"}, f"/assets/{victim}")
    assert b"data was erased" in c.get(r.location, follow_redirects=True).data
    post(c, f"/assets/{victim}/lifecycle", {"to_stage": "Disposed", "reason": "Collected", "evidence": "EW-1042", "data_erased": "1"}, f"/assets/{victim}")
    a = row("SELECT lifecycle_stage, status FROM assets WHERE id=?", victim)
    assert (a["lifecycle_stage"], a["status"]) == ("Disposed", "Scrapped")
    r = post(c, f"/assets/{victim}/lifecycle", {"to_stage": "In stock", "reason": "undo"}, f"/assets/{victim}")
    assert b"final" in c.get(r.location, follow_redirects=True).data or b"it can only go to" in c.get(r.location, follow_redirects=True).data
    # the same person can't propose and approve
    other = asset_id("PA System")
    post(c, f"/assets/{other}/lifecycle", {"to_stage": "Retirement proposed", "reason": "Old"}, f"/assets/{other}")
    r = post(c, f"/assets/{other}/lifecycle", {"to_stage": "Retirement approved", "reason": "self approve"}, f"/assets/{other}")
    assert b"different person" in c.get(r.location, follow_redirects=True).data

    # tags: only an admin, with a reason; the old tag stays searchable and is never handed out again
    oldtag = row("SELECT tag FROM assets WHERE id=?", pc2)["tag"]
    login(c, "tech")
    form = {"name": "Test PC 2", "managing_dept_id": dept("CSE"), "room_id": "1", "tag": "CSE-NEW-1"}
    assert b"Only an admin can correct a tag" in post(c, f"/assets/{pc2}/edit", form, f"/assets/{pc2}/edit").data
    login(c, "admin")
    assert b"Say why the tag" in post(c, f"/assets/{pc2}/edit", form, f"/assets/{pc2}/edit").data
    post(c, f"/assets/{pc2}/edit", {**form, "tag_reason": "Label was printed wrong"}, f"/assets/{pc2}/edit")
    assert row("SELECT tag FROM assets WHERE id=?", pc2)["tag"] == "CSE-NEW-1"
    assert str(pc2).encode() and f"/assets/{pc2}".encode() in c.get(f"/assets?q={oldtag}").data
    with m.app.app_context():
        m.db().execute("UPDATE counters SET value=? WHERE name='tag'", (int(oldtag.split("-")[1]) - 1,))
        m.db().commit()
        assert m.next_tag() != oldtag  # the counter was wound back on purpose; the old tag is still refused

    # old codes and other systems' IDs
    post(c, f"/assets/{pc2}/identifiers", {"kind": "legacy_code", "value": "REC/MECH/LAB1/P07"}, f"/assets/{pc2}")
    assert f"/assets/{pc2}".encode() in c.get("/assets?q=P07").data
    assert b"already belongs" in c.get(post(c, f"/assets/{pc}/identifiers", {"kind": "legacy_code", "value": "REC/MECH/LAB1/P07"}, f"/assets/{pc}").location, follow_redirects=True).data
    assert b"which system" in c.get(post(c, f"/assets/{pc}/identifiers", {"kind": "external", "value": "X1"}, f"/assets/{pc}").location, follow_redirects=True).data

    # archive instead of delete: needs a reason, keeps the record and the tag
    assert b"Archive or move its parts" in c.get(post(c, f"/assets/{pc2}/delete", {"reason": "dup"}, f"/assets/{pc2}").location, follow_redirects=True).data
    child = row("SELECT id FROM assets WHERE parent_id=?", pc2)["id"]
    assert b"Say why" in c.get(post(c, f"/assets/{child}/delete", {}, f"/assets/{child}").location, follow_redirects=True).data
    post(c, f"/assets/{child}/delete", {"reason": "entered by mistake"}, f"/assets/{child}")
    post(c, f"/assets/{pc2}/delete", {"reason": "entered twice"}, f"/assets/{pc2}")
    assert row("SELECT archived_at, status FROM assets WHERE id=?", pc2)["status"] == "Scrapped"
    assert f"/assets/{pc2}".encode() not in c.get("/assets").data and f"/assets/{pc2}".encode() in c.get("/assets?archived=1").data
    assert row("SELECT COUNT(*) n FROM activity WHERE asset_id=?", pc2)["n"] >= 3

    # department scope: enforced on the server
    ece = asset_id("Digital Storage Oscilloscope")
    cse = asset_id("L1-PC-01")
    for who in ("staff", "hod", "lab"):
        login(c, who)
        assert c.get(f"/assets/{ece}").status_code == 404, who
        assert c.get(f"/assets/{cse}").status_code == 200, who
        assert f"/assets/{ece}".encode() not in c.get("/assets?q=Oscilloscope").data
        assert c.get(f"/labels?asset={ece}").data.count(b"Oscilloscope") == 0
    login(c, "staff")
    assert post(c, f"/assets/{cse}/issues", {"title": "Slow"}, f"/assets/{cse}").status_code == 302  # a custodian can report
    assert post(c, f"/assets/{cse}/services", {"type": "Repair"}, f"/assets/{cse}").status_code == 403
    assert post(c, f"/assets/{cse}/custody", {"to": "Stockroom", "reason": "x", "expected": "0"}, f"/assets/{cse}").status_code == 403
    login(c, "lab")
    assert post(c, f"/assets/{cse}/custody", {"to": "Stockroom", "reason": "Lab closed", "expected": str(row("SELECT id FROM custody WHERE asset_id=? AND ended_at IS NULL", cse)["id"])}, f"/assets/{cse}").status_code == 302
    assert c.get("/assets/new").status_code == 403 and c.get("/settings").status_code == 403
    login(c, "auditor")
    assert c.get(f"/assets/{ece}").status_code == 200 and post(c, f"/assets/{ece}/issues", {"title": "x"}, f"/assets/{ece}").status_code in (302, 403)
    assert c.get("/assets/new").status_code == 403
    login(c, "purchase")
    assert c.get(f"/assets/{ece}").status_code == 200 and c.get(f"/assets/{ece}/edit").status_code == 403

    # roles: only a super admin hands out admin roles; extra roles add abilities
    login(c, "admin")
    r = post(c, "/users/add", {"name": "New", "username": "newadmin", "role": "admin", "password": "newpass123"}, "/users")
    assert row("SELECT 1 FROM users WHERE username='newadmin'") is None
    post(c, "/users/add", {"name": "Dual", "username": "dual", "role": "custodian", "extra_roles": ["lab_assistant"], "department_id": dept("ECE"), "password": "dualpass123"}, "/users")
    assert row("SELECT extra_roles FROM users WHERE username='dual'")["extra_roles"] == "lab_assistant"
    login(c, "dual", "dualpass123")
    assert c.get(f"/assets/{ece}").status_code == 200 and c.get(f"/assets/{cse}").status_code == 404
    assert post(c, f"/assets/{ece}/custody", {"to": "Person", "person": "Dr. Z", "reason": "loan", "expected": str(row("SELECT id FROM custody WHERE asset_id=? AND ended_at IS NULL", ece)["id"])}, f"/assets/{ece}").status_code == 302
    assert row("SELECT custodian FROM assets WHERE id=?", ece)["custodian"] == "Dr. Z"
    login(c, "super")
    post(c, "/users/add", {"name": "New", "username": "newadmin", "role": "admin", "password": "newpass123"}, "/users")
    assert row("SELECT 1 FROM users WHERE username='newadmin'")


def row_all(sql, *args):
    with m.app.app_context():
        return m.q(sql, args)


def test_tags_are_unique_under_concurrency():
    m.DB = os.path.join(tempfile.mkdtemp(), "c.db")
    m.init_db()
    tags, lock = [], threading.Lock()

    def worker():
        for _ in range(15):
            with m.app.test_request_context():
                t = m.next_tag()
                m.db().execute("INSERT INTO assets(name, tag, lifecycle_stage) VALUES ('x', ?, 'In stock')", (t,))
                m.db().commit()
                with lock:
                    tags.append(t)

    ts = [threading.Thread(target=worker) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(tags) == 45 and len(set(tags)) == 45


def test_old_database_upgrades():
    """A database from the previous release (3 roles, one status, lending columns) is upgraded in place."""
    old = os.path.join(tempfile.mkdtemp(), "old.db")
    with sqlite3.connect(old) as c:
        c.executescript("""
          CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL, password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin','technician','staff')));
          CREATE TABLE buildings (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, code TEXT);
          CREATE TABLE rooms (id INTEGER PRIMARY KEY, building_id INTEGER NOT NULL REFERENCES buildings(id), name TEXT NOT NULL, type TEXT, floor TEXT, UNIQUE (building_id, name));
          CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, life_years INTEGER DEFAULT 5);
          CREATE TABLE assets (id INTEGER PRIMARY KEY, tag TEXT UNIQUE, name TEXT NOT NULL, category_id INTEGER, room_id INTEGER, parent_id INTEGER,
            status TEXT NOT NULL DEFAULT 'Working', brand TEXT, model TEXT, serial_no TEXT, ip_address TEXT, mac_address TEXT, purchase_date TEXT,
            warranty_until TEXT, cost REAL, vendor TEXT, notes TEXT, next_service_due TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_audit TEXT, assigned_to TEXT, assigned_due TEXT);
          CREATE TABLE activity (id INTEGER PRIMARY KEY, asset_id INTEGER REFERENCES assets(id) ON DELETE CASCADE, user_id INTEGER, text TEXT NOT NULL, at TEXT DEFAULT CURRENT_TIMESTAMP);
          INSERT INTO users VALUES (1, 'admin', 'Admin', 'x', 'admin'), (2, 'tech', 'Tech', 'x', 'technician'), (3, 'staff', 'Staff', 'x', 'staff');
          INSERT INTO assets(id, tag, name, status, last_audit) VALUES (1, 'AMS-00001', 'Working PC', 'Working', '2026-01-01'),
            (2, 'AMS-00002', 'Faulty PC', 'Faulty', NULL), (3, 'AMS-00003', 'In repair', 'Under service', NULL),
            (4, 'AMS-00004', 'Spare', 'In store', NULL), (5, 'AMS-00005', 'Scrap', 'Scrapped', NULL);
          UPDATE assets SET assigned_to='Dr. Meena', assigned_due='2026-02-01' WHERE id=1;""")
    m.DB = old
    m.init_db()
    m.init_db()  # idempotent
    with sqlite3.connect(old) as c:
        c.row_factory = sqlite3.Row
        roles = {r["username"]: r["role"] for r in c.execute("SELECT username, role FROM users")}
        assert roles == {"admin": "super_admin", "tech": "technician", "staff": "custodian"}
        st = {r["id"]: (r["lifecycle_stage"], r["condition"], r["status"]) for r in c.execute("SELECT * FROM assets")}
        assert st[1] == ("Deployed", "Working", "Working") and st[2] == ("Deployed", "Faulty", "Faulty")
        assert st[3][0] == "Under maintenance" and st[4][0] == "In stock" and st[5][0] == "Disposed"
        assert c.execute("SELECT verification FROM assets WHERE id=1").fetchone()[0] == "Verified"
        cu = c.execute("SELECT * FROM custody WHERE asset_id=1 AND ended_at IS NULL").fetchone()
        assert cu["person"] == "Dr. Meena" and cu["due_date"] == "2026-02-01"
        assert c.execute("SELECT assigned_to FROM assets WHERE id=1").fetchone()[0] is None
        assert c.execute("SELECT value FROM counters WHERE name='tag'").fetchone()[0] == 5
        assert c.execute("SELECT COUNT(*) FROM departments").fetchone()[0] > 10
        c.execute("INSERT INTO users(username, name, password_hash, role) VALUES ('h', 'H', 'x', 'hod')")  # the new roles are allowed now


def upload(c, name, data):
    r = c.post("/imports/new", data={"csrf": csrf(c, "/imports/new"), "file": (io.BytesIO(data), name)}, content_type="multipart/form-data")
    return r, int(re.search(r"/imports/(\d+)/map", r.location).group(1)) if r.status_code == 302 else None


def guessed(page):
    out = {}
    for i, blk in re.findall(r'<select name="col_(\d+)"[^>]*>(.*?)</select>', page, re.S):
        sel = re.search(r'<option value="(\w+)" selected', blk)
        out[int(i)] = sel.group(1) if sel else "ignore"
    return out


def map_step(c, bid, **kw):
    page = c.get(f"/imports/{bid}/map").get_data(as_text=True)
    cols = {f"col_{i}": t for i, t in guessed(page).items()}
    form = {"header_row": re.search(r'name="header_row"[^>]*value="(\d+)"', page).group(1), "source_system": "College ERP", "acquisition_basis": "Opening stock (old record)",
            "cost_basis": "Per unit", "default_stage": "In stock", "create_rooms": "1", "department_id": "", "building_id": "", **cols, **kw}
    return post(c, f"/imports/{bid}/map", form, f"/imports/{bid}/map")


def test_import_wizard():
    m.DB = os.path.join(tempfile.mkdtemp(), "w.db")
    m.UPLOADS = os.path.join(os.path.dirname(m.DB), "uploads")
    m.init_db()
    m.seed_demo()
    c = m.app.test_client()
    here = os.path.dirname(os.path.abspath(__file__))
    login(c, "staff")
    assert c.get("/imports").status_code == 403 and c.get("/imports/new").status_code == 403
    login(c, "admin")
    data = open(os.path.join(here, "samples", "allotment_sample.csv"), "rb").read()
    assert b"Only .csv and .xlsx" in upload(c, "x.xls", b"abc")[0].data
    assert b"real Excel" in upload(c, "x.xlsx", b"abc")[0].data

    # 1. upload keeps the original; the columns of the college's allotment report are recognised
    r, bid = upload(c, "allotment.csv", data)
    b = row("SELECT * FROM import_batches WHERE id=?", bid)
    stored = os.path.join(m.UPLOADS, "imports", b["stored_file"])
    assert b["sha256"] and open(stored, "rb").read() == data
    g = guessed(c.get(f"/imports/{bid}/map").get_data(as_text=True))
    assert {"name", "legacy_code", "department", "room", "allot_status"} <= set(g.values())

    # 2. unknown categories and departments need a decision once
    r = map_step(c, bid, save_mapping="ERP allotment report")
    assert r.location.endswith(f"/imports/{bid}/values")
    vals = c.get(r.location).get_data(as_text=True)
    assert "AMC" in vals and "Department of Mechanical Engineering" in vals and "ALLOTED" not in vals  # ALLOTED is understood
    todo = re.findall(r'name="v_(\d+)" aria-label="What ([^"]+) is"', vals)
    answers = {f"v_{i}": ("__skip__" if v == "AMC" else "Mechanical" if v.startswith("Department of") else "__new__") for i, v in todo}
    r = post(c, f"/imports/{bid}/values", answers, f"/imports/{bid}/values")
    assert r.location.endswith(f"/imports/{bid}/preview")

    # 3. preview: nothing added yet; placeholders are blanks; a code repeated inside the file is not added twice
    pv = c.get(f"/imports/{bid}/preview").get_data(as_text=True)
    assert "Rows read" in pv and "Repeated inside this file" in pv and "-No Value-" not in pv
    assert row("SELECT COUNT(*) n FROM staged_rows WHERE batch_id=? AND decision='Create'", bid)["n"] == 7
    assert row("SELECT COUNT(*) n FROM staged_rows WHERE batch_id=?", bid)["n"] == 9  # the 5 blank lines are ignored
    assert row("SELECT COUNT(*) n FROM assets WHERE import_batch_id=?", bid)["n"] == 0
    post(c, f"/imports/{bid}/approve", {}, f"/imports/{bid}/preview")  # must tick the review box
    assert row("SELECT COUNT(*) n FROM assets WHERE import_batch_id=?", bid)["n"] == 0
    post(c, f"/imports/{bid}/approve", {"reviewed": "1"}, f"/imports/{bid}/preview")
    made = row_all("SELECT * FROM assets WHERE import_batch_id=? ORDER BY id", bid)
    assert len(made) == 7 and len({a["tag"] for a in made}) == 7 and all(a["tag"].startswith("AMS-") for a in made)
    a1 = made[0]
    assert (a1["lifecycle_stage"], a1["legacy_code"], a1["acquisition_basis"], a1["managing_dept_id"]) == ("Deployed", "REC / MECH/T.E.LAB L 1/P 07", "Opening stock (old record)", int(dept("Mechanical")))
    assert row("SELECT value FROM asset_ids WHERE asset_id=? AND kind='external' AND source='College ERP'", a1["id"])["value"] == "304"
    cu = row("SELECT * FROM custody WHERE asset_id=? AND ended_at IS NULL", a1["id"])
    assert cu["holder_type"] == "Department" and cu["started_on"] == "2026-09-11" and row("SELECT source_json FROM assets WHERE id=?", a1["id"])["source_json"]
    assert row("SELECT person FROM custody WHERE asset_id=? AND ended_at IS NULL", made[3]["id"])["person"] == "Dr. A. Kumar"
    assert row("SELECT 1 FROM rooms WHERE name='Thermal Lab 1'") and row("SELECT 1 FROM categories WHERE name='A/V Equipments'")
    assert f"/assets/{a1['id']}".encode() in c.get("/assets?q=T.E.LAB").data  # found by the old code
    assert row("SELECT status FROM import_batches WHERE id=?", bid)["status"] == "Approved"
    assert c.get(f"/imports/{bid}/errors.csv").data.startswith(b"row,decision,problems")

    # 4. the same file again: noticed, answers remembered, everything is a duplicate so nothing doubles
    r, bid2 = upload(c, "allotment-again.csv", data)
    assert b"already uploaded" in c.get(r.location, follow_redirects=True).data
    r = map_step(c, bid2)
    assert r.location.endswith("/preview"), r.location
    assert row("SELECT COUNT(*) n FROM staged_rows WHERE batch_id=? AND decision='Create'", bid2)["n"] == 0
    assert row("SELECT COUNT(*) n FROM staged_rows WHERE batch_id=? AND dup_asset_id IS NOT NULL", bid2)["n"] == 8
    post(c, f"/imports/{bid2}/approve", {"reviewed": "1"}, f"/imports/{bid2}/preview")
    assert row("SELECT COUNT(*) n FROM assets WHERE legacy_code='REC / MECH/T.E.LAB L 1/P 07'")["n"] == 1

    # 5. a duplicate can fill the blanks of the existing asset, never overwriting what is there
    with m.app.app_context():
        m.db().execute("UPDATE assets SET brand=NULL WHERE id=?", (a1["id"],))
        m.db().commit()
    r, bid3 = upload(c, "with-brand.csv", b"Asset,Asset Code,Make,Category,Department\nOther name,REC / MECH/T.E.LAB L 1/P 07,Kirloskar,A/V Equipments,Mechanical\n")
    map_step(c, bid3)
    sid = row("SELECT id FROM staged_rows WHERE batch_id=?", bid3)["id"]
    post(c, f"/imports/{bid3}/preview", {f"d_{sid}": "Update"}, f"/imports/{bid3}/preview")
    post(c, f"/imports/{bid3}/approve", {"reviewed": "1"}, f"/imports/{bid3}/preview")
    after = row("SELECT name, brand FROM assets WHERE id=?", a1["id"])
    assert after["brand"] == "Kirloskar" and after["name"] == a1["name"]  # blank filled, existing name kept

    # 6. "20 computers" stays a batch; a day/month date is flagged; a total line is skipped; confirming units gives tags and per-unit cost
    r, bid4 = upload(c, "purchases.csv", open(os.path.join(here, "samples", "purchase_batches_sample.csv"), "rb").read())
    map_step(c, bid4)
    pv = c.get(f"/imports/{bid4}/preview").get_data(as_text=True)
    assert "read as day/month/year" in pv and "Looks like a total line" in pv
    assert row("SELECT COUNT(*) n FROM staged_rows WHERE batch_id=? AND kind='Batch' AND decision='Create'", bid4)["n"] == 2
    assert row("SELECT COUNT(*) n FROM assets WHERE import_batch_id=?", bid4)["n"] == 0
    post(c, f"/imports/{bid4}/approve", {"reviewed": "1"}, f"/imports/{bid4}/preview")
    pb = row("SELECT * FROM purchase_batches WHERE quantity=20")
    assert pb["unit_cost"] == 52000 and pb["units_created"] == 0 and pb["cost_basis"] == "Per unit"
    assert b"Confirm units" in c.get("/batches").data
    assert b"1 to 20" in c.get(post(c, f"/batches/{pb['id']}/units", {"count": "25", "department_id": dept("CSE")}, "/batches").location, follow_redirects=True).data
    post(c, f"/batches/{pb['id']}/units", {"count": "12", "department_id": dept("CSE"), "room_id": "1"}, "/batches")
    units = row_all("SELECT * FROM assets WHERE notes=?", f"Created from purchase batch #{pb['id']}")
    assert len(units) == 12 and len({u["tag"] for u in units}) == 12 and units[0]["cost"] == 52000 and units[0]["cost_basis"] == "Per unit"
    assert row("SELECT status FROM purchase_batches WHERE id=?", pb["id"])["status"] == "Partly received"
    post(c, f"/batches/{pb['id']}/units", {"count": "8", "department_id": dept("CSE"), "room_id": "1"}, "/batches")
    assert row("SELECT status FROM purchase_batches WHERE id=?", pb["id"])["status"] == "Units created"

    # 7. a whole-purchase cost is never copied onto each asset
    r, bid5 = upload(c, "whole.csv", b"Asset,Cost,Department,Room\nProjector A,500000,CSE,Lab Z\nProjector B,500000,CSE,Lab Z\n")
    map_step(c, bid5, cost_basis="Whole purchase")
    post(c, f"/imports/{bid5}/approve", {"reviewed": "1"}, f"/imports/{bid5}/preview")
    got = row_all("SELECT cost FROM assets WHERE import_batch_id=?", bid5)
    assert len(got) == 2 and all(a["cost"] is None for a in got)

    # 8. Excel: title rows above the headings, a spare sheet, real dates, a total line
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Allotments"
    ws.append(["Mechanical Dept stock list"])
    ws.append([])
    ws.append(["Item", "Code", "Dept", "Room", "Cost", "Purchased"])
    ws.append(["Lathe", "OLD-LATHE-1", "Mechanical", "Workshop", 250000, date(2024, 5, 1)])
    ws.append(["Total", None, None, None, 250000, None])
    wb.create_sheet("Notes").append(["not data"])
    buf = io.BytesIO()
    wb.save(buf)
    r, bid6 = upload(c, "stock.xlsx", buf.getvalue())
    assert row("SELECT header_row FROM import_batches WHERE id=?", bid6)["header_row"] == 3 and b"Allotments" in c.get(r.location).data
    r = map_step(c, bid6)
    assert r.status_code == 302
    pv = c.get(f"/imports/{bid6}/preview").get_data(as_text=True)
    assert "Lathe" in pv and "Looks like a total line" in pv
    map_step(c, bid6, expected_rows="2", expected_cost="999")  # the preview compares with what staff expected
    assert "differs" in c.get(f"/imports/{bid6}/preview").get_data(as_text=True)

    # 9. reversal works only while the imported assets are untouched; a corrected file can then be imported again
    post(c, f"/imports/{bid5}/reverse", {}, f"/imports/{bid5}/preview")
    assert row("SELECT COUNT(*) n FROM assets WHERE import_batch_id=? AND archived_at IS NULL", bid5)["n"] == 0
    assert row("SELECT status FROM import_batches WHERE id=?", bid5)["status"] == "Reversed"
    busy = made[1]["id"]
    cur = row("SELECT id FROM custody WHERE asset_id=? AND ended_at IS NULL", busy)["id"]
    post(c, f"/assets/{busy}/custody", {"to": "Stockroom", "reason": "Lab closed", "expected": str(cur)}, f"/assets/{busy}")
    assert b"Can&#39;t reverse" in c.get(post(c, f"/imports/{bid}/reverse", {}, f"/imports/{bid}/preview").location, follow_redirects=True).data
    assert row("SELECT status FROM import_batches WHERE id=?", bid)["status"] == "Approved"
    r, bid7 = upload(c, "whole-again.csv", b"Asset,Cost,Department,Room\nProjector A,500000,CSE,Lab Z\n")
    map_step(c, bid7, cost_basis="Per unit")
    assert row("SELECT COUNT(*) n FROM staged_rows WHERE batch_id=? AND decision='Create'", bid7)["n"] == 1


def test_documents():
    m.DB = os.path.join(tempfile.mkdtemp(), "d.db")
    m.UPLOADS = os.path.join(os.path.dirname(m.DB), "uploads")
    m.init_db()
    m.seed_demo()
    c = m.app.test_client()
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF"
    cse, ece = asset_id("L1-PC-01"), asset_id("Digital Storage Oscilloscope")

    def add(aid, name, data, **kw):
        return c.post(f"/assets/{aid}/documents", data={"csrf": csrf(c, f"/assets/{aid}"), "file": (io.BytesIO(data), name), **kw}, content_type="multipart/form-data")

    login(c, "purchase")
    assert b"PO / invoice" in c.get(f"/assets/{cse}").data
    r = add(cse, "po-0412.pdf", pdf, kind="Purchase order", reference="PO/2023/0412", description="Original order")
    assert r.status_code == 302
    d1 = row("SELECT * FROM documents ORDER BY id DESC LIMIT 1")
    assert d1["sha256"] and d1["version"] == 1 and d1["file"].endswith(".pdf") and open(os.path.join(m.UPLOADS, "docs", d1["file"]), "rb").read() == pdf
    for bad, name in [(b"<html><script>alert(1)</script></html>", "x.pdf"), (b"MZ\x90\x00 binary", "x.exe"), (b"<svg onload=alert(1)>", "x.png"), (b"%PDF-1.4", "x.html"), (b"x" * (9 * 1024 * 1024), "big.pdf")]:
        r = add(cse, name, bad, kind="Invoice")
        assert b"are refused" in c.get(r.location, follow_redirects=True).data or r.status_code == 413, name
    assert row("SELECT COUNT(*) n FROM documents")["n"] == 1

    # a new version keeps the old one
    add(cse, "po-0412-signed.pdf", pdf + b"\n% signed", replaces=str(d1["id"]))
    d2 = row("SELECT * FROM documents ORDER BY id DESC LIMIT 1")
    assert (d2["version"], d2["supersedes_id"], d2["kind"]) == (2, d1["id"], "Purchase order")
    page = c.get(f"/assets/{cse}").get_data(as_text=True)
    assert "po-0412-signed.pdf" in page and "Older versions" in page and "po-0412.pdf" in page
    assert c.get(f"/documents/{d1['id']}").data == pdf and c.get(f"/documents/{d1['id']}").headers["Content-Disposition"].startswith("inline")

    # insurance papers are for admins, purchase and auditors only
    add(cse, "policy.pdf", pdf, kind="Insurance")
    ins = row("SELECT id FROM documents WHERE kind='Insurance'")["id"]
    add(ece, "ece-invoice.pdf", pdf, kind="Invoice")
    ece_doc = row("SELECT id FROM documents WHERE original_name='ece-invoice.pdf'")["id"]
    assert c.get(f"/documents/{ins}").status_code == 200
    login(c, "auditor")
    assert c.get(f"/documents/{ins}").status_code == 200 and c.get("/documents").status_code == 200
    assert add(cse, "no.pdf", pdf).status_code == 403  # read only
    login(c, "hod")  # CSE head: sees CSE papers, not insurance, not other departments'
    assert c.get(f"/documents/{d1['id']}").status_code == 200
    assert c.get(f"/documents/{ins}").status_code == 403 and c.get(f"/documents/{ece_doc}").status_code == 404
    assert b"policy.pdf" not in c.get("/documents").data and b"ece-invoice.pdf" not in c.get("/documents").data and b"po-0412" in c.get("/documents").data
    assert b"policy.pdf" not in c.get(f"/assets/{cse}").data
    login(c, "staff")  # a custodian never sees purchase papers
    assert c.get(f"/documents/{d1['id']}").status_code == 403 and c.get("/documents").status_code == 403
    assert b"po-0412" not in c.get(f"/assets/{cse}").data
    login(c, "lab")
    assert add(cse, "no.pdf", pdf).status_code == 403
    login(c, "admin")
    assert b"po-0412-signed.pdf" in c.get("/documents?q=0412").data and b"policy.pdf" in c.get("/documents?kind=Insurance").data
    assert c.get("/documents/99999").status_code == 404


def test_specs_stock_licences_reports():
    m.DB = os.path.join(tempfile.mkdtemp(), "x.db")
    m.UPLOADS = os.path.join(os.path.dirname(m.DB), "uploads")
    m.init_db()
    m.seed_demo()
    c = m.app.test_client()
    login(c, "admin")

    # specification fields: a computer asks for RAM in GB; bad values are refused with a plain message
    desktop = str(row("SELECT id FROM categories WHERE name='Desktop computer'")["id"])
    switch = str(row("SELECT id FROM categories WHERE name='Network switch'")["id"])
    ram = row("SELECT id FROM attribute_defs WHERE name='RAM' AND category_id=?", int(desktop))["id"]
    managed = row("SELECT id FROM attribute_defs WHERE name='Managed'")["id"]
    form = c.get("/assets/new").get_data(as_text=True)
    assert 'id="specs"' in form and "fields.js" in form and '"RAM"' in form
    base = {"name": "Spec PC", "managing_dept_id": dept("CSE"), "room_id": "1", "category_id": desktop}
    assert b"RAM must be a number in GB" in post(c, "/assets/new", {**base, f"attr_{ram}": "lots"}, "/assets/new").data
    r = post(c, "/assets/new", {**base, f"attr_{ram}": "16", f"attr_{managed}": "Yes"}, "/assets/new")  # 'Managed' isn't a desktop field, so it is ignored
    aid = int(r.location.rsplit("/", 1)[1])
    assert row("SELECT value FROM attribute_values WHERE asset_id=? AND attr_id=?", aid, ram)["value"] == "16"
    assert row("SELECT COUNT(*) n FROM attribute_values WHERE asset_id=?", aid)["n"] == 1
    page = c.get(f"/assets/{aid}").get_data(as_text=True)
    assert "Specifications" in page and "16 GB" in page
    sw = int(post(c, "/assets/new", {**base, "category_id": switch, "name": "Spec switch", f"attr_{managed}": "Yes"}, "/assets/new").location.rsplit("/", 1)[1])
    assert b"must be one of" in post(c, "/assets/new", {**base, "category_id": switch, "name": "x", f"attr_{managed}": "Maybe"}, "/assets/new").data
    post(c, f"/assets/{aid}/edit", {**base, "category_id": switch, f"attr_{managed}": "No"}, f"/assets/{aid}/edit")  # change the kind: old values go
    assert row("SELECT COUNT(*) n FROM attribute_values WHERE asset_id=? AND attr_id=?", aid, ram)["n"] == 0
    assert row("SELECT value FROM attribute_values WHERE asset_id=? AND attr_id=?", aid, managed)["value"] == "No"
    # admins design the fields; a required one is enforced
    post(c, "/settings/fields/add", {"category_id": desktop, "name": "Warranty tag", "datatype": "text", "required": "1"}, "/settings/fields")
    assert b"Warranty tag is required" in post(c, "/assets/new", {**base, "name": "x2"}, "/assets/new").data
    assert b"already has a field" in c.get(post(c, "/settings/fields/add", {"category_id": desktop, "name": "Warranty tag", "datatype": "text"}, "/settings/fields").location, follow_redirects=True).data
    wt = row("SELECT id FROM attribute_defs WHERE name='Warranty tag'")["id"]
    post(c, f"/settings/fields/{wt}/delete", {}, "/settings/fields")
    assert row("SELECT 1 FROM attribute_defs WHERE id=?", wt) is None
    login(c, "tech")
    assert c.get("/settings/fields").status_code == 403

    # consumable stock: counted, never negative, every change kept
    login(c, "admin")
    r = post(c, "/stock/new", {"name": "HP 12A toner", "unit": "nos", "min_level": "2", "department_id": dept("CSE")}, "/stock")
    iid = int(r.location.rsplit("/", 1)[1])
    mv = lambda **kw: post(c, f"/stock/{iid}/move", {"event_date": date.today().isoformat(), **kw}, f"/stock/{iid}")
    mv(kind="Received", qty="10", reference="GRN-5")
    assert b"Only 10 nos in stock" in c.get(mv(kind="Issued", qty="11", reason="x").location, follow_redirects=True).data
    assert b"Say why" in c.get(mv(kind="Issued", qty="1").location, follow_redirects=True).data
    mv(kind="Issued", qty="9", reason="Office printers", department_id=dept("CSE"))
    assert row("SELECT COALESCE(SUM(qty),0) b FROM stock_movements WHERE item_id=?", iid)["b"] == 1
    assert b"low" in c.get("/stock").data
    mv(kind="Adjusted", qty="3", direction="up", reason="Recount")
    assert row("SELECT COALESCE(SUM(qty),0) b FROM stock_movements WHERE item_id=?", iid)["b"] == 4
    assert row("SELECT COUNT(*) n FROM stock_movements WHERE item_id=?", iid)["n"] == 3
    other = int(post(c, "/stock/new", {"name": "ECE soldering wire", "unit": "m", "department_id": dept("ECE")}, "/stock").location.rsplit("/", 1)[1])
    login(c, "staff")
    assert c.get(f"/stock/{iid}").status_code == 200 and c.get(f"/stock/{other}").status_code == 404  # CSE user: no ECE stock
    assert mv(kind="Received", qty="5").status_code == 403 and post(c, "/stock/new", {"name": "x"}, "/stock").status_code == 403
    login(c, "lab")
    assert mv(kind="Received", qty="5").status_code == 302

    # licences: keys only for purchase and admins, exports leave them out for others, renewals flagged
    login(c, "purchase")
    soon = (date.today() + timedelta(days=20)).isoformat()
    post(c, "/licences/add", {"product": "MATLAB Campus", "quantity": "100", "in_use": "120", "renewal_date": soon, "licence_type": "Subscription",
                              "metric": "Per user", "key_ref": "ABCD-1234-SECRET"}, "/licences")
    post(c, "/licences/add", {"product": "Old CAD", "quantity": "5", "renewal_date": "2020-01-01", "key_ref": "OLD-KEY"}, "/licences")
    page = c.get("/licences").get_data(as_text=True)
    assert "ABCD-1234-SECRET" in page and "over by 20" in page and "soon" in page and "expired" in page
    assert b"ABCD-1234-SECRET" in c.get("/licences?csv=1").data
    assert b"Quantity must be 1 or more" in c.get(post(c, "/licences/add", {"product": "x", "quantity": "0"}, "/licences").location, follow_redirects=True).data
    for who in ("hod", "tech", "auditor"):
        login(c, who)
        assert b"ABCD-1234-SECRET" not in c.get("/licences").data and b"ABCD-1234-SECRET" not in c.get("/licences?csv=1").data, who
        assert post(c, "/licences/add", {"product": "x", "quantity": "1"}, "/licences").status_code == 403
    login(c, "staff")
    assert c.get("/licences").status_code == 403
    login(c, "purchase")
    lid = row("SELECT id FROM licences WHERE product='Old CAD'")["id"]
    post(c, f"/licences/{lid}/update", {"product": "Old CAD", "quantity": "7", "in_use": "3"}, "/licences")
    assert row("SELECT quantity, key_ref FROM licences WHERE id=?", lid)["key_ref"] == "OLD-KEY"  # a blank key field keeps the saved key
    post(c, f"/licences/{lid}/archive", {}, "/licences")
    assert b"Old CAD" not in c.get("/licences").data

    # reports: counts by department, data quality, scoped for department users, CSV
    login(c, "admin")
    rp = c.get("/reports").get_data(as_text=True)
    assert "By managing department" in rp and "Data quality" in rp and "Location unknown" in rp and "allotment" in rp
    assert b"Old network switch" in c.get("/reports").data
    assert c.get("/reports?csv=quality").data.startswith(b"problem,tag,asset") and b"Old network switch" in c.get("/reports?csv=quality").data
    full = len(re.findall(r"<li><a class=\"mono\"", rp))
    login(c, "hod")
    scoped = c.get("/reports").get_data(as_text=True)
    assert "Mechanical" not in scoped and "Old network switch" not in scoped and "for your department only" in scoped
    login(c, "auditor")
    assert c.get("/reports").status_code == 200
    login(c, "staff")
    assert c.get("/reports").status_code == 200 and "Imports compared" not in c.get("/reports").get_data(as_text=True)
    assert full >= 1


def test_backup_and_restore():
    import backup
    import zipfile
    m.DB = os.path.join(tempfile.mkdtemp(), "b.db")
    m.UPLOADS = os.path.join(os.path.dirname(m.DB), "uploads")
    m.init_db()
    m.seed_demo()
    c = m.app.test_client()
    login(c, "admin")
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF"
    cse = asset_id("L1-PC-01")
    c.post(f"/assets/{cse}/documents", data={"csrf": csrf(c, f"/assets/{cse}"), "file": (io.BytesIO(pdf), "po.pdf"), "kind": "Purchase order"}, content_type="multipart/form-data")
    png = bytes.fromhex("89504e470d0a1a0a") + b"0" * 20
    c.post(f"/assets/{cse}/photos", data={"csrf": csrf(c, f"/assets/{cse}"), "photos": (io.BytesIO(png), "a.png")}, content_type="multipart/form-data")
    out = tempfile.mkdtemp()
    z = backup.make_backup(m.DB, m.UPLOADS, out)
    ok, problems, summary = backup.verify_backup(z)
    assert ok, problems
    assert summary["assets"] == row("SELECT COUNT(*) n FROM assets")["n"] and summary["files"] == 2
    # a damaged backup is caught
    bad = os.path.join(out, "bad.zip")
    with zipfile.ZipFile(z) as zin, zipfile.ZipFile(bad, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            zout.writestr(item.filename, data[:-3] + b"xyz" if item.filename.startswith("uploads/docs/") else data)
    ok, problems, _ = backup.verify_backup(bad)
    assert not ok and any("damaged" in p for p in problems)
    # a file the database points at but the backup lacks is reported
    gone = os.path.join(out, "gone.zip")
    with zipfile.ZipFile(z) as zin, zipfile.ZipFile(gone, "w") as zout:
        man = json.loads(zin.read("manifest.json"))
        for k in [k for k in man["files"] if k.startswith("uploads/docs/")]:
            del man["files"][k]
        for item in zin.infolist():
            if item.filename == "manifest.json":
                zout.writestr(item.filename, json.dumps(man))
            elif not item.filename.startswith("uploads/docs/"):
                zout.writestr(item.filename, zin.read(item.filename))
    ok, problems, _ = backup.verify_backup(gone)
    assert not ok and any("not in the backup" in p for p in problems)
    # restore into a spare folder; the restored database and files work; an existing database is never overwritten
    spare = os.path.join(tempfile.mkdtemp(), "spare")
    target = backup.restore_backup(z, spare)
    con = sqlite3.connect(target)
    assert con.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == row("SELECT COUNT(*) n FROM assets")["n"]
    assert len(os.listdir(os.path.join(spare, "uploads", "docs"))) == 1
    try:
        backup.restore_backup(z, spare)
        raise AssertionError("should refuse to overwrite")
    except FileExistsError:
        pass


def test_findings_bulk_parts_contracts_api():
    m.DB = os.path.join(tempfile.mkdtemp(), "m.db")
    m.UPLOADS = os.path.join(os.path.dirname(m.DB), "uploads")
    m.init_db()
    m.seed_demo()
    c = m.app.test_client()
    login(c, "admin")

    # scanning a label or typing a tag or an old code opens the asset; scoped users can't reach other departments' assets
    a1 = asset_id("L1-PC-01")
    tag = row("SELECT tag FROM assets WHERE id=?", a1)["tag"]
    assert c.get(f"/t/{tag}").location.endswith(f"/assets/{a1}") and c.get(f"/t/{tag.lower()}").location.endswith(f"/assets/{a1}")
    assert c.get("/t/REC/CSE/L1/PC-01").location.endswith(f"/assets/{a1}")  # old code
    assert "/assets?q=" in c.get("/t/NOPE-1").location
    ece_tag = row("SELECT tag FROM assets WHERE name='Function Generator'")["tag"]
    login(c, "staff")
    assert "/assets?q=" in c.get(f"/t/{ece_tag}").location

    # stock check: equipment seen in the wrong room or not registered at all is recorded, then decided
    login(c, "lab")
    other = asset_id("L2-PC-01")
    other_tag = row("SELECT tag FROM assets WHERE id=?", other)["tag"]
    post(c, "/rooms/1/audit", {"found": str(a1), "extra": f"{other_tag}\nHP printer on the side table, no label\n"}, "/rooms/1?audit=1")
    assert row("SELECT verification FROM assets WHERE id=?", other)["verification"] == "Wrong location"
    fs = row_all("SELECT * FROM check_findings ORDER BY id")
    assert [f["kind"] for f in fs] == ["Wrong location", "Unregistered"] and fs[1]["description"].startswith("HP printer")
    page = c.get("/findings").get_data(as_text=True)
    assert "HP printer" in page and "Wrong place" in page
    assert b"Say what was decided" in c.get(post(c, f"/findings/{fs[0]['id']}/resolve", {"how": "move", "note": ""}, "/findings").location, follow_redirects=True).data
    post(c, f"/findings/{fs[0]['id']}/resolve", {"how": "move", "note": "Moved during the lab rearrangement"}, "/findings")
    a = row("SELECT room_id, verification FROM assets WHERE id=?", other)
    assert (a["room_id"], a["verification"]) == (1, "Verified")
    post(c, f"/findings/{fs[1]['id']}/resolve", {"how": "dismiss", "note": "Belongs to a visitor"}, "/findings")
    assert row("SELECT status FROM check_findings WHERE id=?", fs[1]["id"])["status"] == "Dismissed"
    login(c, "staff")
    assert c.get("/findings").status_code == 403

    # bulk actions: a preview changes nothing; the run gives a result for every asset and leaves the failures alone
    login(c, "admin")
    spares = [asset_id("Spare 24-inch Monitor"), asset_id("Donated Desktop")]
    gone = asset_id("PA System")
    post(c, f"/assets/{gone}/delete", {"reason": "mistake"}, f"/assets/{gone}")  # archived: can't be moved
    ids = [str(i) for i in (*spares, gone)]
    pv = post(c, "/assets/bulk", {"ids": ids, "action": "department", "department_id": dept("ECE"), "reason": "Lab setup", "event_date": date.today().isoformat()}, "/assets")
    text = pv.get_data(as_text=True)
    assert "Preview: nothing has been changed yet" in text and "2 of 3 would go through" in text and "archived" in text.lower()
    assert all(row("SELECT lifecycle_stage FROM assets WHERE id=?", i)["lifecycle_stage"] == "In stock" for i in spares)
    ran = post(c, "/assets/bulk", {"ids": ids, "action": "department", "step": "run", "department_id": dept("ECE"), "reason": "Lab setup", "event_date": date.today().isoformat()}, "/assets")
    assert "2 of 3 done" in ran.get_data(as_text=True)
    assert all(row("SELECT lifecycle_stage, using_dept_id FROM assets WHERE id=?", i)["using_dept_id"] == int(dept("ECE")) for i in spares)
    no_reason = post(c, "/assets/bulk", {"ids": [str(spares[0])], "action": "stockroom"}, "/assets").get_data(as_text=True)
    assert "Say why" in no_reason and "0 of 1" in no_reason
    post(c, "/assets/bulk", {"ids": [str(spares[0])], "action": "verify", "step": "run"}, "/assets")
    assert row("SELECT verification FROM assets WHERE id=?", spares[0])["verification"] == "Verified"
    login(c, "staff")
    assert post(c, "/assets/bulk", {"ids": [str(a1)], "action": "verify"}, "/assets").status_code == 403
    login(c, "lab")  # a CSE lab assistant can't move another department's asset in bulk
    r = post(c, "/assets/bulk", {"ids": [str(asset_id("Function Generator"))], "action": "verify", "step": "run"}, "/assets").get_data(as_text=True)
    assert "0 of 1" in r and "not in your department" in r.lower()

    # replace a part: the old one stays in the history, the new one gets its own tag
    login(c, "admin")
    old = row("SELECT * FROM assets WHERE parent_id=? AND name='8 GB DDR4 RAM'", a1)
    assert b"Say why" in c.get(post(c, f"/assets/{old['id']}/replace", {"reason": " "}, f"/assets/{a1}").location, follow_redirects=True).data
    post(c, f"/assets/{old['id']}/replace", {"reason": "Faulty module", "brand": "Kingston", "serial_no": "KS-1"}, f"/assets/{a1}")
    assert row("SELECT archived_at, status FROM assets WHERE id=?", old["id"])["status"] == "Scrapped"
    new = row("SELECT * FROM assets WHERE parent_id=? AND brand='Kingston'", a1)
    assert new["tag"] != old["tag"] and new["room_id"] == old["room_id"] and new["managing_dept_id"] == old["managing_dept_id"]
    assert row("SELECT 1 FROM activity WHERE asset_id=? AND event_type='Part replaced'", a1) and b"replaced" in c.get(f"/assets/{a1}").data
    assert c.get(post(c, f"/assets/{a1}/replace", {"reason": "x"}, f"/assets/{a1}").location, follow_redirects=True).status_code == 200  # not a part: refused politely
    assert row("SELECT COUNT(*) n FROM assets WHERE name='L1-PC-01' AND archived_at IS NOT NULL")["n"] == 0

    # network interfaces, rack, campus
    sw = asset_id("Core Switch")
    post(c, f"/assets/{sw}/interfaces", {"name": "Gi1/0/12", "ip": "10.10.0.50", "vlan": "20", "connected_to": "L1-PC-02"}, f"/assets/{sw}")
    assert b"Gi1/0/12" in c.get(f"/assets/{sw}").data
    iid = row("SELECT id FROM asset_interfaces WHERE asset_id=?", sw)["id"]
    post(c, f"/interfaces/{iid}/delete", {}, f"/assets/{sw}")
    assert row("SELECT COUNT(*) n FROM asset_interfaces WHERE asset_id=?", sw)["n"] == 0
    post(c, f"/assets/{sw}/edit", {"name": "Core Switch", "managing_dept_id": dept("Central IT"), "room_id": "3", "rack": "Rack 2, U12"}, f"/assets/{sw}/edit")
    assert b"Rack 2, U12" in c.get(f"/assets/{sw}").data
    post(c, "/settings/buildings/add", {"name": "Annex", "campus": "North campus"}, "/settings")
    assert row("SELECT campus FROM buildings WHERE name='Annex'")["campus"] == "North campus"
    login(c, "lab")
    assert post(c, f"/assets/{sw}/interfaces", {"name": "x"}, f"/assets/{sw}").status_code == 403

    # contracts and the reminders page
    login(c, "purchase")
    soon = (date.today() + timedelta(days=30)).isoformat()
    post(c, "/contracts/add", {"kind": "AMC", "vendor": "Cisco partner", "reference": "AMC-77", "end_date": soon, "assets": f"{row('SELECT tag FROM assets WHERE id=?', sw)['tag']}, NOPE-9"}, "/contracts")
    assert row("SELECT COUNT(*) n FROM contract_assets")["n"] == 1
    assert row("SELECT 1 FROM contracts WHERE reference='AMC-77'")
    assert b"Give the end date" in c.get(post(c, "/contracts/add", {"kind": "AMC", "assets": ""}, "/contracts").location, follow_redirects=True).data
    rem = c.get("/reminders").get_data(as_text=True)
    assert "AMC-77" in rem and "Warranty ending" in rem and "Low stock" in rem
    login(c, "staff")
    rem = c.get("/reminders").get_data(as_text=True)
    assert "AMC-77" not in rem and c.get("/contracts").status_code == 403
    login(c, "tech")
    assert post(c, "/contracts/add", {"kind": "AMC", "end_date": soon}, "/contracts").status_code == 403

    # read-only API for other systems: off without a token, never gives costs, wrong tokens refused
    os.environ.pop("AMS_API_TOKEN", None)
    anon = m.app.test_client()
    assert anon.get(f"/api/v1/assets/{tag}").status_code == 404
    os.environ["AMS_API_TOKEN"] = "test-token-123"
    try:
        assert anon.get(f"/api/v1/assets/{tag}").status_code == 401
        assert anon.get(f"/api/v1/assets/{tag}", headers={"Authorization": "Bearer nope"}).status_code == 401
        ok = anon.get(f"/api/v1/assets/{tag}", headers={"Authorization": "Bearer test-token-123"})
        j = ok.get_json()
        assert ok.status_code == 200 and j["tag"] == tag and j["name"] == "L1-PC-01" and j["room"] == "Computer Lab 1" and j["building"] == "CSE Block"
        assert not {"cost", "vendor", "po_number", "key_ref", "notes"} & set(j)
        assert anon.get("/api/v1/assets/NOPE", headers={"Authorization": "Bearer test-token-123"}).status_code == 404
        lst = anon.get("/api/v1/assets?q=L1-PC&limit=3", headers={"Authorization": "Bearer test-token-123"}).get_json()
        assert lst["count"] == 3 and all("tag" in i for i in lst["items"])
        assert anon.get("/api/v1/assets/" + tag, headers={"Authorization": "Bearer test-token-123"}).headers["Content-Type"].startswith("application/json")
        assert anon.post("/api/v1/assets", headers={"Authorization": "Bearer test-token-123"}).status_code in (400, 405)  # nothing can be written
    finally:
        os.environ.pop("AMS_API_TOKEN", None)


def test_helpdesk_writeback():
    m.DB = os.path.join(tempfile.mkdtemp(), "w.db")
    m.UPLOADS = os.path.join(os.path.dirname(m.DB), "uploads")
    m.init_db()
    m.seed_demo()
    a1 = asset_id("L1-PC-01")
    tag = row("SELECT tag FROM assets WHERE id=?", a1)["tag"]
    anon = m.app.test_client()
    url, auth = f"/api/v1/assets/{tag}/ticket-event", {"Authorization": "Bearer wb-token"}
    body = {"event": "opened", "ticket": "HD-0007", "title": "Screen dead"}
    os.environ.pop("AMS_API_TOKEN", None)
    os.environ.pop("AMS_API_WRITE", None)
    assert anon.post(url, json=body).status_code == 404  # API off
    os.environ["AMS_API_TOKEN"] = "wb-token"
    try:
        assert anon.post(url, json=body).status_code == 401  # no token
        assert anon.post(url, json=body, headers=auth).status_code == 403  # token alone can't write
        os.environ["AMS_API_WRITE"] = "1"
        assert anon.post(url, json={"event": "x", "ticket": "HD-1"}, headers=auth).status_code == 400
        assert anon.post(url, json={"event": "opened", "ticket": "<b>"}, headers=auth).status_code == 400
        assert anon.post("/api/v1/assets/NOPE/ticket-event", json=body, headers=auth).status_code == 404
        assert row("SELECT condition FROM assets WHERE id=?", a1)["condition"] == "Working"
        assert anon.post(url, json=body, headers=auth).status_code == 200
        a = row("SELECT condition, status FROM assets WHERE id=?", a1)
        assert (a["condition"], a["status"]) == ("Faulty", "Faulty")
        assert row("SELECT 1 FROM activity WHERE asset_id=? AND text LIKE '%Faulty (from HD-0007)%'", a1)
        # resolved but others still open: logged, still faulty; then the last one marks it working
        assert anon.post(url, json={"event": "resolved", "ticket": "HD-0007", "resolution": "Cable", "mark_working": False}, headers=auth).status_code == 200
        assert row("SELECT condition FROM assets WHERE id=?", a1)["condition"] == "Faulty"
        assert anon.post(url, json={"event": "resolved", "ticket": "HD-0008", "resolution": "Panel", "mark_working": True}, headers=auth).status_code == 200
        a = row("SELECT condition, status FROM assets WHERE id=?", a1)
        assert (a["condition"], a["status"]) == ("Working", "Working")
        assert row("SELECT COUNT(*) n FROM activity WHERE asset_id=? AND event_type='Helpdesk'", a1)["n"] == 3
        # the read-only lookups still work and a form post without a token is still refused
        assert anon.get(f"/api/v1/assets/{tag}", headers=auth).status_code == 200
        assert anon.post("/login", data={"username": "x", "password": "y"}).status_code == 400
    finally:
        os.environ.pop("AMS_API_TOKEN", None)
        os.environ.pop("AMS_API_WRITE", None)


if __name__ == "__main__":
    test_flow()
    test_tags_are_unique_under_concurrency()
    test_old_database_upgrades()
    test_import_wizard()
    test_documents()
    test_specs_stock_licences_reports()
    test_backup_and_restore()
    test_findings_bulk_parts_contracts_api()
    test_helpdesk_writeback()
    print("ok")
