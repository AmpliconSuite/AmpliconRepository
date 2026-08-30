"""Walk every project document and account for every GridFS file.

The measurement itself lives in ``caper.gridfs_ownership``; this is how it gets
run outside a web worker. It is the only runner -- the admin page's button
spawns this command rather than doing the work itself, so there is one
implementation and a person with a shell can produce the same snapshot the page
shows.

Read-only against ``projects`` and ``fs.files``. Nothing here deletes a file.

    manage.py ownership_survey                     # measure and store
    manage.py ownership_survey --notify me@x.org   # ...and mail me when done
    manage.py ownership_survey --report-id <oid>   # fill in a row the page made
"""

from django.core.management.base import BaseCommand

from caper import ownership_survey


class Command(BaseCommand):
    help = 'Measure how much of GridFS is still owned by a project.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--report-id', default=None,
            help='Fill in an existing "running" row instead of opening one. '
                 'The admin page passes this so the row it already showed the '
                 'user is the row that gets the result.')
        parser.add_argument(
            '--started-by', default=None,
            help='Who asked for this run. Stored on the snapshot.')
        parser.add_argument(
            '--notify', default=None,
            help='Email address to tell when the survey finishes or fails.')

    def handle(self, *args, **options):
        from bson.objectid import ObjectId

        from django.conf import settings

        reports, projects, fs_files = ownership_survey.collections()
        started_by = options['started_by'] or 'command line'
        notify = options['notify']

        if options['report_id']:
            report_id = ObjectId(options['report_id'])
            existing = reports.find_one({'_id': report_id})
            if existing is None:
                self.stderr.write(f'no report row {report_id}')
                return
            # The page recorded who to tell when it opened the row; a command
            # line --notify overrides it, absence of one does not clear it.
            notify = notify or existing.get('notify')
        else:
            report_id = ownership_survey.open_report(reports, started_by,
                                                     notify)

        database = getattr(projects, 'database', None)
        self.stdout.write(f'database: {getattr(database, "name", "?")}  '
                          f'report: {report_id}')

        result = ownership_survey.run_and_store(
            reports, projects, fs_files, report_id,
            started_by=started_by, notify=notify)

        if result is None:
            self.stderr.write('survey failed; see the stored report row')
            return

        self.stdout.write(
            f'{result["documents"]:,} documents, '
            f'{result["total_files"]:,} files, '
            f'{result["residue"]:,} residue')
        for label, count in (result.get('counts') or {}).items():
            self.stdout.write(f'  {label}: {count:,}')
        self.stdout.write(
            f'{settings.SITE_URL.rstrip("/")}/admin-file-ownership/'
            f'?snapshot={report_id}')
