"""Every project URL that resolves today, and what it resolves to.

The disaster-recovery plan for this site is not to move 362 GiB off AWS. It is
to keep the current version of every public project somewhere else, and to know
which URLs have to keep working. Rebuilding from re-uploaded projects mints new
ids, so without this file every link ever published -- in a paper, in an email,
in someone's notes -- resolves to nothing and there is no way to find out what
it used to mean.

The identifiers are taken from ``resolver_queries()``, the same definitions
``get_one_project()`` issues, rather than from a list written here. A URL list
maintained separately from the resolver is the defect this repository keeps
finding, and it would fail in the one direction that matters: silently, and
only once someone needed it.
"""

import csv
import io

from .project_status import (LIVE, TOMBSTONE, classify, iter_previous_versions,
                             resolver_queries)
from .visibility import normalize_visibility_field


COLUMNS = (
    'url', 'identifier', 'identifier_kind', 'project_id', 'project_name',
    'status', 'visibility', 'version_chain_id', 'version_ordinal', 'is_latest',
    'current_version_id', 'redirect_to_project', 'date',
)

# The identifiers get_one_project() will match on, and the document field each
# comes from. Derived from resolver_queries() at import so that a resolver that
# grows a lookup fails this module's test rather than quietly dropping a URL.
_IDENTIFIER_FIELDS = ('_id', 'alias_name', 'project_name')


def resolver_identifier_fields():
    """The document fields the resolver matches on, read from its own queries."""
    fields = set()
    for _line, query in resolver_queries(project_id='0' * 24, project_name='x'):
        fields.update(key for key in query if not key.startswith('$'))
    # Status keys are the predicate, not identifiers.
    return {field for field in fields
            if field in ('_id', 'alias_name', 'project_name')}


def rows(collection):
    """One row per (identifier, project) pair that resolves.

    ``linkid`` is included even though the resolver does not query it: it is
    what the site puts in the URL bar, so it is what a published link contains.
    """
    heads = {}
    for doc in collection.find({}, {'version_chain_id': 1, 'is_latest': 1}):
        if doc.get('is_latest') and doc.get('version_chain_id') is not None:
            heads[str(doc['version_chain_id'])] = str(doc['_id'])

    fields = resolver_identifier_fields()
    out = []
    for doc in collection.find({}):
        status = classify(doc)
        chain = doc.get('version_chain_id')
        base = {
            'project_id': str(doc['_id']),
            'project_name': doc.get('project_name') or '',
            'status': status,
            # Normalised, not read raw: 'private' is a visibility enum with a
            # legacy boolean spelling, and a manifest recording True where it
            # means 'private' is a manifest nobody can filter.
            'visibility': normalize_visibility_field(doc.get('private')) or '',
            'version_chain_id': str(chain) if chain is not None else '',
            'version_ordinal': doc.get('version_ordinal') if doc.get('version_ordinal') is not None else '',
            'is_latest': bool(doc.get('is_latest')),
            'current_version_id': heads.get(str(chain), ''),
            'redirect_to_project': str(doc.get('redirect_to_project') or ''),
            'date': str(doc.get('date') or ''),
        }

        seen = set()
        candidates = [('linkid', doc.get('linkid'))]
        candidates += [(field, doc.get('_id') if field == '_id' else doc.get(field))
                       for field in sorted(fields)]
        for kind, value in candidates:
            if not value:
                continue
            value = str(value)
            if value in seen:
                continue
            seen.add(value)
            row = dict(base)
            row['identifier'] = value
            row['identifier_kind'] = kind
            row['url'] = '/project/%s' % value
            out.append(row)

        # A tombstone's whole purpose is that its URL still resolves, by
        # redirect. Recorded with what it points at, because after a rebuild
        # that target is a new id and the redirect has to be re-pointed.
        if status == TOMBSTONE:
            for entry, _encoding in iter_previous_versions(doc):
                linkid = entry.get('linkid') if isinstance(entry, dict) else None
                if linkid and str(linkid) not in seen:
                    seen.add(str(linkid))
                    row = dict(base)
                    row['identifier'] = str(linkid)
                    row['identifier_kind'] = 'tombstone_history'
                    row['url'] = '/project/%s' % linkid
                    out.append(row)

    out.sort(key=lambda row: (row['project_name'], row['project_id'],
                              row['identifier_kind']))
    return out


def as_csv(collection):
    """The manifest as CSV text."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(COLUMNS),
                            extrasaction='ignore')
    writer.writeheader()
    for row in rows(collection):
        writer.writerow(row)
    return buffer.getvalue()


def totals(manifest_rows):
    """Counts for the admin page: URLs, projects, and how many are live."""
    projects = {row['project_id'] for row in manifest_rows}
    live = {row['project_id'] for row in manifest_rows if row['status'] == LIVE}
    return {'urls': len(manifest_rows), 'projects': len(projects),
            'live_projects': len(live)}
