"""Which live projects have samples without CNVkit results, by visibility.

Read-only. One aggregation over the live projects that carries, per sample, only
the two CNV fields of each feature row -- never the rest of ``runs`` -- so the
report stays cheap enough to compute on every page load (measured on prod
2026-11-18: 90 live projects, 22,404 samples, 4.4 s from inside the container).

Two things count as "CNVkit results", and the page reports them separately
because they pick out different samples:

* ``cnvkit_directory`` -- the CNVkit output directory in the upload. Absent when
  CNVkit was not run, e.g. when the user supplied their own CNV BED to
  AmpliconSuite-pipeline.
* ``CNV_BED_file`` -- the CNV calls AA was seeded from, which the sample page
  draws as the CNV track. Absent means the sample plot has no CNV track.

Measured on prod 2026-11-18 across those 22,404 samples: 15,282 have both,
2,878 have a CNV BED but no CNVkit directory, 4,243 have neither, and 1 has a
CNVkit directory but no CNV BED. Every sample's feature rows agreed with each
other on both fields, but a sample counts as having a field if *any* of its rows
does, so one row that disagrees cannot hide a file.

Both spellings of each key are read. Prod documents overwhelmingly use the
underscore forms, and older ones use the space-separated forms.
"""

from .project_status import LIVE, STATUS_QUERIES
from .visibility import normalize_visibility_field

# Display order and labels. hidden_public is what the site elsewhere calls
# "Hidden Public": viewable by anyone with the link, never listed.
VISIBILITY_ROWS = (
    ('public', 'Public'),
    ('hidden_public', 'Public, unlisted'),
    ('private', 'Private'),
)

CNVKIT_DIR = 'cnvkit_dir'
CNV_BED = 'cnv_bed'

_FIELD_KEYS = {
    CNVKIT_DIR: ('cnvkit_directory', 'cnvkit directory'),
    CNV_BED: ('CNV_BED_file', 'CNV BED file'),
}

# What the aggregator and older upload code write when a file is absent.
# 'Not Provided' is the only one measured on prod (2026-11-18: 13,481
# cnvkit_directory rows, 5,762 CNV_BED_file rows); the others are defensive.
_ABSENT_STRINGS = {'', 'not provided', 'na', 'none'}


def _present(value):
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in _ABSENT_STRINGS
    return True  # a GridFS ObjectId


def _pipeline():
    # $objectToArray because runs is keyed by sample, and a projection cannot
    # name keys it does not know. The inner $map keeps only the CNV fields, so
    # nothing else of a feature row leaves the database.
    fields = {f'f{i}': f'$$row.{key}'
              for i, key in enumerate(k for keys in _FIELD_KEYS.values() for k in keys)}
    return [
        {'$match': STATUS_QUERIES[LIVE]},
        {'$project': {
            'project_name': 1,
            'private': 1,
            'samples': {'$map': {
                'input': {'$objectToArray': {'$ifNull': ['$runs', {}]}},
                'as': 'run',
                'in': {'$map': {
                    'input': {'$ifNull': ['$$run.v', []]},
                    'as': 'row',
                    'in': fields,
                }},
            }},
        }},
    ]


def _aliases():
    """Map the aggregation's f0..fN back to the field each one reads."""
    out = {}
    i = 0
    for field, keys in _FIELD_KEYS.items():
        for _ in keys:
            out[f'f{i}'] = field
            i += 1
    return out


def sample_flags(rows, aliases=None):
    """``{field: bool}`` for one sample's projected feature rows, or None.

    A sample with no feature rows has nothing to judge, so it is left out of the
    counts rather than reported as missing everything.
    """
    if not rows:
        return None
    aliases = aliases or _aliases()
    flags = {field: False for field in _FIELD_KEYS}
    for row in rows:
        for alias, value in (row or {}).items():
            field = aliases.get(alias)
            if field and _present(value):
                flags[field] = True
    return flags


def summarize(projects):
    """Build the report from aggregation output (see ``_pipeline``).

    Pure, so the tests can feed it documents without a database.
    """
    aliases = _aliases()

    def empty_bucket():
        return {
            'projects': 0,
            'samples': 0,
            # per field: projects with >= 1 sample missing it, projects where
            # every sample is missing it, and samples missing it
            **{f'{field}_projects': 0 for field in _FIELD_KEYS},
            **{f'{field}_all_projects': 0 for field in _FIELD_KEYS},
            **{f'{field}_samples': 0 for field in _FIELD_KEYS},
            'neither_samples': 0,
        }

    buckets = {key: empty_bucket() for key, _ in VISIBILITY_ROWS}
    affected = []

    for project in projects:
        visibility = normalize_visibility_field(project.get('private', 'private'))
        bucket = buckets[visibility]
        bucket['projects'] += 1

        samples = 0
        missing = {field: 0 for field in _FIELD_KEYS}
        neither = 0
        for rows in project.get('samples') or []:
            flags = sample_flags(rows, aliases)
            if flags is None:
                continue
            samples += 1
            for field, has in flags.items():
                if not has:
                    missing[field] += 1
            if not any(flags.values()):
                neither += 1

        bucket['samples'] += samples
        bucket['neither_samples'] += neither
        for field, count in missing.items():
            bucket[f'{field}_samples'] += count
            if count:
                bucket[f'{field}_projects'] += 1
            if samples and count == samples:
                bucket[f'{field}_all_projects'] += 1

        if any(missing.values()):
            affected.append({
                'linkid': str(project['_id']),
                'project_name': project.get('project_name', ''),
                'visibility': visibility,
                'visibility_display': dict(VISIBILITY_ROWS)[visibility],
                'samples': samples,
                'missing_cnvkit_dir': missing[CNVKIT_DIR],
                'missing_cnv_bed': missing[CNV_BED],
                'missing_both': neither,
            })

    total = empty_bucket()
    for bucket in buckets.values():
        for key, value in bucket.items():
            total[key] += value

    rows = [dict(buckets[key], key=key, label=label) for key, label in VISIBILITY_ROWS]
    rows.append(dict(total, key='total', label='All live projects'))

    affected.sort(key=lambda p: (-p['missing_cnvkit_dir'], -p['missing_cnv_bed'],
                                 str(p['project_name']).lower()))
    return {'rows': rows, 'projects': affected}


def build_report(collection):
    """Run the aggregation against ``collection`` (the projects collection)."""
    return summarize(collection.aggregate(_pipeline(), allowDiskUse=True))
