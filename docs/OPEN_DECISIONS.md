# Decisions the college still has to make

The requirements guide (v0.3) lists these as open. The system uses the default shown until they are decided; each is easy to change.

| Decision | What the system does now | Where to change it |
|---|---|---|
| Permanent tag format | `AMS-00042` (prefix + counter) | `TAG_PREFIX` and `next_tag()` in `app.py` |
| Internal ID as the database key, tag as a separate unique field | Yes: `assets.id` is the key, `assets.tag` is unique | already in place |
| What CPR means and how it relates to the Indent | The college's working definition is **Computerized Payment Receipt**, to be confirmed with staff. CPR and Indent are separate reference fields on an asset | fields `indent_no`, `cpr_no` |
| Who approves purchase, movement, retirement, disposal | Retirement: proposer plus a different HOD or admin. Moves: any role with the move right. No purchase approval yet (guide Phase 3) | `CAPS` in `app.py`, `docs/PERMISSIONS.md` |
| Which IT assets are in scope when another department owns them | All assets; managing, owning and using department are separate fields | |
| Which components get their own tag | Parts are tracked as child assets with their own tag | |
| Software and consumables in the pilot | Basic licence list (quantity, renewal, key) and basic counted stock | pages Licences and Consumables |
| Approved department and location lists | Starting department list; buildings and rooms are entered by an admin | Settings |
| Duplicate-resolution rules | Same tag, old code, old-system ID or serial = duplicate (skip or fill blanks). Same name in the same room = a warning only | `find_duplicate()` in `ams_import.py` |
| Permission scopes and role 7 (custodian) | As in `docs/PERMISSIONS.md` | |
| Hosting, login, backups, recovery | Python (Flask) with SQLite; local sign-in; `backup.py` with verify and restore | the guide proposes PostgreSQL: moving is a contained change because all queries are parameterised |
| Digii and ERP access | Not connected | guide Phase 5 |
| Finance rules for depreciation | Straight line over the category's useful life, shown as an estimate | category life in Settings |
| How long records and documents must be kept | Nothing is ever deleted (assets are archived) | |
