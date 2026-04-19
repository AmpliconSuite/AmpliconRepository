#!/usr/bin/env python3
"""
restore_sample_csv_metadata.py

Repairs projects where user-supplied CSV metadata (extra_metadata_from_csv) was
lost when the project was reaggregated or had samples added/replaced using older
code (before the "retain metadata" fix).

Symptoms of affected projects
------------------------------
- Sample page shows metadata content (standard Sample_metadata_JSON is always
  present), but the profile page metadata checkbox is unchecked.
- The edit-project page does not show the "Retain existing metadata" or
  "Remap sample names" checkboxes.
- The previous version(s) of the project DO have extra_metadata_from_csv on
  their samples.

What this script does
---------------------
1. Scans all current, non-deleted projects for missing extra_metadata_from_csv.
2. For each affected project, walks back through previous_versions to find the
   most recent version that still has the CSV metadata.
3. Merges that recovered metadata into the current version's runs (without
   overwriting any keys that are already present in the current version).
4. Writes the updated runs back to MongoDB.

Dry-run mode (--dry-run) reports what would change without touching the database.

Usage
-----
    # From the repo root, with Django environment variables set:
    source caper/config.sh
    cd caper
    python ../restore_sample_csv_metadata.py --dry-run   # preview
    python ../restore_sample_csv_metadata.py             # apply

    # Or via Django shell:
    cd caper && python manage.py shell < ../restore_sample_csv_metadata.py
"""

import os
import sys
import argparse
import logging

# ---------------------------------------------------------------------------
# Django / settings bootstrap (only needed when running as a standalone script)
# ---------------------------------------------------------------------------
_standalone = __name__ == '__main__'
if _standalone:
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'caper.settings')
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'caper'))
    django.setup()

from bson import ObjectId
from caper.utils import collection_handle

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_csv_metadata(project):
    """Return {sample_name: extra_metadata_from_csv_dict} for a project document."""
    result = {}
    for sample_list in project.get('runs', {}).values():
        for sample in sample_list:
            name = sample.get('Sample_name')
            meta = sample.get('extra_metadata_from_csv')
            if name and isinstance(meta, dict) and meta:
                result[name] = meta
    return result


def _has_csv_metadata(project):
    """True if any sample in project['runs'] has a non-empty extra_metadata_from_csv."""
    for sample_list in project.get('runs', {}).values():
        for sample in sample_list:
            meta = sample.get('extra_metadata_from_csv')
            if isinstance(meta, dict) and meta:
                return True
    return False


def _find_recoverable_metadata(current_project):
    """
    Walk previous_versions (most-recent first) looking for a version that has
    extra_metadata_from_csv.  Returns (old_linkid, metadata_dict) or (None, {}).
    """
    prev_versions = current_project.get('previous_versions', [])
    if not prev_versions:
        return None, {}

    # previous_versions is ordered oldest-first; reverse to check most recent first
    for ver in reversed(prev_versions):
        linkid = ver.get('linkid')
        if not linkid:
            continue
        try:
            old_proj = collection_handle.find_one(
                {'_id': ObjectId(linkid)},
                {'runs': 1, 'project_name': 1}   # only load what we need
            )
        except Exception as e:
            logger.warning(f"  Could not fetch previous version {linkid}: {e}")
            continue

        if old_proj is None:
            logger.warning(f"  Previous version {linkid} not found in database")
            continue

        recovered = _extract_csv_metadata(old_proj)
        if recovered:
            return linkid, recovered

    return None, {}


def _apply_recovered_metadata(runs, recovered_meta):
    """
    Merge recovered_meta into runs in-place.
    Only writes extra_metadata_from_csv for samples that currently lack it.
    Returns the number of samples updated.
    """
    updated = 0
    for sample_list in runs.values():
        for sample in sample_list:
            name = sample.get('Sample_name')
            if not name or name not in recovered_meta:
                continue
            existing = sample.get('extra_metadata_from_csv')
            if isinstance(existing, dict) and existing:
                # Already has metadata — do not overwrite
                continue
            sample['extra_metadata_from_csv'] = dict(recovered_meta[name])
            updated += 1
    return updated


# ---------------------------------------------------------------------------
# Main repair logic
# ---------------------------------------------------------------------------

def restore_csv_metadata(dry_run=False):
    logger.info("=" * 72)
    logger.info("restore_sample_csv_metadata — CSV metadata recovery")
    logger.info(f"Mode: {'DRY RUN (no changes written)' if dry_run else 'LIVE (writing to database)'}")
    logger.info("=" * 72)

    # --- Phase 1: find all affected (current, not deleted, no CSV metadata) ---
    total_current = collection_handle.count_documents({'current': True, 'delete': False})
    logger.info(f"Total current non-deleted projects: {total_current}")

    affected_projects = []
    skipped_no_prev = 0

    for proj in collection_handle.find(
        {'current': True, 'delete': False},
        {'_id': 1, 'project_name': 1, 'runs': 1, 'previous_versions': 1}
    ):
        if _has_csv_metadata(proj):
            continue  # already has metadata — nothing to do

        prev_versions = proj.get('previous_versions', [])
        if not prev_versions:
            skipped_no_prev += 1
            continue  # never had previous versions; no source to recover from

        affected_projects.append(proj)

    logger.info(f"Projects already have CSV metadata (skipped):  {total_current - len(affected_projects) - skipped_no_prev}")
    logger.info(f"Projects with no previous versions (skipped):  {skipped_no_prev}")
    logger.info(f"Projects to examine for recoverable metadata:  {len(affected_projects)}")
    logger.info("")

    if not affected_projects:
        logger.info("No affected projects found.  Nothing to do.")
        return True

    # --- Phase 2: for each affected project, find and apply recoverable metadata ---
    restored_count = 0
    no_source_count = 0
    error_count = 0

    for proj in affected_projects:
        proj_id   = proj['_id']
        proj_name = proj.get('project_name', '<unnamed>')

        logger.info(f"Checking: {proj_name}  (id={proj_id})")

        try:
            old_linkid, recovered_meta = _find_recoverable_metadata(proj)
        except Exception as e:
            logger.error(f"  Error searching previous versions: {e}")
            error_count += 1
            continue

        if not recovered_meta:
            logger.info(f"  No recoverable CSV metadata found in any previous version — skipping")
            no_source_count += 1
            continue

        logger.info(
            f"  Found metadata for {len(recovered_meta)} sample(s) "
            f"in previous version {old_linkid}"
        )

        # Re-fetch the full current document (we loaded runs with projection above,
        # but need the complete runs dict to write back safely)
        full_proj = collection_handle.find_one({'_id': proj_id})
        if full_proj is None:
            logger.error(f"  Could not re-fetch full project document — skipping")
            error_count += 1
            continue

        runs = full_proj.get('runs', {})
        samples_updated = _apply_recovered_metadata(runs, recovered_meta)

        if samples_updated == 0:
            logger.info(f"  No samples needed updating (all already had metadata)")
            no_source_count += 1
            continue

        logger.info(f"  Would update {samples_updated} sample(s)")

        if not dry_run:
            try:
                result = collection_handle.update_one(
                    {'_id': proj_id},
                    {'$set': {'runs': runs}}
                )
                if result.modified_count:
                    logger.info(f"  ✓ Metadata restored successfully")
                    restored_count += 1
                else:
                    logger.warning(f"  update_one reported no modifications (already up-to-date?)")
                    no_source_count += 1
            except Exception as e:
                logger.error(f"  Error writing to database: {e}")
                error_count += 1
        else:
            logger.info(f"  [DRY RUN] Would restore metadata for {samples_updated} sample(s)")
            restored_count += 1

    # --- Summary ---
    logger.info("")
    logger.info("=" * 72)
    logger.info("SUMMARY")
    logger.info("=" * 72)
    if dry_run:
        logger.info(f"Projects that WOULD be restored:  {restored_count}")
    else:
        logger.info(f"Projects restored:                {restored_count}")
    logger.info(f"Projects with no recoverable source: {no_source_count}")
    logger.info(f"Errors:                              {error_count}")
    logger.info("=" * 72)

    if error_count:
        logger.warning("Completed with errors — review output above.")
        return False

    if dry_run and restored_count:
        logger.info("Re-run without --dry-run to apply these changes.")

    logger.info("Done.")
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if _standalone:
    parser = argparse.ArgumentParser(
        description=(
            "Recover lost extra_metadata_from_csv on current project versions "
            "by restoring it from the most recent previous version that still has it."
        )
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help="Report what would change without writing to the database."
    )
    args = parser.parse_args()
    success = restore_csv_metadata(dry_run=args.dry_run)
    sys.exit(0 if success else 1)
else:
    # Running inside Django shell:  python manage.py shell < restore_sample_csv_metadata.py
    print("Running CSV metadata recovery...")
    restore_csv_metadata(dry_run=False)
