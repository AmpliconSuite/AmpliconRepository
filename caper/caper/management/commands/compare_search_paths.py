"""Run a corpus of searches down both paths and report every difference.

This is the gate on switching the search over. The claim being tested is not
"the index is fast" -- that is easy to show and not the risk -- but "a result
served from the index is indistinguishable from one served by reading every
project document". Anything that differs is a defect, including a difference
that looks like an improvement.

    manage.py compare_search_paths                 # the built-in corpus
    manage.py compare_search_paths --genes-from-index 200   # ...plus real genes
    manage.py compare_search_paths --verbose       # show each differing row

Read-only. It runs both searches and compares; it writes nothing.
"""

import itertools
import time

from django.core.management.base import BaseCommand


class AnonymousSearchUser:
    """Stands in for an unauthenticated visitor.

    A real AnonymousUser would do, but this makes the comparison's scope
    explicit: what a logged-out visitor can see. Member-visible rows are
    compared separately, by passing --username.
    """
    is_authenticated = False
    username = None
    email = None


# Queries chosen for the ways the two paths could disagree rather than for
# coverage of the form: an operator each, a wildcard, a term that matches
# nothing, the classification aliases, the three zero-feature branches, and the
# unfiltered corpus, which is the one that exercises the row shape at scale.
CORPUS = [
    ('unfiltered', dict(no_filter=True)),
    ('gene exact', dict(genequery='MYC', no_filter=True)),
    ('gene absent', dict(genequery='ZZZNOSUCHGENE', no_filter=True)),
    ('gene or', dict(genequery='MYC|EGFR', no_filter=True)),
    ('gene and', dict(genequery='MYC&EGFR', no_filter=True)),
    ('gene prefix wildcard', dict(genequery='MY*', no_filter=True)),
    ('gene suffix wildcard', dict(genequery='*GFR', no_filter=True)),
    ('gene contains wildcard', dict(genequery='*ORF*', no_filter=True)),
    ('class ecDNA', dict(classquery='ECDNA', include_no_amp=False)),
    ('class BFB', dict(classquery='BFB', include_no_amp=False)),
    ('class linear alias', dict(classquery='LINEAR AMPLIFICATION', include_no_amp=False)),
    ('class complex alias', dict(classquery='COMPLEX NON-CYCLIC', include_no_amp=False)),
    ('class multi', dict(classquery='ECDNA|BFB', include_no_amp=False)),
    ('class plus no-amp', dict(classquery='ECDNA', include_no_amp=True)),
    ('no-amp only', dict(include_no_amp=True, no_filter=False)),
    ('amps only', dict(include_no_amp=False, no_filter=False)),
    ('gene and class', dict(genequery='MYC', classquery='ECDNA', include_no_amp=False)),
    ('sample name substring', dict(metadata_sample_name='GBM', no_filter=True)),
    ('sample name absent', dict(metadata_sample_name='ZZZNOSUCHSAMPLE', no_filter=True)),
    ('project name substring', dict(project_name='TEST', no_filter=True)),
    ('project name absent', dict(project_name='ZZZNOSUCHPROJECT', no_filter=True)),
    ('cancer or tissue', dict(metadata_cancer_type='BRAIN', no_filter=True)),
    ('sample type', dict(metadata_sample_type='CELL LINE', no_filter=True)),
]

COMPARED_KEYS = (
    'Sample_name', 'Feature_ID', 'Classification', 'All_genes', 'Oncogenes',
    'Sample_type', 'Cancer_type', 'Tissue_of_origin',
    'project_name', 'project_linkid', 'project_url',
)


def _row_key(row):
    """Identity of a result row: which feature of which sample of which project."""
    return (str(row.get('project_linkid')), str(row.get('Sample_name')),
            str(row.get('Feature_ID', '')))


def _comparable(row):
    """Only the fields a result is read by.

    A row from the old path also carries every other column of the project's
    feature table, because it is a DataFrame row. Comparing those would report
    differences in fields nothing displays, which would bury the ones that
    matter.
    """
    return {key: row.get(key) for key in COMPARED_KEYS}


class Command(BaseCommand):
    help = 'Compare the indexed search against the project-document search.'

    def add_arguments(self, parser):
        parser.add_argument('--genes-from-index', type=int, default=0,
                            help='Also compare this many real gene symbols taken '
                                 'from the corpus, which is where the queries '
                                 'nobody thought to write down come from.')
        parser.add_argument('--username', default=None,
                            help='Compare as this user rather than anonymously, '
                                 'so the member-visible rows are covered too.')
        parser.add_argument('--verbose', action='store_true',
                            help='Print each differing row rather than a count.')

    def handle(self, *args, **options):
        from caper.feature_index import gene_catalog_handle
        from caper.search import perform_search
        from caper.search_index import can_serve, search_from_index

        user = AnonymousSearchUser()
        if options['username']:
            from django.contrib.auth.models import User
            user = User.objects.get(username=options['username'])

        corpus = list(CORPUS)
        wanted = options['genes_from_index']
        if wanted:
            symbols = [entry['symbol'] for entry in itertools.islice(
                gene_catalog_handle.find({}, {'symbol': 1, '_id': 0}), wanted)]
            corpus += [(f'gene {symbol}', dict(genequery=symbol, no_filter=True))
                       for symbol in symbols]

        self.stdout.write(f'comparing {len(corpus)} queries as '
                          f'{options["username"] or "an anonymous visitor"}\n')
        header = (f"{'query':26}{'rows':>8}{'old s':>9}{'new s':>9}"
                  f"{'speedup':>9}  verdict")
        self.stdout.write(header)
        self.stdout.write('-' * len(header))

        differing = 0
        skipped = 0
        old_total = new_total = 0.0

        for label, params in corpus:
            if not can_serve(**params):
                skipped += 1
                self.stdout.write(f'{label:26}{"":>8}{"":>9}{"":>9}{"":>9}  '
                                  f'SKIPPED (old path only)')
                continue

            started = time.time()
            old = perform_search(user=user, **params)
            old_seconds = time.time() - started
            started = time.time()
            new = search_from_index(user=user, **params)
            new_seconds = time.time() - started
            old_total += old_seconds
            new_total += new_seconds

            problems = self._compare(old, new)
            rows = len(old['public_sample_data']) + len(old['private_sample_data'])
            speedup = old_seconds / new_seconds if new_seconds else 0
            verdict = 'same' if not problems else f'DIFFERS ({len(problems)})'
            self.stdout.write(
                f'{label:26}{rows:>8,}{old_seconds:>9.3f}{new_seconds:>9.3f}'
                f'{speedup:>8.0f}x  {verdict}')
            if problems:
                differing += 1
                for problem in problems[:None if options['verbose'] else 3]:
                    self.stdout.write(f'      {problem}')
                if not options['verbose'] and len(problems) > 3:
                    self.stdout.write(f'      ... and {len(problems) - 3} more')

        self.stdout.write('')
        self.stdout.write(f'total: old {old_total:.2f}s, new {new_total:.2f}s, '
                          f'{old_total / new_total if new_total else 0:.0f}x overall')
        if skipped:
            self.stdout.write(f'{skipped} query(s) the index does not claim to serve')
        if differing:
            self.stdout.write(self.style.ERROR(
                f'{differing} of {len(corpus) - skipped} queries DIFFER -- '
                f'the search must not be switched over'))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'all {len(corpus) - skipped} comparable queries return identical results'))

    def _compare(self, old, new):
        """Every way the two answers differ, as readable lines."""
        problems = []
        for bucket in ('public_sample_data', 'private_sample_data'):
            old_rows = {_row_key(row): _comparable(row) for row in old[bucket]}
            new_rows = {_row_key(row): _comparable(row) for row in new[bucket]}

            for key in sorted(set(old_rows) - set(new_rows))[:20]:
                problems.append(f'{bucket}: only the old path returned {key}')
            for key in sorted(set(new_rows) - set(old_rows))[:20]:
                problems.append(f'{bucket}: only the index returned {key}')
            for key in sorted(set(old_rows) & set(new_rows)):
                for field in COMPARED_KEYS:
                    if old_rows[key][field] != new_rows[key][field]:
                        problems.append(
                            f'{bucket}: {key} field {field}: '
                            f'old={old_rows[key][field]!r} new={new_rows[key][field]!r}')

        for bucket in ('public_projects', 'private_projects'):
            old_ids = {str(p['_id']) for p in old[bucket]}
            new_ids = {str(p['_id']) for p in new[bucket]}
            if old_ids != new_ids:
                problems.append(f'{bucket}: project sets differ, '
                                f'only-old={sorted(old_ids - new_ids)[:3]} '
                                f'only-new={sorted(new_ids - old_ids)[:3]}')
            old_counts = {str(p['_id']): p.get('sample_count_display') for p in old[bucket]}
            new_counts = {str(p['_id']): p.get('sample_count_display') for p in new[bucket]}
            for project_id in sorted(old_ids & new_ids):
                if old_counts[project_id] != new_counts[project_id]:
                    problems.append(
                        f'{bucket}: {project_id} sample count '
                        f'old={old_counts[project_id]} new={new_counts[project_id]}')
        return problems
