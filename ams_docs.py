"""Purchase and ownership documents (PO, invoice, GRN, warranty...) kept with the asset they belong to.

Files are stored under random names with a fingerprint; every view and download checks the person may see that asset.
Old versions are kept. Only the file's real type (its first bytes) is trusted, never its name.
"""
import hashlib
import os
import re
import secrets

from flask import abort, flash, g, redirect, render_template, request, send_from_directory, url_for

KINDS = ["Purchase order", "Invoice", "Goods receipt (GRN)", "Indent / CPR", "Quotation", "Warranty", "Insurance", "Transfer",
         "Disposal certificate", "Other"]
CONFIDENTIAL = ("Insurance",)  # visible to admins, purchase and auditors only
MAX_DOC = 8 * 1024 * 1024
INLINE = {".pdf", ".png", ".jpg", ".gif", ".webp"}
IMAGES = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png", b"GIF87a": ".gif", b"GIF89a": ".gif"}


def sniff(data, name):
    """The stored extension if the bytes really are a PDF, picture, Excel/Word file or plain text; otherwise None."""
    ext = os.path.splitext(name.lower())[1]
    if data.startswith(b"%PDF-"):
        return ".pdf" if ext == ".pdf" else None
    for magic, e in IMAGES.items():
        if data.startswith(magic):
            return e if ext in (e, ".jpeg") else None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp" if ext == ".webp" else None
    if data.startswith(b"PK\x03\x04") and ext in (".xlsx", ".docx"):
        return ext
    if ext in (".csv", ".txt") and b"\x00" not in data[:4096]:
        try:
            data[:4096].decode("utf-8")
            return ext
        except UnicodeDecodeError:
            return None
    return None


def init(A):
    q, db, app, role = A.q, A.db, A.app, A.role
    DOCS = lambda: os.path.join(A.UPLOADS, "docs")

    def doc_for_user(did):
        """The document, or a 403/404. Needs the right role, and an asset the person may see."""
        d = q("SELECT * FROM documents WHERE id=?", (did,), one=True)
        if not d:
            abort(404)
        if not A.allowed("docs") or (d["kind"] in CONFIDENTIAL and not A.has_role("super_admin", "admin", "purchase", "auditor")):
            abort(403)
        sw, sa = A.scope_sql()
        on_asset = q("SELECT 1 FROM document_links l JOIN assets a ON a.id=l.asset_id WHERE l.document_id=?" + sw, [did, *sa], one=True)
        on_batch = A.dept_ids() is None and q("SELECT 1 FROM document_links WHERE document_id=? AND purchase_batch_id IS NOT NULL", (did,), one=True)
        if not (on_asset or on_batch):
            abort(404)
        return d

    def asset_documents(aid):
        """Documents on an asset that this user may open: latest versions first, older versions kept underneath."""
        if not A.allowed("docs"):
            return []
        rows = q("""SELECT d.*, u.name by_name FROM documents d JOIN document_links l ON l.document_id=d.id LEFT JOIN users u ON u.id=d.uploaded_by
                    WHERE l.asset_id=? ORDER BY d.kind, d.id DESC""", (aid,))
        if not A.has_role("super_admin", "admin", "purchase", "auditor"):
            rows = [r for r in rows if r["kind"] not in CONFIDENTIAL]
        replaced = {r["supersedes_id"] for r in rows if r["supersedes_id"]}
        return [{"d": r, "current": r["id"] not in replaced} for r in rows]

    app.jinja_env.globals["asset_documents"] = asset_documents
    app.jinja_env.globals["DOC_KINDS"] = KINDS

    @app.route("/assets/<int:aid>/documents", methods=["POST"])
    @role("purchase")
    def document_add(aid):
        a = A.get_asset(aid)
        fh, f = request.files.get("file"), request.form
        kind = f.get("kind") if f.get("kind") in KINDS else "Other"
        if not fh or not fh.filename:
            flash("Choose a file.", "error")
            return redirect(url_for("asset", aid=aid))
        data = fh.read(MAX_DOC + 1)
        ext = sniff(data, fh.filename) if len(data) <= MAX_DOC else None
        if not ext:
            flash("Use a PDF, picture (JPG, PNG, GIF, WebP), Excel or Word file, or a CSV/text file, up to 8 MB. Other types are refused.", "error")
            return redirect(url_for("asset", aid=aid))
        prev = q("""SELECT d.* FROM documents d JOIN document_links l ON l.document_id=d.id WHERE d.id=? AND l.asset_id=?""",
                 (A.opt_int(f.get("replaces")), aid), one=True) if f.get("replaces") else None
        os.makedirs(DOCS(), exist_ok=True)
        stored = secrets.token_hex(16) + ext
        with open(os.path.join(DOCS(), stored), "wb") as out:
            out.write(data)
        cur = db().execute("""INSERT INTO documents(file, original_name, ext, size, sha256, kind, version, supersedes_id, description, reference, uploaded_by)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                           (stored, os.path.basename(fh.filename)[:150], ext, len(data), hashlib.sha256(data).hexdigest(), prev["kind"] if prev else kind,
                            prev["version"] + 1 if prev else 1, prev["id"] if prev else None, (f.get("description") or "").strip() or None,
                            (f.get("reference") or "").strip() or None, g.user["id"]))
        db().execute("INSERT INTO document_links(document_id, asset_id) VALUES (?,?)", (cur.lastrowid, aid))
        A.log(aid, f"Document added: {prev['kind'] if prev else kind}" + (f" (version {prev['version'] + 1})" if prev else "")
              + ("" if (prev["kind"] if prev else kind) in CONFIDENTIAL else f", {fh.filename}"), etype="Document")
        db().commit()
        flash("Document saved." + (" The older version is kept." if prev else ""), "ok")
        return redirect(url_for("asset", aid=aid))

    @app.route("/documents/<int:did>")
    def document_open(did):
        d = doc_for_user(did)
        return send_from_directory(DOCS(), d["file"], as_attachment=d["ext"] not in INLINE, download_name=d["original_name"])

    @app.route("/documents")
    @role("docs")
    def documents():
        a = request.args
        sw, sa = A.scope_sql()
        sql = """SELECT d.*, u.name by_name, (SELECT COUNT(*) FROM documents n WHERE n.supersedes_id=d.id) replaced,
                 (SELECT group_concat(x.tag, ', ') FROM document_links l JOIN assets x ON x.id=l.asset_id WHERE l.document_id=d.id) tags
                 FROM documents d LEFT JOIN users u ON u.id=d.uploaded_by WHERE d.id IN (SELECT l.document_id FROM document_links l JOIN assets a ON a.id=l.asset_id WHERE 1=1""" + sw + ")"
        args = list(sa)
        if a.get("kind") in KINDS:
            sql += " AND d.kind=?"
            args.append(a["kind"])
        if a.get("q", "").strip():
            sql += " AND (d.original_name LIKE ? OR d.description LIKE ? OR d.reference LIKE ?)"
            args += [f"%{a['q'].strip()}%"] * 3
        if not A.has_role("super_admin", "admin", "purchase", "auditor"):
            sql += " AND d.kind NOT IN ('Insurance')"
        rows, pg = A.paginate(sql + " ORDER BY d.id DESC", args)
        return render_template("documents.html", rows=rows, pg=pg, KINDS=KINDS)
