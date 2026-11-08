# Data model notes

Conventions to keep in mind when editing search, classification or version
deletion. The data lifecycle and the stores are described in
[backend-architecture.md](backend-architecture.md); the field list is in
[AGENTS.md](../AGENTS.md).

## AC 2.0 / no-amp conventions

Search treats no-amplification samples explicitly. The search UI has four real amplicon class filters (`ecDNA`, `linear amplification`, `BFB`, `complex non-cyclic`) plus the `no-amp` pseudo-filter. Backend no-amp matching uses `search._zero_feature_mask()` and includes:
- empty `runs[sample]` lists, converted to placeholder rows with `Classification='NA'`
- blank `Feature_ID`
- `Classification == 'No FSCNA'`
- `Classification == 'NA'`, the current AmpliconSuiteAggregator convention for samples with no amplification result

When editing search/classification logic, keep `views.search_results()` and `search.perform_search()` semantics aligned: no boxes checked and all five boxes checked both mean no filter; all four amp types checked without `no-amp` excludes no-amp rows.

## Deleted version tombstones

Deleting an old project version purges its heavy GridFS payload but keeps a lightweight MongoDB tombstone with:
- `version_deleted_from_history: True`
- `payload_purged: True`
- `redirect_to_project: <current_project_id>`

`utils.get_one_project()` resolves these tombstones to the surviving project. Cleanup code must preserve them. Use `caper/caper/project_version_cleanup.py` helpers when changing version deletion behavior, especially `iter_gridfs_file_ids()`, `delete_gridfs_payload_for_project()`, and `build_deleted_version_tombstone()`.

Versions of one project share files, so every payload deleter must be told what to protect: pass what `version_file_sharing.still_needed_here()` returns. A version tagged as used in a publication (`publication_versions`) takes a typed project name to delete, and any path with nobody to ask must refuse it.

Files (tarballs from AmpliconSuiteAggregator) are stored in **GridFS** and referenced by ObjectId within the project document. Use `tar_utils.extract_from_project_tarfile()` to stream-extract specific paths without writing the full tar to disk.
