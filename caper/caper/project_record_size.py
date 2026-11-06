"""Whether a project's record will fit in one database document.

A project's samples, features and uploaded metadata all live in one document,
and DocumentDB, like MongoDB, refuses a document over 16 MiB. Hartwig (4,170
samples, 9,090 features) was 15.84 MB on 2026-11-05, so a metadata sheet can reach
the limit on a large project. There is no fixed file size to check instead: the
room left depends on the project, and the same sheet costs more on disk than in
the file because each sample repeats its column names.

The check has to count what is written later. ``sample_data`` and
``ecDNA_context`` are first stored when someone opens the project, and a record
that fits at upload but not then would fail on every page view. ``sample_data``
can be worked out here. ``RESERVED_BYTES`` covers the rest of a new version's
document, ``ecDNA_context`` (64 KB on Hartwig), ``Oncogenes`` (14 KB) and the
counters that grow with use.
"""

import copy
import io
import json
import logging
import tarfile

import bson

from .extra_metadata import get_extra_metadata_from_project, process_metadata_no_request
from .utils import replace_underscore_keys, sample_data_from_feature_list

MAX_RECORD_BYTES = 16 * 1024 * 1024
RESERVED_BYTES = 128 * 1024
LIMIT_BYTES = MAX_RECORD_BYTES - RESERVED_BYTES

# Stored rows hold a 12-byte id where run.json holds a path to the file, and repeat
# three of those ids under their older names. Measured against a stored document on
# 2026-11-05: leaving this out overstated the record by 2%, enough to refuse a large
# project that fits.
_FILE_ID = bson.ObjectId('0' * 24)
_FILE_KEY_SUFFIXES = ('_file', '_directory', '_JSON')
_NOT_A_FILE = ('', 'NA', 'Not Provided')
_STORED_AS_PATHS = ('cnvkit_directory',)
_OLDER_NAMES = {'Graph_PNG_file': 'AA_PNG_file', 'Graph_PDF_file': 'AA_PDF_file',
                'Reconstruction_directory': 'AA_directory'}


class ProjectRecordTooLarge(ValueError):
    """The project would not fit. The message is written for the person uploading."""


def too_large_message(size_bytes, has_metadata):
    size, limit = size_bytes / 1e6, LIMIT_BYTES / 1e6
    advice = ('Remove metadata columns or shorten their values, then try again. '
              if has_metadata else
              'This project has too many features to store as one project. ')
    return (f"This project's record would be {size:.1f} MB, and the limit is {limit:.1f} MB. "
            f"{advice}Nothing was changed.")


def _size_with_derived_fields(document):
    """Encoded size of ``document``, plus ``sample_data`` if it isn't there yet."""
    size = len(bson.encode(document))
    if not document.get('sample_data'):
        rows = [row for rows in document['runs'].values() for row in rows]
        size += len(bson.encode({'sample_data': sample_data_from_feature_list(rows)}))
    return size


def size_after_metadata_edit(project, metadata_file):
    """What the project's record becomes if this sheet is applied to it in place.

    Works on a copy and rewinds the file, so the edit itself still sees both as
    they were. Applies the sheet the way the edit will, so it can raise the same
    error for an unreadable sheet.
    """
    runs = copy.deepcopy(dict(project.get('runs') or {}))
    try:
        runs = process_metadata_no_request(
            runs, metadata_file=metadata_file,
            old_extra_metadata=get_extra_metadata_from_project(project))
    finally:
        metadata_file.seek(0)
    return _size_with_derived_fields({**project, 'runs': runs})


def _read_run_json(archive_path):
    """The aggregated archive's run.json. It sorts before samples/, so this stops early."""
    with tarfile.open(archive_path, 'r:gz') as archive:
        for member in archive:
            if member.name.endswith('results/run.json'):
                with archive.extractfile(member) as handle:
                    return json.load(io.TextIOWrapper(handle, encoding='utf-8'))
    return None


def _row_as_stored(row):
    stored = dict(row)
    for key, value in row.items():
        if (key.endswith(_FILE_KEY_SUFFIXES) and key not in _STORED_AS_PATHS
                and isinstance(value, str) and value not in _NOT_A_FILE):
            stored[key] = _FILE_ID
            if key in _OLDER_NAMES:
                stored[_OLDER_NAMES[key]] = _FILE_ID
    return stored


def _as_stored(runs):
    """run.json rows as a document will hold them."""
    return {sample: [_row_as_stored(row) for row in rows] for sample, rows in runs.items()}


def size_of_new_version(archive_path, metadata_filepath=None, old_extra_metadata=None,
                        remap_names_to_alias=False):
    """Estimated record size of a version made from this aggregated archive.

    Returns None if the archive has no readable run.json. Ingestion reports that
    itself, in its own words.
    """
    run_json = _read_run_json(archive_path)
    if not run_json or not isinstance(run_json.get('runs'), dict):
        return None
    runs = _as_stored(replace_underscore_keys(run_json['runs']))
    # The same call ingestion makes, so the same columns are counted.
    runs = process_metadata_no_request(
        runs, file_path=metadata_filepath, old_extra_metadata=old_extra_metadata,
        remap_name_to_alias=remap_names_to_alias)
    return _size_with_derived_fields({'runs': runs})


def refuse_new_version_over_limit(archive_path, metadata_filepath=None, old_extra_metadata=None,
                                  remap_names_to_alias=False):
    """Raise ProjectRecordTooLarge if this archive's version would not fit.

    Runs before anything is stored. Only a real answer stops an upload: a failure
    to work out the size is logged and the upload carries on as it did before.
    """
    try:
        size = size_of_new_version(archive_path, metadata_filepath, old_extra_metadata,
                                   remap_names_to_alias)
    except Exception:
        logging.exception('Could not estimate the record size of %s', archive_path)
        return
    if size is not None and size > LIMIT_BYTES:
        raise ProjectRecordTooLarge(
            too_large_message(size, bool(metadata_filepath or old_extra_metadata)))
