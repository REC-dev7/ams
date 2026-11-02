# Release checklist

Tick every line before the pilot goes to staff, and again before any later release.

## Before
- [ ] `python test_app.py` and `python helpdesk/test_helpdesk.py` both print `ok`.
- [ ] Demo accounts and demo data are removed (run without `--demo`; delete `DEMO` from the host's settings). First visit asks for the real super admin.
- [ ] The site runs over HTTPS (the host provides it) and `SESSION_COOKIE_SECURE` is on (automatic on Render).
- [ ] The secret key file (`secret.key`) is kept private and backed up separately from the database.
- [ ] The college owns the repository, hosting account and credentials, not one student.

## Data
- [ ] Departments, buildings and rooms are the approved lists.
- [ ] A small, representative import was run end to end with staff: upload, map, review values, preview, approve.
- [ ] Imported quantities were compared with the totals staff expected (the preview does this when you enter them).
- [ ] Reversal was tried on a test import.

## Safety
- [ ] `python backup.py` ran, then `python backup.py --verify <zip>` said OK.
- [ ] A backup was restored into a spare folder (`--restore <zip> --to <folder>`) and opened.
- [ ] Permission checks were tried as each role: a department user cannot open another department's asset, document, photo or report.
- [ ] Uploaded files are limited to the allowed types and sizes (check an `.exe` and a renamed `.html` are refused).
- [ ] Note: uploads are not virus-scanned. If the host offers scanning, switch it on.

## After
- [ ] Staff were shown an asset's page (history, who holds it, documents) and the Reports page.
- [ ] A named person owns each open decision in `OPEN_DECISIONS.md`.
