"""Back up the database and every uploaded file together, then prove the backup can be restored.

    python backup.py                              back up ams.db + uploads/ into backups/ams-<date>.zip
    python backup.py --verify backups/ams-X.zip   unpack to a temporary folder and check it (integrity, counts, missing files)
    python backup.py --restore FILE --to FOLDER   put the database and uploads back into FOLDER

The Helpdesk can use the same tool: python backup.py --db helpdesk/helpdesk.db --uploads helpdesk/uploads --name helpdesk
A backup you have never restored is only a hope, so run --verify after every backup and --restore into a spare folder now and then.
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
# (table, column holding a stored file name, folder under uploads/ where those files live)
FILE_COLUMNS = [("photos", "file", ""), ("attachments", "file", ""), ("documents", "file", "docs"), ("import_batches", "stored_file", "imports")]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_backup(db_path, uploads_dir, out_dir, name="ams"):
    """Write one zip holding a consistent copy of the database (SQLite's own backup, safe while the app runs) and the uploads."""
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = os.path.join(out_dir, f"{name}-{stamp}.zip")
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "data.db")
        src, dst = sqlite3.connect(db_path), sqlite3.connect(copy)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        manifest = {"made": stamp, "name": name, "files": {}}
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(copy, "data.db")
            manifest["db_sha256"] = sha256(copy)
            if os.path.isdir(uploads_dir):
                for root, _, files in os.walk(uploads_dir):
                    for f in files:
                        full = os.path.join(root, f)
                        rel = "uploads/" + os.path.relpath(full, uploads_dir).replace(os.sep, "/")
                        z.write(full, rel)
                        manifest["files"][rel] = sha256(full)
            z.writestr("manifest.json", json.dumps(manifest, indent=1))
    return out


def verify_backup(zip_path):
    """(ok, problems, summary). Unpacks to a temporary folder; checks fingerprints, database integrity and that every referenced file exists."""
    problems, summary = [], {}
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        manifest = json.load(open(os.path.join(tmp, "manifest.json")))
        if sha256(os.path.join(tmp, "data.db")) != manifest["db_sha256"]:
            problems.append("database fingerprint does not match")
        for rel, digest in manifest["files"].items():
            p = os.path.join(tmp, *rel.split("/"))
            if not os.path.exists(p) or sha256(p) != digest:
                problems.append(f"file damaged or missing: {rel}")
        con = sqlite3.connect(os.path.join(tmp, "data.db"))
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            problems.append("database integrity check failed")
        if con.execute("PRAGMA foreign_key_check").fetchall():
            problems.append("database has broken links between records")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("assets", "tickets", "users"):
            if t in tables:
                summary[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for table, col, folder in FILE_COLUMNS:
            if table in tables:
                for (f,) in con.execute(f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL"):
                    rel = "uploads/" + (folder + "/" if folder else "") + f
                    if rel not in manifest["files"]:
                        problems.append(f"{table} points to {rel}, which is not in the backup")
        summary["files"] = len(manifest["files"])
        con.close()
    return not problems, problems, summary


def restore_backup(zip_path, dest, db_name="ams.db"):
    """Unpack the database and uploads into dest (a spare folder first, please). Never overwrites an existing database."""
    target = os.path.join(dest, db_name)
    if os.path.exists(target):
        raise FileExistsError(f"{target} already exists. Restore into an empty folder, then swap it in yourself.")
    os.makedirs(dest, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        shutil.copy(os.path.join(tmp, "data.db"), target)
        if os.path.isdir(os.path.join(tmp, "uploads")):
            shutil.copytree(os.path.join(tmp, "uploads"), os.path.join(dest, "uploads"))
    return target


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(HERE, "ams.db"))
    ap.add_argument("--uploads", default=os.path.join(HERE, "uploads"))
    ap.add_argument("--out", default=os.path.join(HERE, "backups"))
    ap.add_argument("--name", default="ams")
    ap.add_argument("--verify", metavar="ZIP")
    ap.add_argument("--restore", metavar="ZIP")
    ap.add_argument("--to", metavar="FOLDER")
    a = ap.parse_args(argv)
    if a.verify:
        ok, problems, summary = verify_backup(a.verify)
        print("OK" if ok else "PROBLEMS", summary)
        for p in problems:
            print(" -", p)
        return 0 if ok else 1
    if a.restore:
        if not a.to:
            ap.error("--restore needs --to FOLDER")
        print("restored database to", restore_backup(a.restore, a.to, os.path.basename(a.db)))
        return 0
    path = make_backup(a.db, a.uploads, a.out, a.name)
    ok, problems, summary = verify_backup(path)
    print("backup written:", path, "| check:", "OK" if ok else "PROBLEMS", summary)
    for p in problems:
        print(" -", p)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
