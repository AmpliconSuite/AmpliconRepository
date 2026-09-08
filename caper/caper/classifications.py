"""The amplicon classification vocabulary, declared once.

Three spellings of the same classes exist in this codebase's data: project
documents store them upper-cased, the per-sample rows carry the mixed-case
spelling the classifier emits, and the search page matches a couple of them by
regex because the collection holds both ``LINEAR`` and ``LINEAR AMPLIFICATION``.
A client should not have to know any of that, so the map that folds them lives
here and is imported, rather than being restated by each surface that needs it.

This exists because the same list already had copies drifting apart elsewhere in
this repository; a vocabulary is exactly the kind of list that goes wrong when
it is written down twice.
"""

# Project documents store classifications upper-cased -- get_project_classifications()
# in views.py does `.upper()` -- while the per-sample rows that /samples/ returns
# carry the mixed-case spelling ('ecDNA', 'Complex-non-cyclic').  A client that
# filters projects on 'ecDNA' and then reads the samples it selected should not
# have to know that the two levels disagree about capitalisation, so the API
# answers in the sample-level spelling at both.  Unknown values pass through
# untouched: a classification this map has not heard of must not be mangled.
_CANONICAL_CLASSIFICATION = {
    'ECDNA': 'ecDNA',
    'BFB': 'BFB',
    'LINEAR': 'Linear',
    # search.py already treats these as the same class ("if searching for
    # LINEAR AMPLIFICATION, also match just Linear"), and the dev collection
    # holds both spellings -- 4 documents say LINEAR, 3 say LINEAR
    # AMPLIFICATION.  Folding them here means a client filtering on 'Linear'
    # finds both, which is what the UI's search already does.
    'LINEAR AMPLIFICATION': 'Linear',
    'COMPLEX-NON-CYCLIC': 'Complex-non-cyclic',
    'COMPLEX NON-CYCLIC': 'Complex-non-cyclic',
    'FAN': 'FAN',
    'VIRUS': 'Virus',
    'UNKNOWN': 'Unknown',
}


def _canonical_classifications(project):
    """The project's amplicon classes, in the spelling /samples/ uses.

    Reads `Classification` -- singular, which is the key the upload path writes
    (views.py: `project['Classification'] = get_project_classifications(runs)`).
    This serializer read `Classifications`, plural, from the day it was written;
    nothing has ever written that key, so the field was `[]` on every project on
    the site.  Measured 2026-09-04: 0 of 33 public projects reported a
    classification through the API, while 10 of 10 sampled had ecDNA features in
    their sample rows -- an ecDNA repository answering "no ecDNA here" to the
    one question it exists to answer.

    The plural spelling is still read as a fallback: it costs nothing, and a
    document written by some past version may yet turn up holding it.
    """
    raw = project.get('Classification') or project.get('Classifications') or []
    if isinstance(raw, str):
        raw = [raw]
    seen, out = set(), []
    for value in raw:
        canonical = _CANONICAL_CLASSIFICATION.get(str(value).upper(), value)
        if canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out


# The values a caller may filter on.  Both the canonical spellings and every
# alias that folds into one are accepted, so a client that learned 'ecDNA' from
# a sample row and a client that learned 'ECDNA' from a project document both
# work.  An unrecognised value is a 400 rather than an empty result: silently
# returning nothing for a typo is the failure mode this endpoint exists to
# avoid.
# What AmpliconClassifier writes on a sample where it found no focal
# amplification.  These are values a caller legitimately wants to search for --
# "which samples came back clean" is a real question -- so they are part of the
# vocabulary rather than an absence.  They are reported and accepted under one
# name, 'None', which is what the site's own classification charts call them.
NO_AMPLICON_SPELLINGS = frozenset({'NA', 'NO FSCNA'})
NO_AMPLICON_CANONICAL = 'None'

_NO_AMPLICON_INPUTS = NO_AMPLICON_SPELLINGS | {'NONE', 'NO AMPLICON'}

# The values a caller may filter on.  Both the canonical spellings and every
# alias that folds into one are accepted, so a client that learned 'ecDNA' from
# a sample row and a client that learned 'ECDNA' from a project document both
# work.  An unrecognised value is a 400 rather than an empty result: silently
# returning nothing for a typo is the failure mode this endpoint exists to
# avoid.
#
# This set and CANONICAL_CLASSIFICATIONS below are load-bearing together: the
# facets endpoint exists so a client can discover the vocabulary instead of
# guessing, which is worth nothing if a value it advertises is one the filter
# rejects.  tests/test_api_features.py asserts they cannot diverge.
ACCEPTED_CLASSIFICATION_INPUTS = frozenset(
    list(_CANONICAL_CLASSIFICATION)
    + [value.upper() for value in _CANONICAL_CLASSIFICATION.values()]
    + list(_NO_AMPLICON_INPUTS)
)

CANONICAL_CLASSIFICATIONS = tuple(
    dict.fromkeys(list(_CANONICAL_CLASSIFICATION.values())
                  + [NO_AMPLICON_CANONICAL]))


def canonical_classification(value):
    """One classification in the spelling the API answers in."""
    text = str(value).upper()
    if text in _NO_AMPLICON_INPUTS:
        return NO_AMPLICON_CANONICAL
    return _CANONICAL_CLASSIFICATION.get(text, value)
