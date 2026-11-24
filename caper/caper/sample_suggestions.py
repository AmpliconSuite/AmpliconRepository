"""Current sample-page names that a visitor may discover from search."""
import re

from django.urls import reverse

from .project_status import LIVE, status_query
from .visibility import normalize_visibility_field

SEPARATORS = re.compile(r'[\s_./-]+')
MIN_QUERY = 3
MAX_QUERY = 128
RESULT_LIMIT = 10


def normalize_name(value):
    return SEPARATORS.sub('', value.casefold())


def match_rank(query, name):
    """Rank a normalized query against an as-written name. Lower is better."""
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


def suggest_samples(text, projects_collection, sample_index, access, schema_version):
    """Both collections use primary reads. Access is the projects visibility predicate."""
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

    groups = sample_name_groups(sample_index, schema_version, {'project_id': {'$in': list(projects)}})
    matches = []
    for group in groups:
        project_id = group['_id']
        project = projects[project_id]
        for name in group['names']:
            if not name or '/' in name or name in ('.', '..'):
                continue
            rank = match_rank(query, name)
            if rank is not None:
                matches.append((rank, len(name), name.casefold(), name, project['project_name'],
                                project_id))
    return [
        {'sample_name': name, 'project_name': project_name,
         'visibility': normalize_visibility_field(projects[project_id].get('private')),
         'url': reverse('sample_page', args=[str(project_id), name])}
        for _, _, _, name, project_name, project_id in sorted(matches)[:RESULT_LIMIT]
    ]
