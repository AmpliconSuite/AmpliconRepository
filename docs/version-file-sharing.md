# Sharing unchanged files between versions (#624)

As of 2026-11-03.

Every edit of a project makes a whole new version through
`extract_project_files`: replace, append (including the
`ProjectFileAddView` API path), re-aggregate, remove samples, and rename
samples. AC replacement, as planned on #624, will go through the same step.
Until this change, each new version stored a fresh GridFS
copy of every file it names, even when the bytes were identical to the
previous version's. Now a version reuses those files instead.

| Decision | Settled as |
| --- | --- |
| Scope | One version chain. A version reuses files only from the version it was made from, its immediate predecessor |
| Match | Same SHA-256, length and GridFS filename, in the same sample and field. The sample and field are read from the predecessor document, not the file's metadata |
| Files written before this change | Carry no hash until `backfill_gridfs_sha256.py` adds one; a file without a hash is never matched |
| Project archive (`tarfile`) | Stays per version, as decided on #624. Its contents are never identical between versions anyway (`run.json` embeds paths) |
| Backlink `metadata.project_id` | Still names the version that **wrote** the file |
| Who else names it | `metadata.reused_by`, a list written **before** the reusing document names the file |
| Authority for deletion | The documents. Backlinks only add documents to read |

## Why it is possible now

Byte-identical re-aggregation needed two earlier changes, both on 2026-11-02:
aggregator 8.3.1 (#714) and deterministic directory packing (#712). Measured on
dev, a 17-sample project re-aggregated twice gave 296 of 296 directory tarballs
identical, and every other per-file slot identical as well.

## Measured locally, 2026-11-03 (PST)

A project was created through the site and re-aggregated through the edit
page. Then v1 was deleted through the site.

| | v1 stored | v2 stored | v2 reused from v1 |
| --- | --- | --- | --- |
| Contino (9 samples) | 324 files, 76.9 MiB | 1 file, 26.0 MiB (the archive) | 323 files, 50.9 MiB |
| CoRAL (4 samples) | 58 files, 18.0 MiB | 1 file, 7.6 MiB (the archive) | 57 files, 10.4 MiB |

After v1 was deleted, every v2 file was still present and read back whole
(324/324 and 58/58), and the files only v1 named were gone.

## Deletion

Before sharing, every deletion path relied on a measured fact: no GridFS file
was named by two documents (0 of 1.55 million on 2026-08-31). Sharing ends
that, so every application path that deletes a version's payload now first asks
`version_file_sharing.files_still_needed()` and keeps what it returns. That
function reads:

- every member of the victim's chain;
- every version the victim's `previous_versions` names;
- every document the candidate files' backlinks name: the writer, and each
  version listed in `reused_by`.

A file is kept if any of those documents names it. It is also kept if a version
listed in `reused_by` is still being written (`FINISHED?` is False and the
upload has not failed). That version may have claimed the file but not yet
saved the runs that name it.

| Path | What changed |
| --- | --- |
| `delete_project_version`, all three cases | The tombstone's pending list leaves out what other versions need. Before, it protected only the head (or the promoted version), which misses a file an *older* version shares with the one being deleted |
| `version_purge` (and the resume at worker start) | Asks again before each batch, because a version made since may have reused one of the files. If the check cannot be read, the purge stops with the rest still pending |
| `admin_permanent_delete_project` (and account deletion) | Passes the answer as `protected_file_ids`. If the check cannot be read, nothing is deleted |
| Upload failure (`discard_unrecorded_gridfs_files`, `upload_cleanup`) | Unchanged. Both take only files the failed upload *wrote*. A reused file names an earlier writer, and its id never enters `uploaded_file_ids` |
| `cleanup_failed_upload_residue.py`, `sweep_gridfs_unreferenced.py`, `cleanup_orphaned_projects.py` | Unchanged. Each walks every document before deleting, so a file any survivor names is kept |

When the check fails, every path keeps the files. A leaked file can be found and
collected later; a lost one cannot be recovered.

### The race with a concurrent edit

An edit can reuse a file from a version that is deleted while the edit runs.
Two things close that window:

- The claim (`reused_by`) is written before the new document names the file.
  A purge batch that starts after the claim sees a version still being
  written, and keeps the file.
- After its files are stored and before it saves `runs`, the upload checks
  that every reused file still exists. It stores any that went missing again,
  from the bytes still on disk.

## Validator

- **I21** is now "no GridFS file is named by documents of *more than one
  project*". Sharing within a chain is by design. Sharing across projects is
  outside what deletion protects.
- **I13** no longer calls a shared file residue when its writer was deleted.
  The later version that reused it is listed in `reused_by`. A row that a
  document names *without* having reused it, while its backlink names a
  deleted document, is reported as a wrong backlink. Before, that case was
  reported as "residue of a purge", which it is not. One such row exists in
  the local database: a TR14 tarfile labelled in August 2026.

## Files stored before the hash

`backfill_gridfs_sha256.py` records the hash on the files the current versions
name, since only a current version is ever a predecessor. It leaves out the
project archives and the directory tarballs, which can never match: before #712
the site packed directories with the time of packing in the gzip header.
Measured on dev 2026-11-03, re-aggregating two projects, 41 of 41 and 59 of 59
directory tarballs differed.
It reads each file back and hashes it only if the bytes read equal the row's
length, writes only rows without a hash, and records every id it writes in an
undo file (`--rollback` unsets them).

The match reads a file's sample and field from where the predecessor document
names it. The rows' own metadata could not be used: measured read-only on prod
2026-11-03, the 228,153 per-sample files that the 89 current versions name
(15.65 GiB) carry either the document's underscore spelling of the field
(`Feature_BED_file`, from the August 2026 backlink backfill), or the run key
(`sample_1`) where the sample name belongs (uploads since). With the metadata as
the key, hashing them would have matched none. Uploads now record the sample
name, and because the run key is positional, a version that removes a sample
still shares the files of the samples after it.

The match also treats the aggregator 6 field names as their 7.0 names
(`AA PNG file` as `Graph PNG file`, and likewise for the PDF, graph and cycles
files). Re-aggregating a 6.1.0 project on dev with 8.3.1 gave identical bytes
under the new name in 58 of 58 features for each pair.

## Not done here

- **Sharing across projects.** Ruled out on #624: pointers between projects are
  too easy to make stale.
- **Identical files across samples within one version.** Matching requires the
  same sample and field, so a reused file is always the same file in the same
  place.
- **`permanent_delete_size()`.** It still sums every file a project names, so
  for a project that shares files with a surviving version, the admin page
  overstates what a permanent delete frees.
