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

        # The name list is derived from the index the way the index is derived
        # from the projects, and it drifts the same way. Reported beside the
        # rows because a name a search cannot resolve is a sample the site
        # cannot find, however current the rows behind it are.
        self.stdout.write(
            f"searchable names {len(drift['names_missing']) + len(drift['names_extra'])} "
            f"disagreements")
        for label, key, meaning in (
            ('missing ', 'names_missing', 'a name search cannot find these'),
            ('extra   ', 'names_extra', 'harmless; these resolve to no rows'),
        ):
            names = drift[key]
            self.stdout.write(f"  {label} {len(names):>5}   {meaning}")
            for kind, name in names[:10]:
                self.stdout.write(f"             {kind:<8} {name}")
            if len(names) > 10:
                self.stdout.write(f"             ... and {len(names) - 10} more")

        # The gene catalogue is derived the same way and reported the same way,
        # but graded differently below: repo-wide on 2026-09-14 nothing reads
        # it but `manage.py compare_search_paths`, so drift here cannot give
        # anyone a wrong answer. Measured regardless -- a derived collection
        # nothing asks about is the defect this module keeps producing.
        genes = drift['genes_missing'] + drift['genes_extra'] + drift['genes_changed']
        self.stdout.write(f"gene catalogue {len(genes)} disagreements")
        for label, key, meaning in (
            ('missing ', 'genes_missing', 'in the index, not in the catalogue'),
            ('extra   ', 'genes_extra', 'in the catalogue, not in the index'),
            ('changed ', 'genes_changed', 'in both, described differently'),
        ):
            symbols = drift[key]
            self.stdout.write(f"  {label} {len(symbols):>5}   {meaning}")
            if symbols:
                self.stdout.write(f"             {', '.join(symbols[:10])}")
            if len(symbols) > 10:
                self.stdout.write(f"             ... and {len(symbols) - 10} more")

        # Should always be empty. It is printed rather than assumed because the
        # failure it guards against is a collection added to DERIVED_COLLECTIONS
        # and measured by nothing -- which is precisely how ``search_names``
        # came to drift unobserved, and which no amount of care remembers to
        # check by hand.
        if drift['unchecked']:
            self.stdout.write(self.style.ERROR(
                'derived collections no drift check covers: '
                + ', '.join(drift['unchecked'])))

        # What flips the verdict is whether a reader can get a wrong answer.
        # ``names_extra`` cannot: an extra name resolves to an $in entry
        # matching no rows, beside an access filter it does not replace. A
        # missing name can, because the search comes back short without saying
        # so. Gene drift cannot either, today, for want of a reader -- so it is
        # reported as the hazard it is rather than upgraded to an incident.
        if drift['missing'] or drift['stale'] or drift['orphaned'] or drift['names_missing']:
            self.stdout.write(self.style.WARNING(
                'index is not current; rebuild with: manage.py rebuild_feature_index'))
        elif genes:
            self.stdout.write(self.style.NOTICE(
                'rows and names are current; the gene catalogue is behind. '
                'Nothing reads it today, so this is a hazard, not a fault -- '
                'a rebuild clears it.'))
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
