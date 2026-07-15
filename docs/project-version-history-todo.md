# TODO: Formal Project Version History Model

## Summary

Project version history currently works through a hybrid schema:

- The latest project document stores active historical versions in `previous_versions`.
- Older extant versions remain as separate `projects` documents.
- Deleted historical versions are represented by lightweight tombstone documents in `projects`.
- Tombstones use `redirect_to_project` so old deleted-version URLs redirect to the latest surviving version.

This is now functional, but the model is not a single source of truth. A future enhancement should introduce a formal version-chain data structure so history rendering, redirects, deletion, promotion, and cleanup all use the same lineage metadata.

## Current Pain Points

- History is split between embedded `previous_versions` entries and tombstone documents.
- Deleted tombstones require special merge logic when rendering project history.
- Reaggregation/new-version creation must retarget tombstones to the new latest project.
- Current-version deletion/promotion must also retarget tombstones.
- Cleanup and recovery tooling must infer version lineage from several fields.
- The semantics of `delete=True` differ between old extant versions, deleted current projects, and purged tombstones.

## Proposed Direction

Introduce a formal project version-chain model. Two reasonable designs:

### Option A: Version Chain Document

Create a new MongoDB collection such as `project_version_chains`.

```python
{
  "_id": ObjectId(...),
  "latest_project_id": "<project ObjectId string>",
  "project_name": "...",
  "versions": [
    {
      "project_id": "<project ObjectId string>",
      "previous_project_id": "<project ObjectId string or None>",
      "state": "active" | "deleted",
      "payload_state": "present" | "purged",
      "date": "...",
      "delete_date": "...",
      "AA_version": "...",
      "AC_version": "...",
      "ASP_version": "...",
      "aggregator_version": "..."
    }
  ]
}
```

Each project/tombstone document should also store:

```python
version_chain_id: ObjectId(...)
```

### Option B: Linked Project Version Nodes

Keep lineage pointers directly on each project/tombstone document:

```python
{
  "_id": ObjectId(...),
  "version_chain_id": ObjectId(...),
  "previous_version_id": "<project ObjectId string or None>",
  "next_version_id": "<project ObjectId string or None>",
  "is_latest": True | False,
  "state": "active" | "deleted",
  "payload_state": "present" | "purged"
}
```

Pointer chasing in backend code is acceptable because project histories are expected to be small, likely fewer than 100 versions.

## Recommendation

Prefer Option A initially: a chain document with ordered version nodes plus `version_chain_id` on every project/tombstone. It gives a compact single source of truth for history rendering and redirects while avoiding many MongoDB round trips. Option B is also viable if keeping lineage directly on project documents is preferred.

## Migration Plan

1. Add helper APIs without changing behavior:
   - `get_version_chain(project_id)`
   - `build_history_from_chain(project_id)`
   - `resolve_latest_project_id(project_id)`
   - `append_project_version(old_project_id, new_project_id)`
   - `mark_version_deleted(project_id, latest_project_id)`
2. Backfill chain documents from existing data:
   - current project documents
   - `previous_versions`
   - deleted-version tombstones
3. Keep writing `previous_versions` during transition for backward compatibility.
4. Switch read paths to the chain helpers:
   - project history table
   - deleted-version URL redirects
   - API `previous_versions` output
   - admin/QC reports
5. Switch write paths:
   - edit/reaggregation new-version creation
   - deleting old versions
   - deleting current version and promoting prior version
   - version recovery tooling
6. Add an audit/validation command that compares old and new history output for every current project.
7. Only after production validation, decide whether `previous_versions` remains as a denormalized compatibility field or is deprecated.

## Risk Areas

- Old project URLs and deleted-version URLs must keep redirecting to the current latest project.
- Current-version deletion/promotion must keep the chain consistent.
- Failed reaggregation rollback must not leave a new latest pointer behind.
- Cleanup scripts must preserve tombstones and live historical versions.
- API consumers may expect the existing `previous_versions` shape.
- Admin/reporting code may still query `previous_versions.linkid` directly.

## Test Requirements

- Deleted old version remains visible in history as deleted.
- Deleted old version URL redirects to latest with a user-facing message.
- Tombstones remain visible after a later reaggregation creates a new latest version.
- Current-version deletion promotes the newest active prior version and keeps tombstones visible.
- Failed reaggregation rollback leaves latest pointer and history unchanged.
- Cleanup scripts preserve all active version nodes and tombstones.
- API output remains backward compatible.

## Suggested GitHub Issue

Title:

```text
Enhancement: introduce formal project version-chain model for history, tombstones, and redirects
```

Labels:

```text
enhancement
```

Body:

```markdown
## Summary

Project version history currently uses a hybrid schema: active prior versions are embedded in the latest project document's `previous_versions` list, while deleted historical versions are represented by lightweight tombstone project documents with `redirect_to_project`.

This works, but the lineage state is split across multiple fields and documents. We should introduce a formal project version-chain model so history rendering, deleted-version redirects, reaggregation, current-version promotion, cleanup, and recovery all use the same source of truth.

## Current Pain Points

- Deleted tombstones have to be merged into the `previous_versions` display path.
- New-version creation must retarget tombstones to the new latest project.
- Current-version deletion/promotion also has to retarget tombstones.
- Cleanup and recovery tooling infer lineage from several fields.
- `delete=True` has multiple meanings depending on context.

## Proposed Direction

Add a formal version-chain model. One possible shape is a `project_version_chains` collection:

```python
{
  "_id": ObjectId(...),
  "latest_project_id": "<project ObjectId string>",
  "project_name": "...",
  "versions": [
    {
      "project_id": "<project ObjectId string>",
      "previous_project_id": "<project ObjectId string or None>",
      "state": "active" | "deleted",
      "payload_state": "present" | "purged",
      "date": "...",
      "delete_date": "...",
      "AA_version": "...",
      "AC_version": "...",
      "ASP_version": "...",
      "aggregator_version": "..."
    }
  ]
}
```

Each project/tombstone document can also store `version_chain_id`.

Pointer-style traversal would also be acceptable because histories are expected to be small, likely fewer than 100 versions.

## Acceptance Criteria

- A single helper API can return the complete version history for any project version.
- A deleted historical version URL redirects to the latest surviving version.
- Deleted versions remain visible in project history after later reaggregation creates a new version.
- Deleting the current version and promoting a previous version keeps the version chain consistent.
- Failed reaggregation rollback does not corrupt latest/history pointers.
- Existing API output remains backward compatible, at least during migration.
- Cleanup tooling preserves active historical versions and tombstones.

## Migration Notes

This should be implemented additively first. Keep writing `previous_versions` during the transition, backfill version-chain documents from existing project docs/tombstones, then move read/write paths over incrementally.
```
