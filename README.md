# REC Asset Management System (AMS)

Records every piece of electronics and equipment the college owns: where it is, who is responsible, who holds it, what has happened to it and which papers belong to it.
Built from the "Institutional Asset Management and Service Desk" requirements guide (v0.3). The Helpdesk is a separate app in `helpdesk/`.

## Run

```
pip install -r requirements.txt
python app.py            # empty system; the first visit asks you to create the super admin
python app.py --demo     # sample campus data and one-click demo logins
python app.py --lan      # let other campus PCs open it at http://<this-pc-ip>:5000
python app.py --debug    # auto-reload while editing (local only, never with --lan)
```

Open http://localhost:5000. An older database is upgraded in place when the app starts.

Demo logins (only with `--demo`): `<username>` / `demo-<username>-2026` for `super`, `admin`, `sysadmin`, `purchase`, `hod` (CSE), `lab` (CSE lab assistant), `staff` (CSE custodian), `auditor`, `tech`.
The login page has a button for each.

## What it does

- **Identity.** Each asset has an internal ID and a permanent tag (`AMS-00042`) from a counter that only goes up, so a tag is never reused. A tag can be corrected only by an admin with a reason; the old tag stays searchable. Old asset codes and other systems' IDs (with their source) are kept and searchable.
- **Responsibility.** Managing, owning and using department, the person holding it, and a "location not known yet" flag for old records. Unknown purchase facts stay "Unknown" and are never invented.
- **Four separate questions.** Life stage (requested, in stock, deployed, under maintenance, retirement proposed/approved, awaiting disposal, disposed, lost, returned to vendor), working condition, who holds it (custody), and whether it was physically verified.
- **History you can trust.** Issue, return, transfer and every stage change are recorded with who, why, the date it really happened and the date it was entered. Only one holder at a time; two people issuing the same asset can't both succeed. Mistakes are corrected by new entries.
- **Retirement.** One person proposes, a different HOD or admin approves, then disposal needs an evidence reference (and confirmation that data was erased for computers). Deleting is archiving: the record, history and tag stay.
- **Import from old files.** CSV or Excel. The original file is kept with a fingerprint. Choose the sheet and heading row, map columns (saved for next time), review unknown categories/departments/statuses once, preview with errors, duplicates and warnings, approve, download the rows to review, and reverse while nothing has been done to the imported assets. `-No Value-` and similar become blanks; "ALLOTED" and other spellings go through a reviewed dictionary. A row saying "20 computers" becomes a purchase batch, and units are created only when they arrive and pass inspection.
- **Papers.** PO, invoice, goods receipt, warranty and other documents attach to an asset with versions kept, a fingerprint, and a permission check on every download.
- **Specifications per kind of asset.** A computer asks for CPU and RAM, a switch for ports; admins design the fields (type, unit, allowed values, required).
- **Consumables and software licences.** Counted stock that can't go negative, with every receipt and issue kept; licences with quantity, renewal alerts and keys visible only to purchase and admins.
- **Reports.** By department, kind, stage, condition and place; what is in stock, issued, in repair, awaiting disposal or not verified; data-quality lists with CSV; imports compared with the register; cost reports that say whether they show purchase cost or book value.
- **Roles.** Super admin, admin, system admin, purchase, HOD, lab assistant, custodian, auditor, plus IT technician. A user can have several roles. Department roles only see their own department's assets, enforced on the server. See `docs/PERMISSIONS.md`.
- Also: buildings and rooms, parent and part assets, QR labels, stock-check sheets with a verification result, issues with photos, service log, lending with due dates, depreciation estimates.

## Importing a file

1. **Imports → New import**, choose the file.
2. Pick the sheet and the heading row, and match columns (the page guesses; fix what it got wrong). Say where the file came from and how the cost column should be read (per unit, line total or whole purchase).
3. Answer any new category, department or status once. It is remembered.
4. Read the preview. Everything that will happen is listed; nothing is added yet. Enter the units and total you expect and the page tells you if they differ.
5. Tick that you reviewed the exceptions and approve.

Sample files are in `samples/`.

## Backup and restore

```
python backup.py                              # database + uploads into backups/
python backup.py --verify backups/ams-X.zip   # unpack, check integrity and that every file the database mentions is there
python backup.py --restore backups/ams-X.zip --to D:\spare   # restore into an empty folder
```

Run `--verify` after each backup and try a restore now and then. A backup you have never restored is only a hope.

## Handover documents (`docs/`)

`DATA_DICTIONARY.md` and `PERMISSIONS.md` are generated from the code (`python docs/make_docs.py`). `ERD.md` shows how records connect. `OPEN_DECISIONS.md` lists what the college still has to decide. `RELEASE_CHECKLIST.md` is the pre-pilot list.

## Test

```
python test_app.py        # permissions, history, import wizard, documents, stock, licences, reports, backup, upgrade of an old database
```

## Files

`app.py` the app and core records · `ams_import.py` import wizard and purchase batches · `ams_docs.py` documents · `ams_extras.py` specification fields, stock, licences, reports · `backup.py` · `templates/`, `static/` · `samples/` · `docs/`

## Known limits

Uploads are checked by type and size but not virus-scanned. The database is SQLite on one server (the guide proposes PostgreSQL; the queries are parameterised so moving is a contained change). Online Indent/CPR/PO purchasing, warranty and AMC reminders, planned maintenance, ERP and Digii links are later phases in the guide and are not built.
