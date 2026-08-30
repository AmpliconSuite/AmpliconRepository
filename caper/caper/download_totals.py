"""Download counts for a project, as opposed to for one version of it.

A download is a fact about the project. Which version happened to be current
when it was served is an implementation detail of the site, and no reader has
ever wanted it broken down that way -- the weekly report and the admin stats
page both present these numbers as "how much has this project been downloaded".

The site stores them per version, and until now displayed only the current
version's share. Measured on prod 2026-08-29, across the 45 chains that have
more than one version:

    field               head shows    chain earned    never displayed
    project_downloads          627           7,459        6,832  (92%)
    sample_downloads        54,888         529,432      474,544  (90%)

Nothing was lost to produce those gaps. The counts are on the older version
documents right now; ``update_project_download_count()`` writes to whichever
document served the file, and reaggregation starts the new version's dict
empty rather than carrying the old one forward. So 90% of the project's
download history exists and is simply not added up. This module adds it up.

**Why summing is correct here and would be wrong for ``views`` and
``downloads``.** Those two are integers, and reaggregation *does* carry them
forward (``views.py``, where a new version is created from an old one). A
version's ``downloads`` therefore already contains its predecessors' -- adding
them across a chain would count the same download once per version it has
outlived. The per-date dicts are never carried forward, so each one holds
exactly what its own version earned, and the sum is the total. Do not
"harmonise" the two by summing all four.

The counts are keyed by ``YYYY-MM-DD`` and two versions can hold the same date:
a version superseded at noon and its successor both serve downloads that day.
Merging adds those, which is right -- they are different downloads.

**The measurement that settles it.** Reading the upload code says the dicts are
not carried forward, but the data was written by every past version of that
code, so it was checked against prod on 2026-08-29. Containment is not the
test: PCAWG is downloaded most days, so a successor's dates contain its
predecessor's by coincidence, and a crude subset screen flagged it along with
four projects whose whole "history" was a single shared day. The decisive test
is that a version cannot have served a download before it existed. Across the
250 documents holding a dated counter and a usable creation date, **none held a
key earlier than its own birth**. Every dated count was earned by the document
holding it.

``downloads`` fails that same test by construction -- 59 consecutive version
pairs on prod have a non-decreasing value, and a new version is seeded with its
predecessor's total -- which is the other half of why the two pairs are treated
differently.
"""

from . import lineage

#: The counters this module owns. Keyed by date, written by the download views.
DATED_COUNTERS = ('project_downloads', 'sample_downloads')

#: What a chain-total query needs to load. ``version_chain_id`` because that is
#: how the members are found; the counters because they are what is summed.
PROJECTION = {'_id': 1, 'version_chain_id': 1, 'version_ordinal': 1,
              'project_downloads': 1, 'sample_downloads': 1}


def as_dated(value):
    """One counter as a ``{date: count}`` dict, whatever encoding it is in.

    Three encodings are in production, all of them written by code that has
    shipped: a dict (163 project documents), a bare int from before the
    per-date breakdown existed (12), and absent (the rest). The bare int has no
    date to attribute itself to -- the download views migrate it to *today*
    when they next touch the document, which is wrong but already done and not
    this function's to repeat. Here it is kept out of the date breakdown and
    reported under ``None`` so that a caller summing values still sees it.
    """
    if isinstance(value, dict):
        return {key: count for key, count in value.items()
                if isinstance(count, (int, float))}
    if isinstance(value, bool):
        # bool is an int subclass; a True here is corruption, not a count of 1.
        return {}
    if isinstance(value, (int, float)):
        return {None: value} if value else {}
    return {}


def merge_dated(counters):
    """Add several ``{date: count}`` dicts together."""
    merged = {}
    for counter in counters:
        for key, count in as_dated(counter).items():
            merged[key] = merged.get(key, 0) + count
    return merged


def total(value):
    """One counter as a single number."""
    return sum(as_dated(value).values())


def chain_totals(collection, project):
    """The dated counters summed over every version of *project*'s chain.

    Includes tombstones deliberately: a deleted version's downloads still
    happened, and dropping them would make a project's history shrink whenever
    an old version was tidied away. Returns *project*'s own counts unchanged
    when it has no chain -- an unpointered document is a chain of one.
    """
    members = lineage.chain_members(collection, project, PROJECTION)
    if not members:
        members = [project]
    return {name: merge_dated(member.get(name) for member in members)
            for name in DATED_COUNTERS}


def chain_totals_for(collection, projects):
    """``chain_totals`` for many projects, in one query per chain.

    The admin stats page renders every public project, so the per-project form
    would be one chain query per row. Documents with no chain id keep their own
    counts, as in ``chain_totals``.
    """
    by_chain = {}
    for project in projects:
        chain_id = project.get('version_chain_id')
        if chain_id is not None:
            by_chain.setdefault(str(chain_id), chain_id)

    members_by_chain = {}
    if by_chain:
        for member in collection.find(
                {'version_chain_id': {'$in': list(by_chain.values())}},
                PROJECTION):
            key = str(member.get('version_chain_id'))
            members_by_chain.setdefault(key, []).append(member)

    totals = {}
    for project in projects:
        chain_id = project.get('version_chain_id')
        members = members_by_chain.get(str(chain_id)) if chain_id is not None else None
        if not members:
            members = [project]
        totals[str(project['_id'])] = {
            name: merge_dated(member.get(name) for member in members)
            for name in DATED_COUNTERS
        }
    return totals
