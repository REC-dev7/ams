"""Regenerate docs/DATA_DICTIONARY.md and docs/PERMISSIONS.md from the code, so they always match it.

    python docs/make_docs.py
"""
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import app as m  # noqa: E402

TABLES = {
    "users": "People who can sign in. A main role plus optional extra roles; department scopes are in user_scopes.",
    "user_scopes": "Extra departments a department-level user may see besides their own.",
    "departments": "Approved department list (managing, owning and using department on assets point here).",
    "buildings": "Campus buildings.", "rooms": "Rooms, labs and offices inside a building.", "categories": "Kinds of asset (asset types), with the useful life used for book value.",
    "assets": "One row per individually tracked item (and per part of an item). id is the internal key; tag is the permanent label that is never reused.",
    "asset_ids": "Other names for an asset: old asset codes, other systems' IDs (with their source) and previous tags. Searchable, unique per kind+value+source.",
    "custody": "Who holds an asset, over time. Exactly one record per asset has ended_at empty (enforced by a unique index).",
    "activity": "History of everything that happened to an asset. event_date is when it really happened, at is when it was entered.",
    "issues": "Problems reported against an asset.", "services": "Repairs and maintenance done.", "photos": "Pictures of an asset or issue.",
    "documents": "Purchase and ownership papers (PO, invoice, GRN, warranty...). Newer versions point back to the one they replace.",
    "document_links": "Which asset or purchase batch a document belongs to.",
    "attribute_defs": "Extra fields a kind of asset asks for (name, data type, unit, allowed values, required).",
    "attribute_values": "The values entered for those fields on each asset.",
    "stock_items": "Consumables counted by quantity (toner, cables).", "stock_movements": "Every receipt, issue and count correction for those items.",
    "licences": "Software the college has the right to use: quantity, renewal, key (visible to purchase and admins only).",
    "import_batches": "One uploaded file: the original is stored untouched with its fingerprint; status runs Uploaded, Mapped, Previewed, Approved or Reversed.",
    "staged_rows": "The preview of each file row before anything enters the register: what will happen, errors, warnings, duplicate match.",
    "import_mappings": "Saved column mappings, reused when the same file layout arrives again.",
    "value_maps": "The reviewed dictionary: how a spelling in an old file (category, department, status) is understood.",
    "purchase_batches": "A purchase such as '20 computers' that has not yet been turned into individually tagged assets.",
    "counters": "The permanent-tag counter. It only goes up.",
}


def data_dictionary():
    tmp = os.path.join(tempfile.mkdtemp(), "doc.db")
    m.DB = tmp
    m.init_db()
    con = sqlite3.connect(tmp)
    lines = ["# Data dictionary", "", "Generated from the live database definition by `python docs/make_docs.py`. Do not edit by hand.", ""]
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        lines += [f"## {name}", "", TABLES.get(name, ""), "", "| Field | Type | Notes |", "|---|---|---|"]
        fks = {r[3]: f"{r[2]}.{r[4]}" for r in con.execute(f"PRAGMA foreign_key_list({name})")}
        for _, col, typ, notnull, default, pk in con.execute(f"PRAGMA table_info({name})"):
            notes = []
            if pk:
                notes.append("primary key")
            if notnull and not pk:
                notes.append("required")
            if col in fks:
                notes.append(f"links to {fks[col]}")
            if default is not None:
                notes.append(f"default {default}")
            lines.append(f"| {col} | {typ or ''} | {', '.join(notes)} |")
        lines.append("")
    return "\n".join(lines)


def permissions():
    roles = m.ROLES
    out = ["# Who can do what", "", "Generated from `app.py` by `python docs/make_docs.py`. Final approval rules are still for the college to confirm (see OPEN_DECISIONS.md).", "",
           "Every check is made on the server. A hidden button is never the only protection: a person who types an address or calls a route directly gets 403 or 404.", ""]
    acts = {"edit": "Add and edit assets, log service, resolve issues, add old codes", "move": "Issue, return, transfer, retire, stock checks, record stock movements",
            "approve": "Approve a retirement (never the person who proposed it)", "admin": "Settings, imports, tag corrections, archive, specification fields",
            "purchase": "Purchase documents, licences and their keys, purchase batches", "users": "Create and edit users (only a super admin can give admin roles)",
            "docs": "Open purchase documents and licences (insurance papers: super admin, admin, purchase and auditor only)"}
    out += ["| Role | Sees | " + " | ".join(acts) + " |", "|---|---|" + "---|" * len(acts)]
    for r in roles:
        scope = "every department" if r in m.GLOBAL_ROLES else "own department(s) only"
        out.append(f"| {m.ROLE_LABEL[r]} | {scope} | " + " | ".join("yes" if r in m.CAPS[a] else "" for a in acts) + " |")
    out += ["", "## What each column means", ""] + [f"- **{a}**: {t}" for a, t in acts.items()]
    out += ["", "## Rules that are not a simple yes/no", "",
            "- A user can hold several roles; they get the union of what the roles allow.",
            "- Department roles (HOD, lab assistant, custodian) only see assets whose managing, owning or using department is theirs. This covers lists, pages, exports, labels, reports, photos and documents.",
            "- Retirement: one person proposes, a different HOD or admin approves, then it waits for disposal. Disposal needs an evidence reference and, for computers and disks, confirmation that data was erased.",
            "- Two people issuing the same asset: the second is refused because the form carries the custody record it was looking at.",
            "- Auditors can read everything, including documents and exports, and change nothing.",
            "- Licence keys and insurance papers are hidden from everyone except super admin, admin and purchase (auditors may read insurance papers)."]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    open(os.path.join(HERE, "DATA_DICTIONARY.md"), "w", encoding="utf-8", newline="\n").write(data_dictionary() + "\n")
    open(os.path.join(HERE, "PERMISSIONS.md"), "w", encoding="utf-8", newline="\n").write(permissions())
    print("docs written")
