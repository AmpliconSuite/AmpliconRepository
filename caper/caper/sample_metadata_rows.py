"""Where a sample's uploaded metadata lives in a project's ``runs``.

``runs`` has one list of feature rows per sample and no per-sample slot, so the
upload used to copy ``extra_metadata_from_csv`` onto every row. The copies carry
nothing: across all 209 prod documents on 2026-11-05, in 28,623 sample entries
with metadata, a sample's rows never differed. They do cost space in a document
limited to 16 MiB: Hartwig (9,090 rows, 4,170 samples) was 15.84 MB with four
columns, of which the copies were about 0.6 MB, and six more columns would have
taken it over the limit.

So a new upload stores it on the sample's first row only. The field stays in
``runs`` rather than moving to a top-level dict keyed by sample name, because
sample names can contain dots and this way every version keeps one shape.

Versions written before this keep their copies: they are not rewritten, and
every reader here accepts both forms. Readers that want one sample's metadata
ask ``sample_metadata_of``. Readers that work a row at a time (search and the
feature index, which each build one row per feature) start from
``with_metadata_on_every_row``.
"""

METADATA_KEY = 'extra_metadata_from_csv'


def sample_metadata_of(rows):
    """One sample's metadata from its feature rows, or None if it has none."""
    for row in rows or []:
        if isinstance(row, dict):
            metadata = row.get(METADATA_KEY)
            if isinstance(metadata, dict) and metadata:
                return metadata
    return None


def with_metadata_on_every_row(runs):
    """``runs`` with each row carrying its sample's metadata, for reading only.

    The stored rows are left alone: a row that needs the field is copied, and the
    copies share one metadata dict. A caller must not write the result back, or
    the stored document grows to what this module exists to avoid.

    Rows are matched by ``Sample_name`` within their list, not by position in it,
    so a list holding more than one sample name cannot lend one sample's metadata
    to another.
    """
    if not isinstance(runs, dict):
        return runs
    filled = {}
    for run_key, rows in runs.items():
        filled[run_key] = _fill_rows(rows)
    return filled


def _fill_rows(rows):
    if not isinstance(rows, list) or len(rows) < 2:
        return rows
    by_name = {}
    missing = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        metadata = row.get(METADATA_KEY)
        if isinstance(metadata, dict) and metadata:
            by_name.setdefault(row.get('Sample_name'), metadata)
        else:
            missing = True
    if not missing or not by_name:
        return rows
    filled = []
    for row in rows:
        metadata = by_name.get(row.get('Sample_name')) if isinstance(row, dict) else None
        if metadata is not None and not row.get(METADATA_KEY):
            row = dict(row)
            row[METADATA_KEY] = metadata
        filled.append(row)
    return filled
