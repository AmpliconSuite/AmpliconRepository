# The backend, and the tools that keep it honest

Written 2026-09-02, at the close of the storage-and-lineage work that runs from
tag `v3.1.0_082426` to release 4.0.0 — 135 files, 22 new application modules,
20 new operational scripts, 42 new test files.

This is the map. It says what the stores are, what a project document is, how a
version chain is encoded, where the bytes go, and which tool answers which
question. It does not repeat the modules: each one carries its own reasoning in
its docstring, and those are the primary source. What is here is the shape they
fit into and the pointers between them.

Read [AGENTS.md](../AGENTS.md) first for environment setup and the data model.
The verification discipline this codebase demands before a claim about the data
is written down anywhere is stated in full in §12 below, rather than pointed at:
the file it used to live in is not tracked by git, so a link to it would resolve
for nobody who clones this repository. The short version, because everything
here depends on it: **name the falsifying measurement and run it before the
claim goes into a comment, a commit message or a report.** Every number in this
document is dated and scoped for that reason.

---

## 1. The stores

Four systems hold state, and only one of them is the authority for project data.

```mermaid
flowchart TD
    subgraph app["Django application (gunicorn, preload_app)"]
        V["views.py / views_admin.py / views_apis.py"]
    end

    subgraph auth["SQLite — caper.sqlite3"]
        S1["Django auth, sessions"]
        S2["Mezzanine CMS pages"]
    end

    subgraph mongo["DocumentDB — one cluster, two databases"]
        M1["projects — every version of every project"]
        M2["fs.files / fs.chunks — the GridFS payload"]
        M3["project_audit_log — provenance"]
        M4["project_version_chains — derived chain view"]
        M5["storage_statistics / site_statistics — daily snapshots"]
        M6["gridfs_ownership_reports — survey results"]
    end

    subgraph s3["S3"]
        A1["amprepo-private — download cache"]
        A2["amprepobucket — static files"]
        A3["amprepo-backups — nightly SQLite"]
        A4["amprepo-refs — reference genomes"]
    end

    N["Neo4j — co-amplification graph"]

    V --> auth
    V --> mongo
    V --> s3
    V --> N

    M1 -.->|names its files| M2
    M2 -.->|backlink names its document| M1
    M1 ==>|rebuilt from| M4
```

Two properties of this picture are load-bearing and are asserted by tests:

**Authority runs documents → files, never the other way round.** A GridFS file
is retained because a retained project document names it. The backlink in
`fs.files.metadata` is an index into that fact, so that "is *this one file*
orphaned?" can be a `find_one` instead of a traversal of every document. It is
provenance and never authority: nothing may delete a file because the file's own
metadata says it is unowned. See `gridfs_backlinks.py` and
`gridfs_ownership.py`, which compute the same answer by deliberately different
routes so that their agreement means something.

**`project_version_chains` is a materialised view.** Feature code reads it; only
the rebuild writes it; the documents always win. A `source_digest` makes
staleness a one-string comparison rather than a field-by-field diff. See
`version_chains.py`.

The two databases — `caper` (prod) and `caper-dev` (dev) — **share one cluster**.
Anything that connects must assert which one it opened. The dev application
credential is scoped to `caper-dev` and raises `OperationFailure` on `caper`,
which is a guard rail rather than an obstacle: prod measurements run from the
prod container.

---

## 2. A project document is two things wearing one shape

It is a **version** — one aggregation run, its samples, its tool versions, its
payload — and it is the **project** that owns that version: name, members,
visibility, alias, counters.

That distinction is not cosmetic. When a version is deleted and an older one
promoted in its place, promotion has to decide which half the promoted document
inherits. It used to decide by a hand-written list of nine field names;
`downloads` was on it and `project_downloads` was not, and the counter reset
every time a version was deleted.

`project_fields.py` is the declaration that list should have been, and promotion
reads it rather than a literal. **The test for which level a field belongs to is
not its name and not taste: a field is chain-level exactly when it must survive
the deletion of every version.**

| level | examples | on promotion |
|---|---|---|
| chain-level | `project_name`, `project_members`, `private`, `alias_name`, `featured`, `privateKey` | carried |
| chain-level, kept where earned | `project_downloads`, `sample_downloads`, `downloads` | **not** carried — summed across the chain when read |
| version-level | `runs`, `samples`, tool versions, GridFS keys | never carried |

The counters are the interesting case. They are chain-level facts stored per
version, so reading only the head under-reports by everything the older versions
earned — on a chain with several versions that is most of the total.
`download_totals.py` sums the chain at read time rather than moving the numbers,
which is why they are the one chain-level group promotion does *not* carry.

---

## 3. The five statuses

`project_status.py` is the single authority for "what is this document". Before
it, 72 call sites answered that question themselves in 21 distinct query shapes,
and both production incidents on record were the same failure: a predicate that
lives in the application, re-derived somewhere else, drifted.

```mermaid
stateDiagram-v2
    [*] --> LIVE: upload completes
    LIVE --> SUPERSEDED: a new version is uploaded
    SUPERSEDED --> LIVE: promotion, after the head is deleted
    LIVE --> SOFT_DELETED: user deletes the project
    SOFT_DELETED --> LIVE: admin restores
    SUPERSEDED --> TOMBSTONE: version deleted from history, payload purged
    LIVE --> TOMBSTONE: last version deleted
    SOFT_DELETED --> [*]: admin permanent delete
    TOMBSTONE --> [*]: admin permanent delete

    DETACHED
    note right of DETACHED
        Not a transition target.
        A document whose meaning
        cannot be read off the schema.
    end note
```

| status | reachable by URL? | payload | meaning |
|---|---|---|---|
| `LIVE` | yes | retained | current head of a chain |
| `SUPERSEDED` | **yes** | retained | an earlier version — published links must keep working |
| `SOFT_DELETED` | no | retained | user-deleted, restorable from the admin page |
| `TOMBSTONE` | yes, as a redirect | purged | removed from history |
| `DETACHED` | varies | retained | meaning undetermined; a named state so the population is countable |

Every status is derived from one table of `(delete, current)` flag pairs plus
the tombstone markers, so the classifier, the Mongo filters and the values
written on a change cannot drift from each other. A grep guard in
`tests/test_project_status_guard.py` keeps `delete` and `current` from being
spelled by hand outside the module.

**`SUPERSEDED` documents are load-bearing.** They serve published results, and
"not the head of a chain" is never a reason to delete anything.

---

## 4. Lineage: three axes that are deliberately independent

A chain is encoded twice, and both encodings are still written.

```mermaid
flowchart LR
    subgraph chain["one version chain"]
        direction LR
        V1["v1<br/>ordinal 1<br/>SUPERSEDED"]
        V2["v2<br/>ordinal 2<br/>SUPERSEDED"]
        V3["v3<br/>ordinal 3<br/>is_latest ✓<br/>LIVE"]
    end
    V1 -->|next_version_id| V2
    V2 -->|next_version_id| V3
    V3 -->|previous_version_id| V2
    V2 -->|previous_version_id| V1
    CV["project_version_chains<br/>(derived, source_digest)"]
    V3 -.-> CV
    V2 -.-> CV
    V1 -.-> CV
```

**Pointers are structure, `is_latest` is position, status is state.** Those three
answer different questions and are not required to agree:

- **structure** — `version_chain_id`, `previous_version_id`, `next_version_id`,
  `version_ordinal`: which documents belong together, and in what order.
- **position** — `is_latest`: which one the site presents as current. Promotion
  moves it backwards along a chain whose pointers do not change.
- **state** — `classify()`: what each document is.

The older encoding, the denormalised `previous_versions[]` array, is cumulative:
every version carries the whole ancestry, so two documents naming the same
ancestor is normal and is not a fork. Since 2026-09-02 the site reads history
from the pointers only; the array fallback was removed once both databases were
fully pointered — no document in either database is left without a
`version_chain_id`, which is what invariant I1 asserts.

Every query that used to ask `{'previous_versions.linkid': <id>}` — a scan of an
array on every document in the collection — is now an equality match on
`version_chain_id`.

The independence of the three axes is a decision, not an oversight, and it has a
visible failure mode: the "you are viewing an older version" banner reads
`is_latest` alone, so a document that is `LIVE` but not flagged renders the
banner and links elsewhere. Invariant I7 catches the shape that produces it and
names the offending document by id, so this class of fault is never silent.

---

## 5. The life of a payload

```mermaid
sequenceDiagram
    participant U as User
    participant W as gunicorn worker
    participant G as GridFS
    participant D as projects document
    participant S as amprepo-private

    U->>W: upload tar.gz
    W->>W: aggregate (in-process, background thread)
    loop per file
        W->>G: fs.put(file) with a backlink in metadata
    end
    W->>D: write the document that names them
    Note over W,D: a failure between the loop and this write<br/>is the orphan factory — upload_cleanup.py<br/>removes what was already stored

    U->>W: download project
    W->>S: is the project tar.gz already cached?
    alt hit
        S-->>U: redirect to the object
    else miss
        W->>G: read the payload (~21 MiB/s)
        W->>S: write the object
        S-->>U: redirect
    end
```

Two measured facts shape everything on this path.

**Regeneration is recoverable but not free.** A cache miss rebuilds inside the
request at about 21 MiB/s, so a multi-gigabyte project is several minutes of a
blocked worker against gunicorn's 900 s ceiling. That is why nothing expires
cached objects by age or size, and why a sweep touches only objects nothing can
ever ask for again.

**Failed uploads, not deletions, are what strands files.** TCGA_Sarcoma's failed
upload of 2026-08-14 left 2,695 files inside a 90-second window.
`upload_cleanup.py` closes the case where an exception runs the failure path;
a worker that is *killed* runs no failure path at all, and the weekly
`cleanup_failed_upload_residue.py` sweep is the backstop for that. That split is
the right architecture rather than a gap: only something that runs later can
collect after a crash.

---

## 6. Deletion, and why the order changed

Deleting a version used to purge the GridFS payload first and write the
tombstone afterwards, inside the web request. A payload can take longer to
delete than a request is allowed to live — about `96.4 × GiB + 0.035 × files`
seconds, against a 900 s worker timeout — and a killed worker writes no
traceback.

Measured on dev 2026-09-02, deleting version 2 of a five-version PCAWG chain:
the event was recorded at 16:49:30, the worker was killed at 17:04:31 (901 s),
**15,272 of 15,733 files were deleted**, and the document was left classified
`SUPERSEDED` with no tombstone markers. The site presented an ordinary older
version whose payload was 97% gone, and nothing was looking for that state.

```mermaid
flowchart TD
    A["user deletes a version"] --> B["provenance.record() — written BEFORE the mutation"]
    B --> C["write the tombstone, synchronously<br/>one document write; the user waits on this"]
    C --> D["record the payload ids on the tombstone<br/>under PENDING_PAYLOAD_KEY"]
    D --> E["promote the predecessor if the head was deleted<br/>carrying CARRIED_ON_PROMOTION"]
    E --> F["purge the payload off the request thread<br/>ids removed from the tombstone as they go"]
    F --> G["provenance.confirm()"]
```

Three consequences worth knowing:

- **The tombstone carries the ids.** A tombstone is built fresh and does not
  inherit the payload keys, so once it is written nothing else knows which files
  that version owned. Recording them on the tombstone makes an interrupted purge
  resumable *by name*, rather than a matter for a global orphan sweep.
- **Provenance is written before the mutation, and never raises.** An event with
  `completed` unset is the signature of an operation that started and did not
  finish — the only way to tell that apart from one that never started. Because
  the writes are swallowed on failure, the log is evidence and never authority:
  do not build a check that assumes an event exists for every mutation.
- **A single `delete_many` per chunk range times out on large files.** That
  reports failure for a delete that in fact succeeded.

---

## 7. The maintenance toolkit

Four kinds of tool, and the kind matters more than the name. **Nothing in the
first two columns writes.**

### Surveyors — report only, never delete

| tool | the question it answers |
|---|---|
| `report_gridfs_orphans.py` | Account for every GridFS file. Read-only, always. |
| `caper/ownership_survey.py` + `manage.py ownership_survey` | Which project owns each file, and how much is residue. Runs outside a web worker. |
| `caper/storage_stats.py` + `snapshot_storage.py` | What the databases hold, by status bucket, once a day, with history. |
| `check_volume_reclamation.py` | Did deleted storage actually come back? (Measured answer: no — see §9.) |
| `compare_version_history.py` | Diff the two version-history readers over every project. |
| `dump_metadata.py` | Every collection except the payload, for an off-AWS copy. |
| `caper/url_manifest.py` | Every project URL that resolves today, and to what. |
| `check_project_flags.py` | The legacy flag pairs, per document. |

### Validators — assert invariants

| tool | the question it answers |
|---|---|
| `validate_project_lineage.py` | 21 invariants over version history. I7 catches a LIVE document listed in another's history; I9 catches a stale chain view; I11 catches array/pointer disagreement; I12–I14 need GridFS and are skipped under `--skip-gridfs`. |
| `caper/schema_validate.py` | JSON-schema validation of project documents. |
| `verify_indexes.py` | The indexes the query plans assume. |

### Sweepers — these delete. Report-only by default.

| tool | scope | the guard that makes it safe |
|---|---|---|
| `sweep_gridfs_unreferenced.py` | GridFS files no document references | acts on a reviewed snapshot, not a fresh count |
| `sweep_s3_unreferenced.py` | S3 objects no document in **any** database references | `--require-databases` refuses to run without an id set per database; objects are listed *before* ids are read, so a project created in between counts as referenced |
| `cleanup_failed_upload_residue.py` | files a failed ingestion left behind | selects by metadata only within the narrow window where that is sound |
| `clear_stale_uploads.py` | upload placeholders that crashed before becoming projects | finds them by owner, never by date |
| `cleanup_orphaned_projects.py` | documents no resolver can reach | protection rules derived from `project_status`, not re-typed |
| `purge-local-db.py` | **local development databases only** | GridFS key list imported from the application |

Every sweeper stages with `--limit N` and writes an undo record. A staged run
whose prediction misses is a stop, not a retry.

### Backfills and repairs — one-time, and they write

`backfill_project_status.py` · `backfill_gridfs_backlinks.py` ·
`backfill_create_events.py` · `backfill_audit_chain_ids.py` ·
`rebuild_version_chains.py` · `repair_head_chain_fields.py` ·
`repair_promoted_tombstone.py` · `recover_deleted_version.py` ·
`zero_carried_forward_views.py` · `migrate_project_visibility.py` ·
`restore_sample_csv_metadata.py`

One warning about `backfill_project_status.py`, because it is the writer behind
the only lineage defect still visible on dev: `order_chain()` picks a chain head
as "the member no other document's history names", and consults
`classify() == LIVE` **only** when more than one member qualifies. A chain where
exactly one member qualifies and it is `SUPERSEDED` gets the wrong head.

---

## 8. The admin pages

All sixteen were exercised locally on 2026-09-02; every one returned without
raising.

| page | what it is for | writes? |
|---|---|---|
| Statistics | users, projects, storage snapshot with history | regenerate button only |
| Version details | tool versions across projects | no |
| Featured projects | choose what the front page shows | yes |
| Project files report | per-project file inventory, with the audit log | no |
| **File ownership & orphans** | the survey result: owned vs residue | **no — deliberately** |
| Download backups | SQLite, metadata dump, URL manifest — the copies that can leave AWS | records who took one |
| Delete project / Delete user | soft delete, restore, permanent delete | yes |
| Prepare shutdown | the Mongo flag that closes the site | yes |
| Send email | mail to members | yes |
| Clear cache | drop the GridFS read cache | yes |

**File ownership & orphans has no delete control, and that is the design.** Both
production incidents behind this work were a count exactly like the one on that
page being believed and acted on. The page reports; a sweeper acts, from a
reviewed snapshot, staged, with an undo record.

---
## 9. Measuring the current state

An earlier draft of this section was a census: document counts, storage totals,
per-status shares. That was a mistake. A number describing current state is
wrong the moment somebody uploads or deletes anything, and a stale number in a
document is worse than no number, because it gets quoted.

So this section names the command instead. Each is read-only and each prints
what the older table used to claim:

| To learn | Run |
|---|---|
| documents per status, chains, unpointered documents | `validate_project_lineage.py --expect-db <db>` |
| GridFS files, bytes, owned vs residue, backlink coverage | `report_gridfs_orphans.py --expect-db <db>` |
| storage by status with trend | Admin ▸ Statistics, or `snapshot_storage.py` |
| who owns each file | `manage.py ownership_survey` |
| whether deleted storage came back | `check_volume_reclamation.py` |
| S3 objects and which database claims each | `sweep_s3_unreferenced.py` report-only |

If you do record a measurement somewhere — a commit message, an incident note,
a wiki page — **date it and name its scope**. "935 private against 2 live,
measured locally 2026-08-27" survives contact with a reader six months later.
"The counter is wrong" does not.

---

## 10. Facts that do not go stale

These are worth writing down because they are not current-state readings. They
are either rates, or records of something that already happened, and re-running
a census will not change them.

**Authority runs documents → files.** A file is retained because a retained
document names it. The `fs.files.metadata` backlink is an index into that fact,
never a substitute: nothing may delete a file because its own metadata says it
is unowned.

**A payload can take longer to delete than a request may live.** Roughly
`96.4 × GiB + 0.035 × files` seconds, against a 900-second worker timeout. That
ratio is why deletion writes the tombstone first and purges afterwards.

**Cache regeneration is recoverable but not free.** A download-cache miss
rebuilds inside the request at about 21 MiB/s, so a multi-gigabyte project is
several minutes of a blocked worker. Hence: nothing expires cached objects by
age or size.

**Reads go to a replica.** The cluster URI sets `secondaryPreferred`. In 40
write-then-immediately-read trials on dev, 40 of 40 missed on the default
preference and 0 of 40 missed pinned to `PRIMARY`. This is not intermittent, so
"add a retry" is the wrong shape of fix. Pin anything that writes and then
checks, and anything that writes a *derived* value computed from what it read.

**Deleting data does not shrink the cluster volume.** Hundreds of gigabytes were
removed across both databases without `VolumeBytesUsed` moving. `collStats`
free-list figures are worse than useless here: `unusedStorageSize` froze
byte-identical across a multi-gigabyte write and drifted on quiet days.

**The S3 download cache is versioned, so a delete does not free anything for 90
days.** Deleting an object leaves a delete marker and makes the old version
noncurrent; a lifecycle rule expires noncurrent versions after 90 days. The live
namespace shrinks immediately, the bill does not. This is the same trap as the
cluster volume, in a different service.

**`private` is an enum, not a boolean** — `'private'`, `'public'`,
`'hidden_public'`, with legacy booleans still present. `{'private': False}`
matches nothing, silently.

**A field's name tells you nothing, and neither does the code that writes it
today.** `cnvkit directory` holds both file paths and GridFS ids, because a
version of the upload loop that stopped writing ids there shipped in March 2026
and everything written before still has them. Read the writer to form the
hypothesis; count the values to settle it. `git log -S` on the field name finds
the writer you did not know about.

**Failed uploads, not deletions, are what strand files.** One failed upload left
2,695 files inside a 90-second window. The in-process failure path cleans up
what it stored; a *killed* worker runs no failure path at all, which is why a
periodic sweep is the backstop rather than a gap.

**Two production incidents shaped every safety rule here**, and both were the
same failure: a predicate that lives in the application, re-derived somewhere
else, and drifted. One cleanup script classified a quarter of production as
orphaned when most of those still held their payload; one local-database purge
would have marked tens of thousands of live GridFS files as garbage. This is why
`project_status.py` exists, why the ownership page has no delete control, and
why every sweeper is report-only by default.

---

## 11. Testing

The suite is the record of what has already gone wrong; nearly every file in it
is named after an incident.

```
pytest -m "not slow"     # ~33 s, the loop to use while working
pytest                   # full suite
pytest tests/test_browser.py -m browser --base-url http://localhost:8000
```

The browser tests skip rather than error when no `--base-url` is given, so a
plain `pytest` run reports them skipped and still passes; supply the URL to run
them for real.

Two conventions worth keeping:

- **A test that behaves differently in isolation than in a suite is a flawed
  test, not an environment quirk.** Fix the test.
- **Evaluating a predicate is not exercising a code path.** An emptied project's
  page was reported as rendering correctly on the strength of calling
  `is_empty_project(doc)` and getting `True`; every such page was in fact a 500.
  `tests/test_page_render_matrix.py` renders every page against every status and
  asserts only that no view raises — the status codes are printed as a table
  rather than asserted, because what they *should* be is a product question per
  cell.

---

## 12. The rules that keep being relearned

1. **A backlink is provenance, never authority.** Documents decide a file's fate.
2. **"The code permits X" is not "the data shows X."** A stale key list is a
   hazard; it becomes a defect when something is measured on the other side.
3. **Trace to the writer — and remember the writer had earlier versions.** A
   field's name tells you nothing, and neither does the code that writes it
   today. `git log -S` on the field name finds the writer you did not know about.
4. **Prefer the load-bearing question to the adjacent one.** "Do the key lists
   match?" is adjacent. "Are there orphaned files?" is load-bearing, and one
   command answers it.
5. **Date every measurement and name its scope.**
6. **Every write to production is a separate, explicit decision** — approval for
   one is not approval for the next. Capture a state probe first, write the
   predicted numbers down before running, stage it, keep the undo record.
