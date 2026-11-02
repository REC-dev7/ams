# Who can do what

Generated from `app.py` by `python docs/make_docs.py`. Final approval rules are still for the college to confirm (see OPEN_DECISIONS.md).

Every check is made on the server. A hidden button is never the only protection: a person who types an address or calls a route directly gets 403 or 404.

| Role | Sees | edit | move | approve | admin | purchase | users | docs |
|---|---|---|---|---|---|---|---|---|
| Super admin | every department | yes | yes | yes | yes | yes | yes | yes |
| Admin | every department | yes | yes | yes | yes | yes | yes | yes |
| System admin | every department |  |  |  |  |  | yes | yes |
| Purchase | every department |  |  |  |  | yes |  | yes |
| HOD | own department(s) only |  | yes | yes |  |  |  | yes |
| Lab assistant | own department(s) only |  | yes |  |  |  |  |  |
| Custodian / end user | own department(s) only |  |  |  |  |  |  |  |
| Auditor (read only) | every department |  |  |  |  |  |  | yes |
| IT technician | every department | yes | yes |  |  |  |  | yes |

## What each column means

- **edit**: Add and edit assets, log service, resolve issues, add old codes
- **move**: Issue, return, transfer, retire, stock checks, record stock movements
- **approve**: Approve a retirement (never the person who proposed it)
- **admin**: Settings, imports, tag corrections, archive, specification fields
- **purchase**: Purchase documents, licences and their keys, purchase batches
- **users**: Create and edit users (only a super admin can give admin roles)
- **docs**: Open purchase documents and licences (insurance papers: super admin, admin, purchase and auditor only)

## Rules that are not a simple yes/no

- A user can hold several roles; they get the union of what the roles allow.
- Department roles (HOD, lab assistant, custodian) only see assets whose managing, owning or using department is theirs. This covers lists, pages, exports, labels, reports, photos and documents.
- Retirement: one person proposes, a different HOD or admin approves, then it waits for disposal. Disposal needs an evidence reference and, for computers and disks, confirmation that data was erased.
- Two people issuing the same asset: the second is refused because the form carries the custody record it was looking at.
- Auditors can read everything, including documents and exports, and change nothing.
- Licence keys and insurance papers are hidden from everyone except super admin, admin and purchase (auditors may read insurance papers).
