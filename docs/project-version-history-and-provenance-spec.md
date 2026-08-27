# Project Version History and Provenance — Implementation Spec

**Status:** ready to implement
**Supersedes:** `docs/project-version-history-todo.md` (the source of issue #598), which
framed this as an enhancement. The measurements in §2 reclassify it as a
correctness problem.
**Related issues:** #598 (version history model), #622 (the two cleanup-script fixes
that motivated this)
**Measured against:** production and dev, 2026-08-25. Every number in this document
came from a read-only query on that date; none is estimated.

---

## 1 · Purpose

A project version's meaning is currently *inferred* by each consumer from a
combination of four flags, three tombstone fields, one embedded array, and — in
109 of 345 production documents — the **absence** of a field.

There is no function that answers "what is this document." There are 72 call
sites that each answer it themselves, in 21 distinct query shapes.

This spec defines:

1. A single authoritative status model, with one resolver every consumer calls.
2. A schema where field absence is never semantically meaningful.
3. A lineage representation with one direction of authority.
4. A provenance record that makes a project's history readable rather than
   reconstructible.

**The goal is not tidiness.** It is that a person or a script can look at any
project document and know, without guessing, whether it is live, superseded,
deleted, or detached — and how it got that way.

---

## 2 · Why now — the measured problem

### 2.1 The incident that prompted this

`cleanup_orphaned_projects.py` classified **84 of 345** production project
documents as orphaned and deletable. Verified against the application's own
`get_one_project()`:

| | |
| --- | ---: |
| Documents it would have deleted | **84** |
| **Resolvable by URL right now** | **14** |
| Resolvable by project-name lookup | 13 |
| Still holding a GridFS tarfile payload | **77** |

The cause: `get_one_project()` falls back to
`{'_id': …, 'current': False, 'delete': True}` (`caper/utils.py:722`) and again
by `project_name` (`:736`). The script's protection rules covered `delete=False`
and `delete=True AND current=True` — but not that pair.

The same wrong assumption was independently encoded a second time, in
`tests/test_purge_local_db.py::test_reachable_scope_does_not_protect_deleted_non_current_projects`.
Two people, two files, same error.

### 2.2 The near-miss in the other direction

`purge-local-db.py` kept a parallel hand-written GridFS key set. It had drifted
8 keys behind the application. That set decides which files count as
*referenced*, so the drift did not leak storage — it made live files look like
garbage:

| | |
| --- | ---: |
| Referenced ids seen with the stale key set | 903,081 |
| Referenced ids seen with the canonical key set | 948,515 |
| **Live files the stale set would mark deletable** | **80,170** |
| Total `fs.files` | 1,065,019 |

`--smart-gridfs --reference-strategy app-fields --execute` would have deleted
**7.5% of all stored files**, each one named by a live project document.

Both bugs have the same root cause: **a predicate that lives in the
application was re-derived somewhere else, and drifted.**

### 2.3 The state space, as it actually exists

Every observed combination in production (345 documents):

| `delete` | `current` | tombstone | count | referenced as history | has own `previous_versions` | has tarfile |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `False` | `True` | — | 119 | 0 | 43 | 100 |
| `True` | `False` | — | 103 | **89** | 46 | 94 |
| `True` | **absent** | — | **70** | **0** | 1 | 70 |
| `False` | `False` | — | **39** | **0** | 0 | 23 |
| `True` | `True` | — | 12 | 0 | 1 | 9 |
| `True` | `False` | tombstone | 2 | 0 | 0 | 0 |

Dev (303 documents) shows the same six combinations in different proportions,
plus two conditions absent from prod: **3 documents that are `delete=False,
current=True` while also being referenced as another project's history** — a
document that is simultaneously live and superseded.

Read that table carefully. Two rows are the problem:

- **70 documents are `delete=True` with no `current` field at all.** They are
  unreachable *only* because `{'current': False}` does not match a missing
  field. **70 of 70 still hold a tarfile.** Backfilling `current` — an obvious
  hygiene action someone will eventually take — silently makes all 70 reachable
  and changes the behaviour of every cleanup script.
- **39 documents are `delete=False, current=False`.** Reachable by URL, not the
  head of anything, not recorded as any project's history. 23 hold tarfiles.

**109 of 345 documents — 32% — are in a state whose meaning cannot be determined
from the schema.** Not corrupt. Not obviously wrong. Simply undecidable.

### 2.4 Lineage is already torn

`previous_versions[].linkid` values that point at no existing document:

| | Dangling references |
| --- | ---: |
| Production | **2** |
| Dev | **6** |

Nothing detects this today. There is no validator, and a torn chain produces no
error — it produces a history page that is quietly missing an entry.

### 2.5 Provenance barely exists

`project_audit_log` has the right shape — timestamp, user email, project uuid,
event type, AA/AC/ASP versions, sample count, S3 URI and size. It has **one
write site**: `caper/views.py:2855`, covering create and edit.

| | |
| --- | ---: |
| Audit entries | 121 |
| Projects | 345 |

Nothing is recorded for version deletion, version promotion, payload purge,
membership change, visibility change, or permanent delete. When a document's
state is ambiguous — as 109 of them are — there is no record to consult.

### 2.6 What this blocks

Cleanup cannot be trusted. That is not a hypothetical: two cleanup scripts were
each one flag away from destroying production data, and the only reason neither
did is that nobody ran them. Any future storage reclamation, any version
pruning, any migration is gated on being able to answer "what is this document"
— and today nobody can.

---

## 3 · Map of the problem surface

### 3.1 Fields that carry status

| Field | Type on prod | Meaning | Trap |
| --- | --- | --- | --- |
| `delete` | bool, present on 345/345 | "not the live document" — **not** "gone" | The name says the opposite of what it means. Promotion code calls the reverse operation "Un-delete." |
| `current` | bool on 275, **absent on 70** | "is the head of a chain" | Absence is load-bearing and undocumented |
| `previous_versions[]` | array on 91/345 | backwards lineage, on the head only | One direction only; 2 entries dangle |
| `version_deleted_from_history` | bool, 2 docs | tombstone marker | Meaningful only with the next two |
| `payload_purged` | bool, 2 docs | GridFS payload removed | — |
| `redirect_to_project` | string, 2 docs | where the old URL should land | — |
| `linkid` | absent on 208/345 | **display alias for `_id`**, not a key | Looks like a foreign key. `prepare_project_linkid()` (`utils.py:1238`) is one line: `project['linkid'] = project['_id']`. **No query filters on top-level `linkid`.** |

### 3.2 Consumers

| | Count |
| --- | ---: |
| Call sites querying `delete` and/or `current` | **72** |
| Distinct query shapes for the same predicates | **21** |
| References to `previous_versions` | 48 |
| References to the three tombstone fields | 23 |

Sample of the 21 shapes, showing the divergence:

```
{'current': True, 'delete': False}            ×5
{'delete': False, 'current': True}            ×1   # same predicate, other order
{'delete': False}                             ×4
{'_id': <oid>, 'delete': False}               ×3
{'_id': <oid>, 'current': False, 'delete': True}  ×2
{'project_name': …, 'current': False, 'delete': True}  ×2
{'alias_name' : …, 'delete':False}            ×1   # note the spacing
{'alias_name': …, 'delete': False}            ×2   # …and again, differently
```

### 3.3 The resolver these must agree with

`get_one_project()` (`caper/utils.py:683`) resolves, in order:

| Step | Query | Line |
| --- | --- | --- |
| 1 | `{'_id': ObjectId(x), 'delete': False}` | 692 |
| 2 | `{'alias_name': x, 'delete': False}` | 703 |
| 3 | `{'project_name': x, 'delete': False}` | 711 |
| 4 | `{'_id': ObjectId(x), 'current': False, 'delete': True}` | **722** |
| 5 | `{'project_name': x, 'current': False, 'delete': True}` | **736** |

Steps 4 and 5 are the ones every cleanup tool has missed. They log
`"Could not lookup project …, had to use previous project ids!"`, which is the
signature to grep for when auditing.

### 3.4 Reverse lookup is a full scan

`{'previous_versions.linkid': <id>}` — "which project supersedes this one" — is
the only uncovered query in the collection (`COLLSCAN`, 345 documents /
232 MiB). Call sites: `utils.py:227`, `utils.py:673`, `views_admin.py:872`.

This is a *symptom*, not a separate problem: lineage pointers only run
backwards, so answering the forward question requires reading every document.
The design in §5 removes it. **Do not add a standalone index for this** — it
becomes dead weight once lineage is bidirectional.

---

## 4 · Target status model

Exactly five states. Every project document is in exactly one, determinable from
the document alone, with no reliance on field absence.

| Status | Meaning | Reachable by URL | Payload retained |
| --- | --- | --- | --- |
| `LIVE` | current head of a chain, not deleted | yes | yes |
| `SUPERSEDED` | an earlier version of a chain whose head is `LIVE` or `SOFT_DELETED` | **yes** — old links must keep working | yes |
| `SOFT_DELETED` | user-deleted; recoverable from the admin page | admin only | yes |
| `TOMBSTONE` | version removed from history, payload purged, URL redirects | redirects | no |
| `DETACHED` | belongs to no chain and is reachable by nothing | no | **undecided — see §7** |

`DETACHED` is deliberately a named state rather than "everything else." Its
population today is the 109 ambiguous documents, and naming it is what makes
them countable and reviewable instead of invisible.

---

## 5 · Design decision: pointers, with a derived chain view

### 5.1 What Option A (a `project_version_chains` collection) is genuinely good for

Five things, and they are real:

1. **Atomic whole-chain mutation.** Promotion, version deletion and tombstone
   retargeting all touch several documents. In A that is one document write,
   which DocumentDB makes atomic. In B, a crash mid-operation leaves a torn
   chain — and §2.4 shows chains already tear today.
2. **Whole-lineage read in one document.** No pointer chasing, no fan-out.
3. **A home for chain-level facts** that belong to no single version: the
   canonical name across versions, who owns the lineage, whether the lineage is
   retired.
4. **Explicit ordering.** `versions[]` has an order. Deriving order from dates
   in B is unsafe — production contains same-day version pairs (3 of the 12
   name-collision cases in §7 are same-day).
5. **Enumerating lineages** without scanning `projects`. "How many logical
   projects exist" is a question users ask and the current schema cannot answer
   cheaply.

### 5.2 Why A alone is the wrong choice anyway

A chain document is a **second source of truth**. When `chain.versions[]` and
the documents disagree, nothing says which wins, and nothing detects the
disagreement. That is precisely the failure this spec exists to end — it would
recreate the current problem one level up, with a nicer schema.

### 5.3 The hybrid: authority on documents, chain document derived

Adopt **Option B for lineage**, and add a chain document that is a
**materialized view, never a source of truth**.

```
Authority:   the project documents.  previous_version_id / next_version_id /
             is_latest / version_ordinal are written by feature code.

Derived:     project_version_chains.  Rebuilt from documents.  Feature code
             READS it and never WRITES it.

Exception:   chain-level metadata that has no document to live on is
             authoritative on the chain document — and is never duplicated
             onto project documents.
```

This yields A's benefits 2, 3 and 5, keeps B's single source of truth, and
converts A's silent-disagreement failure into a **detected, self-healing** one:
a validator recomputes chains from documents, and on disagreement the chain
document is rebuilt. Documents always win.

Benefit 4 (ordering) is solved directly on the document with an explicit
`version_ordinal`, not by the chain.

Benefit 1 (atomicity) is the cost we accept. Mitigation: every multi-document
lineage mutation writes an audit event **first** (§6.5) describing the intended
end state, so a torn chain is reconstructible rather than merely detectable. The
validator in §8 finds tears; the audit log says what the chain should have been.

**The rule that makes this safe, and it must be enforced by a test:** exactly
one direction of authority, documents → chain, never the reverse.

---

## 6 · Target schema

### 6.1 On each project document

```python
{
  # ---- identity ----------------------------------------------------
  "_id": ObjectId(...),

  # ---- lineage (AUTHORITATIVE) -------------------------------------
  "version_chain_id":     ObjectId(...),   # groups every version of one project
  "previous_version_id":  ObjectId | None, # None on the first version
  "next_version_id":      ObjectId | None, # None on the head
  "version_ordinal":      int,             # 1-based, monotonic, no ties
  "is_latest":            bool,            # true iff next_version_id is None

  # ---- status (AUTHORITATIVE, always present) ----------------------
  "status": "LIVE" | "SUPERSEDED" | "SOFT_DELETED" | "TOMBSTONE" | "DETACHED",
  "payload_state": "present" | "purged",

  # ---- retained for compatibility, written but never read ----------
  "delete":   bool,     # kept in sync during transition; see §9
  "current":  bool,     # ALWAYS PRESENT after backfill — never absent
  "previous_versions": [ ... ],   # denormalised, derived from lineage

  # ---- tombstone ---------------------------------------------------
  "redirect_to_project": ObjectId | None,
}
```

`status` is **stored**, not only computed. Storing it makes queries indexable
and makes the state visible to a human reading the document. The validator in
§8 asserts stored == computed on every document; they may never diverge
silently.

### 6.2 The derived chain document

```python
# collection: project_version_chains        ← DERIVED. Never write from feature code.
{
  "_id": ObjectId(...),
  "head_project_id": ObjectId(...),        # the is_latest member
  "versions": [                            # ordered by version_ordinal
    {"project_id": ObjectId(...), "ordinal": 1, "status": "SUPERSEDED",
     "payload_state": "present", "date": "..."},
    ...
  ],
  "rebuilt_at": datetime,                  # when this view was last derived
  "source_digest": "<sha256 of the member (id, ordinal, status) tuples>",

  # ---- chain-level metadata: AUTHORITATIVE HERE ONLY ---------------
  "canonical_name": "...",
  "retired": bool,
}
```

`source_digest` is what makes staleness cheap to detect: recompute the digest
from documents and compare. No field-by-field diff needed for the common case.

### 6.3 Indexes

| Index | Serves |
| --- | --- |
| `{version_chain_id: 1, version_ordinal: 1}` | whole lineage, in order, one seek |
| `{previous_version_id: 1}`, sparse | reverse lookup — **replaces the COLLSCAN in §3.4** |
| `{status: 1, is_latest: 1}` | listing pages |
| `{head_project_id: 1}` on chains | document → chain |

### 6.4 File-level backlinks — making "is this file orphaned?" a lookup

**The problem this solves.** Today, deciding whether a single GridFS file is
orphaned requires traversing **every** project document — 345 documents,
232 MiB, building a set of 948,515 ids — and diffing it against 1,065,019
`fs.files` rows. There is no way to ask the question about one file. Both
incidents in §2 happened inside exactly that whole-database traversal, and both
were traversal bugs.

Measured on prod, 2026-08-25:

| | |
| --- | ---: |
| Distinct file ids named by project documents | 948,515 |
| `fs.files` total | 1,065,019 |
| **Unreferenced (orphaned) files** | **116,504** |
| Documents naming a file that no longer exists | **0** |
| Total bytes in `fs.files` | 347.1 GiB |

The reachability graph is currently **one-directional**: documents point at
files, files point at nothing. So the only way to evaluate a file is to
reconstruct the whole graph, and a single mistake in that reconstruction is
worth 80,170 files (§2.2).

**The fix, following the same authority rule as §5.3.** GridFS supports a
`metadata` subdocument. Write derived backlinks into it:

```python
# fs.files.metadata          ← DERIVED. Documents remain authoritative.
{
  "project_id":        ObjectId(...),   # the document that named this file
  "version_chain_id":  ObjectId(...),   # which lineage it belongs to
  "sample_name":       "...",
  "feature_key":       "AA_PNG_file",   # which GRIDFS_FILE_KEYS slot
  "written_at":        datetime,
  "written_by_event":  ObjectId(...),   # the project_audit_log entry
}
```

**Authority direction: documents → files, never the reverse.** A file is
retained because a retained document names it. `metadata` is an index into that
fact, not a substitute for it — the validator (I12–I14) asserts they agree, and
on disagreement the metadata is rebuilt from documents.

**What this buys, in the terms of the question that prompted it:**

| Question | Today | With backlinks |
| --- | --- | --- |
| Is this one file orphaned? | traverse 345 docs / 232 MiB | one `find_one` |
| What was this file? | unanswerable | `metadata` says project, version, sample, feature |
| Why is it orphaned? | unanswerable | see the classification below |
| Can I delete it? | only by re-deriving the whole graph correctly | check the named document's `status` |

**Orphan classification becomes possible**, which is the part that matters for
deciding what is safe to remove:

| `metadata` state | Meaning | Safe to delete |
| --- | --- | --- |
| absent | written before this change, **or** by an ingestion that crashed before recording it | after backfill, absent ⇒ genuinely stranded |
| names a document that exists and still references it | live file | **no** |
| names a document that exists but no longer references it | residue of a version edit | yes, once the document's `status` is confirmed |
| names a document that no longer exists | residue of a purge or permanent delete | yes |
| names a `TOMBSTONE` document | payload was supposed to be purged and was not | yes — and that is a bug worth reporting |

**Synergy with #620.** That fix discards GridFS files written before the
document that names them could be updated. Writing `metadata` at `fs.put()`
time — carrying the *intended* `project_id` — means even a crashed ingestion
leaves a file that says what it was for. The orphan-factory failure mode becomes
self-describing rather than anonymous.

**Scale caution.** 1,065,019 files. Backfilling `metadata` is a large one-time
operation and must be batched, resumable, and idempotent. It is read-mostly
against `projects` and write-only against `fs.files`, so it can run without
downtime, but it should run on dev first and be measured there.

**This section is a prerequisite for reclaiming the 116,504 orphans — it is not
that reclamation.** Deleting them stays out of scope (§12). The point is to make
the decision *checkable per file* instead of resting on a whole-database
traversal being correct, because that traversal has now been wrong twice.

### 6.5 The provenance event

Extend the **existing** `project_audit_log` collection and the existing helper
at `caper/views.py:2855`. Do not build a new mechanism; the shape is already
right and already carries user, timestamp and tool versions.

```python
{
  "timestamp": datetime,
  "user_email": "...",
  "project_uuid": "...",
  "version_chain_id": ObjectId(...),   # NEW — ties events to a lineage
  "project_name": "...",
  "event_type": "...",                 # see the table below
  "before": {"status": ..., "is_latest": ..., "version_ordinal": ...},
  "after":  {"status": ..., "is_latest": ..., "version_ordinal": ...},
  "AA_version": "...", "AC_version": "...", "ASP_version": "...",
  "sample_count": int,
  "s3_uri": "...", "s3_file_size_bytes": int,
}
```

Event types to add. Existing: `CREATE`, `EDIT_NEW_VERSION`, `EDIT_NO_VERSION`.

| New event | Written when |
| --- | --- |
| `VERSION_DELETED` | a non-head version is removed from history |
| `VERSION_PROMOTED` | a previous version becomes the head |
| `PAYLOAD_PURGED` | GridFS payload deleted, tombstone created |
| `SOFT_DELETED` / `RESTORED` | user deletes / un-deletes |
| `PERMANENTLY_DELETED` | admin permanent delete |
| `MEMBERS_CHANGED` | project_members modified |
| `VISIBILITY_CHANGED` | private/public/hidden_public change |
| `PROJECT_EMPTIED` | the last non-tombstone version was deleted (T7, T8) |
| `PROJECT_REPOPULATED` | a version was uploaded into an empty chain (T9) |
| `CHAIN_REPAIRED` | the validator rebuilt or repaired a lineage |

**Write the event before the mutation**, carrying the intended `after` state.
That is what makes a torn multi-document chain reconstructible (§5.3).

---

## 6A · Worked transitions

The schema in §6 is only useful if every mutation has one defined before/after.
These are the five that matter. Each is written as the state change, the events
emitted, and the invariants that must survive it.

**Read `delete_project_version()` (`caper/views.py:2914` onward) alongside this.**
It is the operation these replace, and it is where the current model hurts most.

### T1 · New version created (re-aggregation)

```
before:  A[ordinal=1, is_latest=True,  status=LIVE]
after:   A[ordinal=1, is_latest=False, status=SUPERSEDED, next_version_id=B]
         B[ordinal=2, is_latest=True,  status=LIVE, previous_version_id=A,
           version_chain_id = A.version_chain_id]
events:  EDIT_NEW_VERSION  (before/after on A and B)
holds:   I3 (one head), I4 (contiguous ordinals), I5 (mutual inverses)
```

A keeps its payload. `SUPERSEDED` is reachable by URL — that is the point.

### T2 · A non-head version is deleted from history

This is the case the question "replace a historical version with a tombstone and
delete the old project" describes.

```
before:  A[ordinal=1, SUPERSEDED] -> B[ordinal=2, SUPERSEDED] -> C[ordinal=3, LIVE]
delete B
after:   A[ordinal=1, SUPERSEDED, next_version_id=B]      <-- unchanged
         B[ordinal=2, status=TOMBSTONE, payload_state=purged,
           redirect_to_project=C, previous_version_id=A, next_version_id=C]
         C[ordinal=3, LIVE, previous_version_id=B]        <-- unchanged
events:  VERSION_DELETED, then PAYLOAD_PURGED
holds:   I4 (B keeps ordinal 2 — the chain does NOT renumber),
         I5 (A<->B and B<->C still mutual), I8 + I14 (no GridFS ids remain on B)
```

**The tombstone stays in the chain.** It is a node whose payload is gone, not a
node that is gone. That is what keeps `/project/<B>` resolving, and it is why
ordinals must not be renumbered on delete — renumbering would break I4 for every
downstream version and invalidate every audit event referencing the old ordinal.

Contrast with today: B is `replace_one`'d wholesale by a tombstone document, its
position in history is reconstructed by merging `previous_versions[]` with
tombstone documents, and `retarget_deleted_version_tombstones()` must walk and
repoint every other tombstone. In the target model, B's neighbours do not change
at all.

### T3 · The head version is deleted, a previous version is promoted

```
before:  A[ordinal=1, SUPERSEDED] -> B[ordinal=2, LIVE]
delete B
after:   A[ordinal=1, status=LIVE, is_latest=True, next_version_id=None]
         B[ordinal=2, status=TOMBSTONE, payload_state=purged,
           redirect_to_project=A, previous_version_id=A, next_version_id=None]
events:  VERSION_DELETED, VERSION_PROMOTED, PAYLOAD_PURGED
holds:   I3 (exactly one is_latest — A), I7 (A is LIVE and no longer a member
         of anyone else's chain)
```

Note the shape: `is_latest` moves backwards along the chain while ordinals stay
fixed. `is_latest` is position-in-time; `version_ordinal` is identity. Conflating
them is how the current model ends up needing "Un-delete."

**Two hazards this transition carries today, both must be fixed here:**

1. **Promotion order comes from array position.** `prev_versions_list[-1]`
   (`views.py:2944`) picks the last element of `previous_versions[]`. Nothing
   guarantees that array is ordered, and production contains same-day version
   pairs where a date sort would also tie. **Use `version_ordinal`** — promote
   `max(ordinal)` among surviving non-tombstone members.
2. **Project-level metadata is carried forward by a hardcoded list** — see D12.

### T4 · Project soft-deleted, then restored

```
before:  A[SUPERSEDED] -> B[LIVE]
after:   A[SUPERSEDED] -> B[status=SOFT_DELETED, is_latest=True]
events:  SOFT_DELETED / RESTORED
holds:   I3 (B is still the head — soft delete does not move the head),
         payload retained on both
```

`SOFT_DELETED` applies to the head and therefore to the whole lineage. It is a
visibility state, not a lineage state. A `SUPERSEDED` member of a soft-deleted
chain stays `SUPERSEDED`.

### T5 · Permanent delete of an entire project

```
before:  A[SUPERSEDED] -> B[SUPERSEDED] -> C[SOFT_DELETED, is_latest]
after:   every member TOMBSTONE, payload_state=purged, redirect_to_project=None
         chain retained, marked retired=True on the chain document
events:  PERMANENTLY_DELETED (one per member)
holds:   I8, I14 on every member
```

**Documents are not removed.** The lineage survives as tombstones so old URLs
give "this project was deleted" rather than a 404, and so the audit trail still
resolves. Removing the documents is a separate decision and is out of scope
(§12).

### T6 · The chain is the project — and "empty" is a chain state

The transitions below need one thing named first. In the target model the
**chain document is the project**; the version documents are its history. That
is what makes the terminal cases expressible, because today there is no project
entity separate from its versions, so deleting the last version deletes the
thing that owned the name, the members and the URL.

```
EMPTY  ==  every member of the chain has status == TOMBSTONE
```

`EMPTY` is therefore **not a sixth document status**. It is a derived property
of the chain, and it must stay derived — a stored flag would be the same
second-source-of-truth mistake as everything else here.

An empty project is **live and appendable**, not dead:

- its URL resolves, to a shell page rather than a 404
- `canonical_name`, members, visibility and ownership survive on the chain
- a new upload appends `ordinal = max(ordinal) + 1` and the chain is non-empty
  again

**This answers D12.** The chain-level / version-level split is not a matter of
taste: **a field is chain-level exactly when it must survive the deletion of
every version.** Name, members, visibility, ownership and alias must. AA/AC
versions, sample data, classifications and payloads must not. Adjudicate the six
disputed fields in D12 against that test.

**Note on the existing `EMPTY?` field.** It already exists and is already
unreliable: on prod, **22 live projects have no `runs`, and only 3 carry
`EMPTY?: True`.** `is_empty_project` (`views.py:875`) survives on a fallback
(`or not project.get('runs')`). Do not extend that field. Derive emptiness from
the chain, and treat `EMPTY?` as legacy to be read-only during transition.

### T7 · Deleting the head when no restorable version remains

Generalises T3. Promotion targets **the highest `version_ordinal` among members
whose status is not `TOMBSTONE`** — never array position, never date.

```
before:  A[ordinal=1, TOMBSTONE] -> B[ordinal=2, LIVE]
delete B
after:   A[ordinal=1, TOMBSTONE]                       <-- not restorable
         B[ordinal=2, TOMBSTONE, payload_state=purged,
           is_latest=True, redirect_to_project=None]
         chain: EMPTY  (every member is a TOMBSTONE)
events:  VERSION_DELETED, PAYLOAD_PURGED, then PROJECT_EMPTIED
holds:   I3 (B keeps is_latest — position, not state), I4, I8, I14
```

`redirect_to_project` is `None` because there is nowhere to redirect *to*. The
URL resolves to the empty-project shell. That is the distinction between an
empty project and a deleted one, and it is why T7 does not remove documents.

### T8 · Deleting the only version

The degenerate case of T7, and the one the current code gets wrong.

```
before:  A[ordinal=1, is_latest=True, LIVE]      (no previous versions)
delete A
after:   A[ordinal=1, is_latest=True, TOMBSTONE, payload_state=purged,
           redirect_to_project=None]
         chain: EMPTY
events:  VERSION_DELETED, PAYLOAD_PURGED, PROJECT_EMPTIED
holds:   I8 + I14 — **the payload must actually be purged**
```

**What the current implementation does instead** (`views.py:3012`, "Case 3"):

| | Case 1 / Case 2 | **Case 3 (sole version)** |
| --- | --- | --- |
| purge GridFS | yes | **no** |
| `payload_purged` | set | **not set** |
| `redirect_to_project` | set | **not set** |
| resulting document | complete tombstone | `delete=True, current=False` + `version_deleted_from_history` only |

The log line reads `"project fully removed"`. It is not removed: the document is
still resolvable through `utils.py:722`, and its entire GridFS payload is still
stored and still billed. The user is redirected to `/accounts/profile/` as if
the project were gone.

**Measured on prod: 0 documents in this partial state**, so the path is latent
rather than damaging — but it is unexercised, not correct. It is also a
plausible source of the 14 documents in §2.1 that are reachable while referenced
by nothing.

**Requirement:** T8 must purge the payload and write a complete tombstone, the
same as every other deletion path. There must be exactly one tombstone-creation
routine, used by T2, T3, T5, T7 and T8 alike — a fifth divergent code path for
the degenerate case is how this bug happened.

### T9 · Re-populating an empty project

```
before:  A[ordinal=1, TOMBSTONE]  chain EMPTY
upload
after:   A[ordinal=1, TOMBSTONE, is_latest=False, next_version_id=B]
         B[ordinal=2, is_latest=True, LIVE, previous_version_id=A]
         chain: not empty
events:  CREATE  (or EDIT_NEW_VERSION), PROJECT_REPOPULATED
holds:   I3, I4 (ordinals still contiguous — A was never renumbered), I5
```

A tombstone is a legitimate `previous_version_id`. History reads
"version 1 deleted, version 2 current", which is true and is exactly what the
current model cannot represent.


### The atomicity gap, stated plainly

T2, T3 and T5 each touch several documents plus GridFS plus site statistics.
DocumentDB will not make that atomic without transactions, and the current
implementation does it as an unguarded sequence: update the promoted document →
purge GridFS → replace the old document with a tombstone → retarget other
tombstones → update statistics. A crash at any step leaves an inconsistent
state, and nothing detects it.

The mitigation required by §5.3: **write the event first**, carrying the
intended `after` state for every document the transition will touch. Then a torn
transition is not merely detectable by the validator (§8) — the audit log says
what the end state should have been, so `CHAIN_REPAIRED` can finish the job.


---

## 7 · Danger cases

Each of these is a real population, measured. An implementation that does not
handle them will destroy data or break URLs.

### D1 · `delete=True` means "not live", not "deleted" — **103 documents**

`SUPERSEDED` versions carry `delete=True` and are **reachable by URL**
(`utils.py:722`, `:736`). 14 of them are reachable *and* not referenced by any
chain. Any code that reads `delete=True` as "safe to remove" destroys live
links and payloads.

**Test:** a `SUPERSEDED` document must resolve through `get_one_project()` by
both `_id` and `project_name`, before and after migration.

### D2 · Field absence is load-bearing — **70 documents**

`{'current': False}` does not match a document with no `current` field. All 70
hold a tarfile. Backfilling `current` changes cleanup behaviour for all of them
at once.

**Requirement:** backfill `current` and `status` **in the same migration step**,
so no window exists where the documents are newly reachable but not yet
classified. Do not backfill `current` on its own — that is the dangerous half.

### D3 · `delete=False, current=False` — **39 documents**

Reachable by URL, not the head of any chain, referenced by no
`previous_versions`. 23 hold tarfiles. The schema cannot say whether these are
drafts, abandoned uploads, or unlinked predecessors.

**Requirement:** classify as `DETACHED`, retain, report. Do not guess.

### D4 · Unlinked predecessors — **12 documents**

12 of the 70 share an exact `project_name` with a live project, and **9 of the
12 predate their live namesake** (e.g. `Welm` 2023-04-05 → live `Welm`
2023-04-17; `CCLE` 2023-09-12 → live `CCLE` 2026-07-17). None appears in the
live project's `previous_versions` — verified explicitly, including for live
projects that *do* have chains (the live `CCLE` has 3 previous versions; neither
`CCLE` review document is among them).

These are almost certainly earlier uploads that were re-uploaded rather than
re-aggregated, so no chain was ever created. **They are history in every sense
except the recorded one.**

**Requirement:** the migration must **not** infer lineage from name matches.
Surface them in a report for human adjudication. A name is not a key —
`project_name` is not unique, and using it to link chains would fabricate
provenance. Fabricated provenance is worse than absent provenance.

### D5 · Dangling lineage references — **2 on prod, 6 on dev**

`previous_versions[].linkid` values pointing at documents that do not exist.
Migration must not abort on these and must not silently drop them.

**Requirement:** record each as a `CHAIN_REPAIRED` audit event naming the
dangling id, and continue.

### D6 · Simultaneously live and superseded — **3 on dev, 0 on prod**

A document that is `delete=False, current=True` while being referenced as
another project's `previous_versions`. The five-state model forbids this.

**Requirement:** detect, report, do not auto-resolve. Prod is clean today;
dev is where the fixture for this case should live.

### D7 · The GridFS key list

Three separate hand-written copies of this list have existed; the last drift was
worth 80,170 live files (§2.2). `caper/caper/project_version_cleanup.py` holds
the canonical `GRIDFS_FILE_KEYS` / `iter_gridfs_file_ids`.

**Requirement:** any new code touching GridFS imports them. A test must assert
no module defines its own list. Grep guard: a test that fails if
`'AA PNG file'` appears as a string literal outside
`project_version_cleanup.py` and the test suite.

### D8 · `linkid` is not a foreign key

`prepare_project_linkid()` sets `project['linkid'] = project['_id']` on an
in-memory dict for templates. It is stored on only 137/345 documents and **no
query filters on it**. It is meaningful only *inside* `previous_versions[]`.

**Requirement:** do not migrate top-level `linkid` into the new model, and do
not index it. Removing the ambiguity between "display alias" and "lineage edge"
is one of the points of this work.

### D9 · Documents are large

`projects` averages ~690 KB per document (232 MiB / 345) because `runs` is
embedded. Migration must project away `runs` when reading, and must use targeted
`$set` updates rather than whole-document replacement. A naive
`find({})`-then-`replace_one` migration will move ~460 MiB unnecessarily and may
exhaust memory on a full pass.

### D10 · Prod and dev share one DocumentDB cluster

Both resolve to `ampliconubuntu.cluster-…`, differing only by `DB_NAME`
(`caper` vs `caper-dev`). A migration script that takes the database name from
anywhere other than `DB_NAME` — or that iterates databases — can write to
production while "running on dev."

**Requirement:** the migration asserts its target database name explicitly and
refuses to run against `caper` without an additional flag.

### D11 · File orphan status is not answerable per file — **116,504 orphans**

Deciding whether one GridFS file is orphaned currently requires rebuilding the
entire reachability graph: 345 documents, 232 MiB, 948,515 ids, diffed against
1,065,019 `fs.files` rows. Both incidents in §2 occurred inside that
reconstruction.

Measured on prod: **116,504 unreferenced files** out of 1,065,019, and
**0 documents naming a file that no longer exists** — the graph is currently
clean in the document→file direction, and that is worth locking in as an
invariant before it stops being true.

**Requirement:** implement §6.4. Until a file carries a backlink, every deletion
decision about it rests on a traversal being correct, and correctness of that
traversal is not locally checkable.

### D12 · Version promotion drops project-level fields — hardcoded carry-forward list

`delete_project_version()` copies exactly **9** fields from the deleted head onto
the promoted version (`views.py:2955`): `project_members`, `subscribers`,
`views`, `downloads`, `alias_name`, `publication_link`, `private`, `privateKey`,
`featured`.

Live production projects carry **25 further fields** not in that list. Most are
legitimately version-owned and *should* come from the promoted version
(`Classification`, `Oncogenes`, `aggregate_df`, `sample_data`,
`reference_genome`, `description`). Several are not:

| Field | On N of 119 live projects | Why it looks project-level |
| --- | ---: | --- |
| `owner` | 16 | ownership is not a property of an aggregation run |
| `project_downloads` | 56 | a counter — and `downloads` **is** copied |
| `sample_downloads` | 39 | same |
| `sample_name_remap_enabled` | 21 | a project setting |
| `original_project_name` | 16 | project identity |
| `alias` | 2 | distinct field from the copied `alias_name` |

The tell is `downloads` being copied while `project_downloads` and
`sample_downloads` are not: the list was written once and later fields were never
added to it. **This is the same hand-maintained-list defect as the GridFS keys
(D7), in a fourth location.**

**Requirement:** replace the hardcoded list with an explicit split — declare
which fields are *chain-level* (carried, or better, stored once on the chain
document per §6.2) and which are *version-level* (never carried). Add a test that
fails when a project document grows a field belonging to neither set, so the
next addition cannot silently fall through.

**Adjudicated 2026-08-27.** The six were measured against prod before the
decision: 45 multi-version chains, 43 of which hold `project_downloads` on more
than one version, and PCAWG's live version reporting 30 of the chain's 1,430
project downloads and 2,898 of its 114,310 sample downloads.

| Field | Level | Rationale |
| --- | --- | --- |
| `project_downloads` | **chain** | Displayed total is the sum across the chain. Per-version dicts stay on their versions — the detail is kept, it is simply not what the site shows. |
| `sample_downloads` | **chain** | Same. |
| `alias` / `alias_name` | **chain** | A URL identity. It names the project, not an aggregation run. `alias` is the form field and `alias_name` the stored one; the two spellings are a separate cleanup. |
| `sample_name_remap_enabled` | **version** | Re-derived from the upload form at each re-aggregation; it describes how *that* version's samples were ingested. |
| `owner` | **neither** | Placeholder scaffolding: set at insert, `$unset` when the real project replaces it. It must not exist on a finished project, which is what `clear_stale_uploads.py` uses to find crashed uploads. |
| `original_project_name` | **neither** | Same. |

Chain-level here means *displayed as the chain's value*, not that the per-version
data is discarded. Nothing in the adjudication requires a migration: the old
documents still hold their entries, so summing across the chain recovers the
history that re-aggregation has been hiding.

### D13 · The sole-version deletion path is a fifth, divergent code path

`delete_project_version()` Case 3 (`views.py:3012`) handles "no previous
versions" separately from Case 1 and Case 2, and diverges from both: it does not
purge GridFS, does not set `payload_purged`, does not set `redirect_to_project`,
and logs `"project fully removed"` for a document that stays resolvable via
`utils.py:722` with its full payload intact.

**0 documents are in this state on prod** — the path is latent. It is still a
plausible origin for the 14 documents in §2.1 that are reachable while
referenced by nothing.

**Requirement:** T8. One tombstone-creation routine, used by every deletion
path (I18). The degenerate case must not get its own implementation.

### D14 · `EMPTY?` does not mean empty

On prod, **22 live projects have no `runs` and only 3 carry `EMPTY?: True`.**
`is_empty_project` (`views.py:875`) is correct only because it falls back to
`not project.get('runs')`.

**Requirement:** derive emptiness from the chain (T6). Do not write `EMPTY?`;
read it only during transition, and drop it in Phase 4.

---

## 8 · Invariants and the validator

Ship a validator command that runs read-only in CI and on demand. Each invariant
is a hard assertion with a named error.

| # | Invariant |
| --- | --- |
| I1 | Every project document has `status`, `current`, `delete`, `version_chain_id`, `version_ordinal`, `is_latest` — **all present, none absent** |
| I2 | Stored `status` equals `classify(doc)` recomputed from the document |
| I3 | Exactly one `is_latest=True` document per `version_chain_id` |
| I4 | `version_ordinal` is unique and contiguous from 1 within a chain |
| I5 | `previous_version_id` / `next_version_id` are mutual inverses |
| I6 | Every `previous_version_id` / `next_version_id` resolves to an existing document, or is recorded as a known dangling reference |
| I7 | No document is both `LIVE` and referenced as another chain's member (D6) |
| I8 | `payload_state == "purged"` implies `status == "TOMBSTONE"` and no GridFS ids remain in the document |
| I9 | Chain document `source_digest` matches the digest recomputed from its members |
| I10 | `get_one_project()` resolves every `LIVE` and every `SUPERSEDED` document by `_id` |
| I11 | The denormalised `previous_versions[]` matches the lineage derived from pointers (during the compatibility window only) |
| I12 | Every GridFS id named by a retained document exists in `fs.files` — **currently 0 violations on prod; keep it that way** |
| I13 | Every `fs.files` row whose `metadata.project_id` names an existing document is still referenced by that document |
| I14 | No `TOMBSTONE` document has any GridFS id remaining, and no `fs.files` row points at one |
| I15 | A chain is `EMPTY` iff every member is a `TOMBSTONE` — never stored, always derived |
| I16 | Every chain, including an `EMPTY` one, has exactly one `is_latest` member; `is_latest` is position, `status` is state, and they are independent |
| I17 | Every field declared chain-level survives the emptying of a chain (T6); no version-level field does |
| I18 | Exactly one tombstone-creation routine exists, and every deletion path calls it |

**I2 and I10 are the ones that would have prevented both incidents in §2.**

---

## 9 · Phased implementation

Each phase is independently shippable and independently revertible. **Do not
begin a phase before the previous one has soaked.**

### Phase 0 — one resolver, no data change

Create `caper/caper/project_status.py`:

```python
LIVE, SUPERSEDED, SOFT_DELETED, TOMBSTONE, DETACHED = ...

def classify(doc) -> str
    """In-memory classification of a loaded document."""

STATUS_QUERIES: dict[str, dict]
    """The Mongo filter selecting each status. Same semantics as classify()."""

def is_reachable_by_url(doc) -> bool
    """Mirrors get_one_project() steps 1–5 (utils.py:692,703,711,722,736)."""
```

- Both forms are required: scripts need queries, views need in-memory checks.
- **Ship with a test that runs `classify()` and `STATUS_QUERIES` over every
  document in the database and fails if they disagree.** This cross-check is
  the mechanism, not a nicety.
- Replace all 72 call sites (§3.2). Add a grep-guard test that fails if
  `'delete':` or `'current':` appears in a query literal outside
  `project_status.py`.
- **No schema change. No migration. Fully revertible.**

This phase alone unblocks cleanup work, and is the smallest change that would
have prevented both incidents.

### Phase 1 — normalise, backfill, validate

- Backfill `current` and write `status` in **one** update per document (D2).
- Backfill `version_chain_id`, `previous_version_id`, `next_version_id`,
  `version_ordinal`, `is_latest` from existing `previous_versions[]`.
- **Do not infer lineage from `project_name`** (D4). Documents with no recorded
  lineage become single-member chains with `status: DETACHED`.
- Emit a report: every `DETACHED` document, every dangling reference, every D6
  conflict, with names, dates, creators and payload presence.
- Ship the validator (§8) and run it. It must pass before Phase 2.

Also normalise the one boolean `private: True` document to `'private'` while
touching every document anyway — it is a schema irregularity, **not** an
exposure risk (all queries use `$in` across both forms).

### Phase 2 — switch reads, then writes

- Switch read paths to lineage pointers: history rendering, redirects, API
  `previous_versions` output, admin reports.
- Keep writing the denormalised `previous_versions[]`; invariant I11 holds it to
  the pointers.
- Then switch write paths: new-version creation, version deletion, current
  deletion + promotion, recovery tooling.
- Add the derived `project_version_chains` view and its rebuild command. Feature
  code reads it; only the rebuild command writes it (§5.3).

### Phase 2b — file backlinks

Implement §6.4. Write `metadata` at `fs.put()` time for all new uploads first —
that is small and stops the problem growing — then backfill the existing
1,065,019 files in resumable batches. Run and measure on dev before prod.

Ship the per-file orphan classifier from §6.4 as a read-only report. **Do not
delete anything with it.**

### Phase 3 — provenance

- Extend the existing helper (`views.py:2855`) with the event types in §6.5.
- Write the event **before** each mutation, carrying the intended `after` state.
- Add `version_chain_id` to every event.
- Add an admin view: given a project, show its full event history.
- **Backfill what can be honestly backfilled and nothing more.** `CREATE` events
  can be synthesised from `date` + `creator`. Everything else is unknown and
  must stay unknown — a fabricated event is worse than a gap.

### Phase 4 — retire compatibility

Only after Phase 2 has soaked and the validator has been green continuously:
drop the denormalised `previous_versions[]` reads, and decide whether `delete`
and `current` remain as compatibility fields or are removed. **Removing them is
optional and low value; leaving them is fine if I1 and I2 hold.**

---

## 10 · Test requirements

Beyond the invariants, these behaviours must be covered:

1. A `SUPERSEDED` version resolves by `_id` **and** by `project_name`, before
   and after migration (D1).
2. A deleted old version stays visible in history, marked deleted.
3. A deleted-version URL redirects to the surviving head with a user-facing
   message.
4. Tombstones survive a later re-aggregation that creates a new head.
5. Deleting the current version promotes the newest surviving prior version and
   keeps tombstones visible.
6. A failed re-aggregation rollback leaves the head pointer and history
   unchanged.
7. Cleanup scripts preserve every `LIVE`, `SUPERSEDED`, `SOFT_DELETED` and
   `TOMBSTONE` document, and delete no `DETACHED` document without an explicit
   flag naming it.
8. API `previous_versions` output is byte-identical before and after Phase 2.
9. `classify()` and `STATUS_QUERIES` agree over the entire database.
10. No module outside `project_version_cleanup.py` defines a GridFS key list
    (D7).
11. Each transition T1–T5 in §6A produces exactly the stated end state, and the
    validator passes after each — including a same-day version pair, where
    promotion must pick by `version_ordinal` and not by date or array position.
12. A version deleted from the middle of a chain leaves its neighbours' pointers
    and ordinals unchanged (T2), and `/project/<deleted>` still resolves.
13. Promotion preserves every field declared chain-level and no field declared
    version-level (D12); a project document carrying a field in neither set
    fails the test.
14. Deleting the sole version of a project purges its payload and writes a
    complete tombstone (T8, D13) — the assertion is on `fs.files`, not on the
    document alone.
15. Deleting the head when every remaining member is a tombstone empties the
    chain rather than promoting a tombstone (T7).
16. An empty project's URL resolves to a shell, its name, members and visibility
    survive, and a subsequent upload appends `max(ordinal)+1` without
    renumbering the tombstones (T9).
17. `EMPTY` is never read from a stored field (I15).

### Test fixtures — build these on dev

The reason these bugs kept requiring production data to find is that **dev has
no fixture containing the awkward states**. Seed dev with at least one document
of each:

| Fixture | Mirrors |
| --- | ---: |
| `SUPERSEDED` with `current=False, delete=True`, referenced by a head | 89 prod docs |
| `SUPERSEDED` reachable but referenced by nothing | 14 prod docs |
| soft-deleted with **no** `current` field, holding a tarfile | 70 prod docs |
| `delete=False, current=False` holding a tarfile | 39 prod docs |
| tombstone triple with `payload_purged` | 2 prod docs |
| dangling `previous_versions[].linkid` | 2 prod / 6 dev |
| live document also referenced as history | 3 dev docs |
| name collision between a detached doc and a live project | 12 prod docs |

**All development and testing happens on dev.** Production is for read-only
measurement only.

---

## 11 · Migration safety

- **PITR is ~2 days deep until roughly 2026-09-28** — DocumentDB retention was
  raised on 2026-08-24 and the window is still filling. Take a manual cluster
  snapshot immediately before any Phase 1 or Phase 2 run against prod.
- Every migration step is idempotent and re-runnable.
- Every migration step is **report-only by default**; mutation requires
  `--execute` (the pattern now used in `cleanup_orphaned_projects.py`).
- Capture a digest of `(_id, status, is_latest, version_ordinal,
  previous_version_id, next_version_id)` for every document before and after,
  and diff it. A migration that changes a document it did not intend to touch
  must be visible.
- Assert the target database name explicitly (D10).

---

## 12 · Explicitly out of scope

- **Deleting anything.** This spec makes `DETACHED` documents *countable and
  reviewable*. Deciding their fate is a separate, human decision. The 109
  ambiguous documents stay put.
- **Linking the 12 name-collision documents** into their apparent chains (D4).
  Requires human adjudication; a name match is not evidence.
- **A standalone `previous_versions.linkid` index** (§3.4). The new lineage
  indexes replace it.
- **Reclaiming the 116,504 orphaned GridFS files** (~11% of 1,065,019; total
  store 347.1 GiB). §6.4 makes each deletion *checkable per file* rather than
  dependent on a whole-database traversal; actually deleting them is a separate,
  human-approved exercise. An earlier estimate of ~239,000 circulated internally
  and was **wrong** — it is corrected here and measured in §6.4.
- **Changing how `runs` is embedded.** Related (it is why documents are 690 KB)
  but a separate problem with a separate risk profile.

---

## 13 · Definition of done

- Zero documents fail any invariant in §8.
- `classify()` and `STATUS_QUERIES` agree over every document in both databases.
- Zero query literals containing `'delete':` or `'current':` outside
  `project_status.py`.
- Zero GridFS key lists outside `project_version_cleanup.py`.
- The reverse lookup `{previous_version_id: …}` is an `IXSCAN`.
- Every state transition in §6.5 writes an audit event, written **before** the
  mutation and carrying the intended end state.
- Every transition in §6A is covered by a test that asserts the full end state,
  not just the changed field.
- No hardcoded field list governs promotion; the chain-level / version-level
  split is declared and test-enforced (D12).
- Every `fs.files` row carries `metadata` naming its project, chain, sample and
  feature key — or is positively identified as stranded.
- "Is this file orphaned?" is answerable with a single `find_one`, and its
  answer is classified by the table in §6.4.
- The `DETACHED` report is produced, reviewed, and its contents recorded — with
  no document and no file deleted as part of this work.
