"""Build, stage or check the feature index.

The index is a flat, indexable copy of the values ``perform_search`` reads out
of every project document.  Why it exists, and why it can be rebuilt without
risk, is in ``caper/feature_index.py``.

    manage.py rebuild_feature_index --check       # report drift, write nothing
    manage.py rebuild_feature_index --limit 5     # stage on five projects
    manage.py rebuild_feature_index               # full rebuild

``--check`` is the falsifying measurement for "the index is current" and is the
one to reach for first: it is read-only, it takes a projection rather than
whole documents, and a clean result means both that the write hooks fired and
that nothing wrote around them.

Staging is not ceremony.  ``--limit N`` reindexes N projects and leaves the
rest alone, so a builder change can be checked against real documents before it
is applied to all of them.  A staged run whose numbers do not match the
prediction is a stop, not a retry.
"""

from django.core.management.base import BaseCommand

from caper import feature_index


class Command(BaseCommand):
    help = 'Rebuild the searchable feature index from the project documents.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--check', action='store_true',
            help='Report what disagrees between projects and the index, and '
                 'write nothing.')
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Reindex at most this many projects. Leaves every other '
                 'project untouched, including ones no longer indexable.')
        parser.add_argument(
            '--quiet', action='store_true',
            help='Report totals only, not each project as it is indexed.')

    def handle(self, *args, **options):
        if options['check']:
            return self._check()
        return self._rebuild(options['limit'], options['quiet'])

    def _check(self):
        drift = feature_index.feature_index_drift()
        self.stdout.write(
            f"indexable projects {drift['indexable']}, indexed {drift['indexed']}")
        for label, key, meaning in (
            ('missing ', 'missing', 'search under-reports these'),
            ('stale   ', 'stale', 'search reports an older truth for these'),
            ('orphaned', 'orphaned', 'search over-reports these'),
        ):
            ids = drift[key]
            self.stdout.write(f"  {label} {len(ids):>5}   {meaning}")
            for project_id in ids[:10]:
                self.stdout.write(f"             {project_id}")
            if len(ids) > 10:
                self.stdout.write(f"             ... and {len(ids) - 10} more")

        if drift['missing'] or drift['stale'] or drift['orphaned']:
            self.stdout.write(self.style.WARNING(
                'index is not current; rebuild with: manage.py rebuild_feature_index'))
        else:
            self.stdout.write(self.style.SUCCESS('index is current'))

    def _rebuild(self, limit, quiet):
        feature_index.ensure_feature_index_indexes()

        def progress(name, projects, rows):
            if not quiet:
                self.stdout.write(f"  [{projects:>4}] {rows:>8,} rows  {name}")

        result = feature_index.rebuild_feature_index(limit=limit, progress=progress)
        genes = feature_index.rebuild_gene_catalog()
        names = feature_index.rebuild_search_names()

        self.stdout.write(self.style.SUCCESS(
            f"indexed {result['projects']} projects, {result['rows']:,} rows; "
            f"{genes:,} gene symbols, {names:,} searchable names"))
        if limit:
            self.stdout.write(
                'staged run: projects beyond the limit were left as they were, '
                'and no longer-indexable project was removed. Run --check to see '
                'what is still outstanding.')
