"""Current sample-page names that a visitor may discover from search."""
import re
from hashlib import sha256

from bson import BSON

from django.core.cache import cache
from django.urls import reverse

from .project_status import LIVE, status_query
from .visibility import normalize_visibility_field

SEPARATORS = re.compile(r'[\s_./-]+')
MIN_QUERY = 3
MAX_QUERY = 128
RESULT_LIMIT = 10


def normalize_name(value):
    return SEPARATORS.sub('', value.casefold())


def match_rank(query, name, key=None):
    """Rank a normalized query against an as-written name. Lower is better."""
    if key is None:
        key = normalize_name(name)
    if key == query:
        return 0
    if key.startswith(query):
        return 1
    if len(query) >= 4 and query in key:
        parts = SEPARATORS.split(name.casefold())
        offset = 0
        for part in parts:
            if key.startswith(query, offset):
                return 2
            offset += len(part)
        return 3

    # Reverse containment rescues H1792 when someone types NCI-H1792.
    # Keep a sample number whole: ICGCMB1 should not match ICGCMB101.
    if (len(key) >= 4 and key in query
            and any(c.isalpha() for c in key) and any(c.isdigit() for c in key)):
        start = query.find(key)
        while start >= 0:
            end = start + len(key)
            splits_left = start > 0 and query[start - 1].isdigit() and key[0].isdigit()
            splits_right = end < len(query) and query[end].isdigit() and key[-1].isdigit()
            if not splits_left and not splits_right:
                return 4
            start = query.find(key, start + 1)
    return None


def sample_name_groups(sample_index, schema_version, query):
    """One set of names per project that has a non-empty indexed run."""
    return sample_index.aggregate([
        {'$match': {**query, 'schema_version': schema_version, 'feature_count': {'$gt': 0}}},
        {'$group': {'_id': '$project_id', 'names': {'$addToSet': '$sample_name'}}},
    ])


def _settled_versions(manifest, project_ids):
    """``{project_id: version}`` for the projects no index write is part way through."""
    return {entry['project_id']: (entry['digest'], entry['indexed_at'])
            for entry in manifest.find(
                {'project_id': {'$in': project_ids}, 'digest': {'$ne': None}, 'indexed_at': {'$ne': None}},
                {'_id': 0, 'project_id': 1, 'digest': 1, 'indexed_at': 1})}


def sample_names(sample_index, manifest, schema_version, projects):
    """``{project_id: [(name, key)]}``, shared across keystrokes and workers.

    A list is kept with the manifest version seen before it was read, and used
    only while the project still has that version. index_project clears the
    digest before it deletes a row and writes a new indexed_at after the last
    one, so a list read during a reindex is never used again, and an edit shows
    as soon as its reindex ends.
    """
    project_ids = sorted(projects, key=str)
    digest = sha256(BSON.encode({'ids': project_ids})).hexdigest()
    cache_key = f'sample-names-v2:{sample_index.full_name}:{schema_version}:{digest}'
    versions = _settled_versions(manifest, project_ids)
    current = {project_id: entry for project_id, entry in (cache.get(cache_key) or {}).items()
               if project_id in versions and entry[0] == versions[project_id]}
    names = {project_id: entry[1] for project_id, entry in current.items()}
    missing = [project_id for project_id in project_ids if project_id not in names]
    if missing:
        found = {project_id: [] for project_id in missing}
        for row in sample_name_groups(sample_index, schema_version, {'project_id': {'$in': missing}}):
            found[row['_id']] = [(name, normalize_name(name)) for name in row['names']
                                 if isinstance(name, str) and name and '/' not in name
                                 and name not in ('.', '..')]
        names.update(found)
        settled = {project_id: (versions[project_id], found[project_id])
                   for project_id in missing if project_id in versions}
        if settled:
            cache.set(cache_key, {**current, **settled})
    return names


def suggest_samples(text, projects_collection, sample_index, manifest, access, schema_version):
    """All collections use primary reads. Access is the projects visibility predicate."""
    if len(text) > MAX_QUERY:
        return []
    query = normalize_name(text)
    if len(query) < MIN_QUERY:
        return []

    # Membership and visibility can change even if the subsequent reindex fails.
    projects = {p['_id']: p for p in projects_collection.find(
        status_query(LIVE, access),
        {'project_name': 1, 'private': 1})}
    if not projects:
        return []

    matches = []
    for project_id, names in sample_names(sample_index, manifest, schema_version, projects).items():
        project = projects[project_id]
        for name, key in names:
            rank = match_rank(query, name, key)
            if rank is not None:
                matches.append((rank, len(name), name.casefold(), name, project['project_name'],
                                project_id))
    return [
        {'sample_name': name, 'project_name': project_name,
         'visibility': normalize_visibility_field(projects[project_id].get('private')),
         'url': reverse('sample_page', args=[str(project_id), name])}
        for _, _, _, name, project_name, project_id in sorted(matches)[:RESULT_LIMIT]
    ]
