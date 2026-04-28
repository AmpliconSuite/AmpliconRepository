#!/usr/bin/env python3
"""
Recover a project version deleted from the edit-project history table.

Usage:
    python recover_deleted_version.py <deleted_version_id> <current_version_id>

Arguments:
    deleted_version_id  - MongoDB _id of the version that was deleted from history
    current_version_id  - MongoDB _id of the current (surviving) version that should
                          have this version restored into its previous_versions list

The script:
  1. Looks up both documents and shows you what it found (dry-run by default).
  2. Re-adds the deleted version to the current version's previous_versions array
     (in date order, oldest first).
  3. Clears the version_deleted_from_history flag on the recovered document.

Run with --apply to actually write the changes.
"""

import sys
import os
import argparse
import django

# ---------------------------------------------------------------------------
# Bootstrap Django so we can reuse the app's DB settings and collection_handle
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'caper'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'caper.settings')
django.setup()

from bson import ObjectId
from caper.utils import collection_handle  # noqa: E402 – must come after setup


def fetch(doc_id, label):
    doc = collection_handle.find_one({'_id': ObjectId(doc_id)})
    if doc is None:
        print(f"ERROR: {label} document {doc_id!r} not found in the database.")
        sys.exit(1)
    return doc


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('deleted_version_id',
                        help='_id of the version deleted from history')
    parser.add_argument('current_version_id',
                        help='_id of the current (surviving) project version')
    parser.add_argument('--apply', action='store_true',
                        help='Actually write changes (default is dry-run)')
    args = parser.parse_args()

    deleted_id = args.deleted_version_id.strip()
    current_id = args.current_version_id.strip()

    print(f"\n{'='*60}")
    print(f"Deleted version : {deleted_id}")
    print(f"Current version : {current_id}")
    print(f"Mode            : {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"{'='*60}\n")

    # ------------------------------------------------------------------
    # 1. Fetch both documents
    # ------------------------------------------------------------------
    deleted_doc = fetch(deleted_id, 'deleted_version')
    current_doc = fetch(current_id, 'current_version')

    print("Deleted version document fields:")
    for key in ('project_name', 'date', 'current', 'delete', 'version_deleted_from_history',
                'ASP_version', 'AA_version', 'AC_version', 'aggregator_version'):
        print(f"  {key}: {deleted_doc.get(key, '<missing>')}")

    print("\nCurrent version document fields:")
    for key in ('project_name', 'date', 'current', 'delete'):
        print(f"  {key}: {current_doc.get(key, '<missing>')}")

    existing_prev = current_doc.get('previous_versions', [])
    print(f"\n  previous_versions entries: {len(existing_prev)}")
    for pv in existing_prev:
        print(f"    linkid={pv.get('linkid')}  date={pv.get('date')}")

    # ------------------------------------------------------------------
    # 2. Check the deleted document is not already in previous_versions
    # ------------------------------------------------------------------
    already_present = any(str(pv.get('linkid')) == deleted_id for pv in existing_prev)
    if already_present:
        print(f"\nNOTHING TO DO: {deleted_id} is already in the current version's "
              "previous_versions list.")
        sys.exit(0)

    # ------------------------------------------------------------------
    # 3. Build the new previous_versions entry for the deleted document
    # ------------------------------------------------------------------
    new_entry = {
        'date': str(deleted_doc.get('date', '1999-01-01T00:00:00.000000')),
        'linkid': deleted_id,
        'ASP_version': deleted_doc.get('ASP_version', 'NA'),
        'AA_version': deleted_doc.get('AA_version', 'NA'),
        'AC_version': deleted_doc.get('AC_version', 'NA'),
        'aggregator_version': deleted_doc.get('aggregator_version', 'NA'),
    }

    # Insert in chronological order (oldest first, matching normal app behaviour)
    updated_prev = existing_prev + [new_entry]
    updated_prev.sort(key=lambda pv: pv.get('date', ''))

    print(f"\nNew previous_versions list will be ({len(updated_prev)} entries):")
    for pv in updated_prev:
        tag = ' <-- RECOVERED' if pv['linkid'] == deleted_id else ''
        print(f"  linkid={pv['linkid']}  date={pv['date']}{tag}")

    print(f"\nWill also clear 'version_deleted_from_history' flag on {deleted_id}.")

    # ------------------------------------------------------------------
    # 4. Apply or report
    # ------------------------------------------------------------------
    if not args.apply:
        print("\nDRY-RUN complete. Re-run with --apply to write these changes.")
        return

    # Update the current version document
    collection_handle.update_one(
        {'_id': ObjectId(current_id)},
        {'$set': {'previous_versions': updated_prev}}
    )

    # Clear the deletion flag on the recovered document
    collection_handle.update_one(
        {'_id': ObjectId(deleted_id)},
        {'$unset': {'version_deleted_from_history': ''}}
    )

    print("\nDone. Changes written successfully.")
    print(f"  - {deleted_id} re-added to previous_versions of {current_id}")
    print(f"  - version_deleted_from_history flag removed from {deleted_id}")
    print(f"\nVerify by visiting: /project/{current_id}")


if __name__ == '__main__':
    main()
