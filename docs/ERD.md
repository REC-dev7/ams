# Entity relationship diagram

How the main records connect. Field-level detail is in `DATA_DICTIONARY.md`. The diagram renders on GitHub.

```mermaid
erDiagram
    DEPARTMENTS ||--o{ ASSETS : "manages / owns / uses"
    CATEGORIES ||--o{ ASSETS : "kind of"
    CATEGORIES ||--o{ ATTRIBUTE_DEFS : "asks for"
    ATTRIBUTE_DEFS ||--o{ ATTRIBUTE_VALUES : "filled in as"
    ASSETS ||--o{ ATTRIBUTE_VALUES : "has"
    BUILDINGS ||--o{ ROOMS : contains
    ROOMS ||--o{ ASSETS : "located in"
    ASSETS ||--o{ ASSETS : "has parts"
    ASSETS ||--o{ ASSET_IDS : "also known as"
    ASSETS ||--o{ CUSTODY : "held by (one active)"
    DEPARTMENTS ||--o{ CUSTODY : "holds"
    ASSETS ||--o{ ACTIVITY : "history"
    ASSETS ||--o{ ISSUES : "problems"
    ASSETS ||--o{ SERVICES : "repairs"
    ASSETS ||--o{ PHOTOS : pictures
    DOCUMENTS ||--o{ DOCUMENT_LINKS : "attached through"
    ASSETS ||--o{ DOCUMENT_LINKS : "has papers"
    PURCHASE_BATCHES ||--o{ DOCUMENT_LINKS : "has papers"
    DOCUMENTS ||--o| DOCUMENTS : "replaces older version"
    IMPORT_BATCHES ||--o{ STAGED_ROWS : "previewed as"
    IMPORT_BATCHES ||--o{ ASSETS : "created"
    IMPORT_BATCHES ||--o{ PURCHASE_BATCHES : "created"
    PURCHASE_BATCHES ||--o{ ASSETS : "confirmed as units"
    STOCK_ITEMS ||--o{ STOCK_MOVEMENTS : "counted by"
    DEPARTMENTS ||--o{ LICENCES : "uses"
    USERS ||--o{ USER_SCOPES : "may also see"
    DEPARTMENTS ||--o{ USERS : "belongs to"
```

## Reading it

- **Asset** is one individually tracked thing. Its tag is permanent and comes from a counter that only goes up (`counters`), so it is never reused, even after archiving or a correction (the old tag is kept in `asset_ids`).
- **Custody** answers "who holds it". Exactly one row per asset is open at a time; moving an asset closes one row and opens the next in one step.
- **Lifecycle stage, condition, verification** are separate fields on the asset. The old one-word status is worked out from them.
- A purchase is not an asset. A row such as "20 computers" is a **purchase batch** until units are confirmed; each confirmed unit becomes its own asset.
- Everything that arrives from a file passes through **import batch, staged rows and approval** first, and keeps a link back to its source row.
