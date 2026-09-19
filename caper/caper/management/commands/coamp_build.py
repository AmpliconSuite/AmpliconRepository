"""Run one co-amplification graph build.  Launched by coamp_build.start_pending
as its own process; see that module for why it is not a thread.

    manage.py coamp_build <cache_key>

The build record must already exist and be RUNNING -- this command does not
claim a slot, it does the work for a slot that was claimed for it.
"""

import os
import sys

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Run one queued co-amplification graph build (launched by the site itself).'

    def add_arguments(self, parser):
        parser.add_argument('cache_key')

    def handle(self, *args, **options):
        from caper.coamp_build import builds_handle, run_build, RUNNING
        key = options['cache_key']
        doc = builds_handle.find_one({'_id': key})
        if doc is None or doc['state'] != RUNNING:
            raise CommandError(f'no running build recorded for {key!r}')
        ok = run_build(key, doc['project_ids'])
        # Not sys.exit: the app's ready() hook starts threads that would keep
        # the interpreter alive after the work is done.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0 if ok else 1)
