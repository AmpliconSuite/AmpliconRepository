"""`upload`: send reviewed archives to the site one at a time, and check what it stored.

The only stage that writes to a site, and it needs a site admin's token. Anyone else updates a
project through its edit page.

A real upload stops the batch at the first thing that goes wrong, so a problem costs one
project. Run it again to carry on: finished projects are skipped, and a project whose job was
still running is followed rather than sent again.
"""

import datetime
import json
import os
import sys
import time

from . import archive, pipeline, report
from .site_api import Refused, SiteError

log = pipeline.log

POLL_SECONDS = 20
# Hartwig's archive is 2.2 GB, and GridFS writes took 0.30 s/MiB in autumn 2026. Well over that,
# because reaching this limit means a job died without saying so.
JOB_LIMIT_SECONDS = 6 * 3600
# Long enough for a restart. A version's job survives one or dies; either way the status says.
OUTAGE_LIMIT_SECONDS = 15 * 60
PAGE_LIMIT_SECONDS = 30 * 60
RECORD_FILE = 'uploads.jsonl'
DRY_RUN_FILE = 'dry_run.md'
SUMMARY_FILE = 'summary.md'
# The fields a dry run and the real result both report.
_COMPARED_FILE_COUNTS = ('shared', 'shared_bytes', 'new', 'new_bytes')


class Stop(SiteError):
    """Don't send any more projects."""


def dry_run_concerns(dry_run):
    """What a dry run found that a person should look at before the real thing."""
    if not dry_run:
        return []
    if dry_run.get('state') == 'failed':
        return ['The site\'s dry run failed: %s' % dry_run.get('error')]
    found = []
    record = dry_run.get('record') or {}
    if record.get('fits') is False:
        found.append('The site\'s record limit: %.2f MB of %.2f MB'
                     % (record['bytes'] / 1e6, record['limit_bytes'] / 1e6))
    missing = (dry_run.get('files') or {}).get('missing')
    if missing:
        found.append('run.json names %d file(s) the archive lacks' % missing)
    return found


def select(plan, states, only=None, accept_flagged=False):
    """What can go up now: (list of (entry, state), list of (name, why not))."""
    ready, skipped = [], []
    for entry in plan['projects']:
        if only and entry['name'] not in only and entry.get('id') not in only:
            continue
        state = states.get(entry.get('id')) or {}
        result = state.get('result')
        upload = state.get('upload') or {}
        if upload.get('state') == 'done':
            skipped.append((entry['name'], 'already uploaded as %s' % upload['job_id']))
        elif not result:
            skipped.append((entry['name'], 'failed: %s' % state['failed'] if state.get('failed')
                            else 'no archive built (%s)' % entry.get('status')))
        elif upload.get('state') == 'started':
            ready.append((entry, state))  # nothing more to decide: it has gone up already
        elif entry.get('metadata_sheet') and not os.path.exists(entry['metadata_sheet']):
            skipped.append((entry['name'], 'no such metadata sheet: %s' % entry['metadata_sheet']))
        else:
            flagged = report.concerns(result) + dry_run_concerns(state.get('dry_run'))
            if flagged and not accept_flagged:
                skipped.append((entry['name'], 'flagged, see --accept-flagged: %s' % '; '.join(flagged)))
            else:
                ready.append((entry, state))
    return ready, skipped


def wait_for_job(site, job_id):
    """Follow a job to its end. A site that stops answering for a while isn't the job failing."""
    started = answered = time.monotonic()
    while True:
        try:
            status = site.replace_status(job_id)
            answered = time.monotonic()
            if status.get('state') != 'running':
                return status
        except Refused:
            raise
        except SiteError as error:
            if time.monotonic() - answered > OUTAGE_LIMIT_SECONDS:
                raise Stop('The site has not answered about job %s for %d minutes: %s'
                           % (job_id, OUTAGE_LIMIT_SECONDS // 60, error))
            log('  no answer yet: %s' % error)
        if time.monotonic() - started > JOB_LIMIT_SECONDS:
            raise Stop('Job %s has not finished after %d hours. Look at the project on the site '
                       'before going on.' % (job_id, JOB_LIMIT_SECONDS // 3600))
        time.sleep(POLL_SECONDS)


def _wait_for_page(site, version_id):
    """Get the new version's page ready. Not a failure if it can't: its first visitor does this."""
    try:
        if site.prepare_page(version_id):
            return True
        started = time.monotonic()
        while time.monotonic() - started < PAGE_LIMIT_SECONDS:
            if site.replace_status(version_id).get('page_ready'):
                return True
            time.sleep(POLL_SECONDS)
    except SiteError as error:
        log('  note: %s' % error)
    return False


def check_result(status, archive_path, target_ac, dry_run=None):
    """How what the site stored differs from what was sent. Empty means it matches."""
    sent = archive.run_json_summary(archive_path)
    misses = []
    for label, stored, expected in (('samples', status.get('samples'), sent['entries']),
                                    ('features', status.get('features'), sent['features'])):
        if stored != expected:
            misses.append('The site stored %s %s, and the archive has %s' % (stored, label, expected))
    stored_ac = (status.get('versions') or {}).get('AC_version')
    if stored_ac != target_ac:
        misses.append('The site reports AC %s, not %s' % (stored_ac, target_ac))
    predicted = (dry_run or {}).get('files')
    if predicted:
        stored_files = status.get('files') or {}
        for key in _COMPARED_FILE_COUNTS:
            if stored_files.get(key) != predicted.get(key):
                misses.append('Files %s: the dry run predicted %s and the site stored %s'
                              % (key, predicted.get(key), stored_files.get(key)))
    return misses


def _append_record(machine, record):
    os.makedirs(machine.output_dir, exist_ok=True)
    with open(os.path.join(machine.output_dir, RECORD_FILE), 'a') as handle:
        handle.write(json.dumps(record) + '\n')


def upload_one(entry, state, plan, machine, site):
    project_id = entry['id']
    archive_path = state['result']['archive']
    upload = state.get('upload') or {}
    if upload.get('state') == 'started':
        log('  following the job already started: %s' % upload['job_id'])
    else:
        log('  sending %.1f MB ...' % (os.path.getsize(archive_path) / 1e6))
        try:
            answer = site.replace(project_id, archive_path, project_id, dry_run=False,
                                  metadata_path=entry.get('metadata_sheet') or None)
        except Refused as refused:
            if refused.code == 'stale_version':
                raise Stop('%s The latest version is %s. If an earlier attempt lost its connection, '
                           'it may have got through, so look at the project first.'
                           % (refused, refused.latest_id))
            raise Stop(str(refused))
        except SiteError as error:
            raise Stop('%s Nothing can be sent twice, because the site checks the version, so '
                       'it\'s safe to run this again.' % error)
        upload = {'state': 'started', 'job_id': answer['job_id'], 'replaces': project_id}
        state['upload'] = upload
        pipeline.save_state(machine, project_id, state)
        log('  accepted. The new version is %s' % upload['job_id'])

    job_id = upload['job_id']
    status = wait_for_job(site, job_id)
    if status.get('state') != 'finished':
        del state['upload']
        pipeline.save_state(machine, project_id, state)
        raise Stop('The site could not make the new version of %s: %s. Its old version is %s.'
                   % (entry['name'], status.get('error'), status.get('restored_project_id') or 'unchanged'))

    page_ready = _wait_for_page(site, job_id)
    misses = check_result(status, archive_path, plan['target_ac'], state.get('dry_run'))
    latest = site.project(job_id)
    if not latest.get('is_latest_version', True):
        misses.append('The API says the new version is not the latest')
    url = site.project_url(job_id)
    _append_record(machine, {
        'time': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
        'site': site.name, 'name': entry['name'], 'replaced': project_id, 'new_version': job_id,
        'url': url, 'samples': status.get('samples'), 'features': status.get('features'),
        'versions': status.get('versions'), 'files': status.get('files'),
        'predicted_files': (state.get('dry_run') or {}).get('files'),
        'page_ready': page_ready, 'misses': misses})
    state['upload'] = dict(upload, state='done', misses=misses)
    pipeline.save_state(machine, project_id, state)

    files = status.get('files') or {}
    log('  stored: %s samples, %s features, AC %s. %s files shared, %s new (%.1f MB), archive %.1f MB.'
        % (status.get('samples'), status.get('features'), (status.get('versions') or {}).get('AC_version'),
           files.get('shared'), files.get('new'), (files.get('new_bytes') or 0) / 1e6,
           (files.get('archive_bytes') or 0) / 1e6))
    log('  %s' % url)
    if not page_ready:
        log('  note: its page isn\'t ready yet, so its first visitor will wait for that')
    if misses:
        raise Stop('%s went up, but not as expected:\n  %s' % (entry['name'], '\n  '.join(misses)))


def _print_skipped(skipped):
    for name, why in skipped:
        log('  skipped %s: %s' % (name, why))


def _require_review(machine, ready):
    """An archive newer than the summary, or no summary, hasn't been reviewed."""
    path = os.path.join(machine.output_dir, SUMMARY_FILE)
    if not os.path.exists(path):
        raise Stop('No change summary at %s. Run `summary` and read it first.' % path)
    for entry, state in ready:
        if os.path.getmtime(state['result']['archive']) > os.path.getmtime(path):
            raise Stop('%s was built after the change summary. Run `summary` and read it again.'
                       % entry['name'])


def _confirm(site_name, ready, assume_yes):
    log('\nTo upload to %s, in this order:' % site_name)
    for entry, state in ready:
        result = state['result']
        calls = result['calls']
        log('  %-40s %5d samples  ecDNA+ %d to %d  %d amplicon(s) changed  %.1f MB'
            % (entry['name'], result['archive_samples'], calls['ecdna_old'], calls['ecdna_new'],
               len(calls['amplicon_changes']), result['archive_bytes'] / 1e6))
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise Stop('Nothing is attached to confirm this. Pass --yes.')
    if input('\nType %s to send these %d project(s): ' % (site_name, len(ready))).strip() != site_name:
        raise Stop('Not confirmed. Nothing was sent.')


def run_uploads(plan, machine, site, only=None, accept_flagged=False, assume_yes=False):
    """Returns the number uploaded. Raises Stop at the first thing that goes wrong."""
    site.check_admin()
    ready, skipped = select(plan, pipeline.load_states(machine, plan['projects']), only, accept_flagged)
    _print_skipped(skipped)
    if not ready:
        log('Nothing to upload.')
        return 0
    _require_review(machine, ready)
    _confirm(site.name, ready, assume_yes)
    for done, (entry, state) in enumerate(ready):
        log('\n%s\n%s (%s)' % ('=' * 78, entry['name'], entry['id']))
        try:
            upload_one(entry, state, plan, machine, site)
        except SiteError as error:
            raise Stop('%s\n%d of %d uploaded before this. Run `upload` again to carry on.'
                       % (error, done, len(ready)))
    log('\nUploaded %d. The record is in %s' % (len(ready), os.path.join(machine.output_dir, RECORD_FILE)))
    return len(ready)


def run_dry_runs(plan, machine, site, only=None):
    """Ask the site what each upload would store. Writes nothing lasting to the site, and one
    failing doesn't stop the rest. Returns the number that failed."""
    site.check_admin()
    # Every built project, flagged or not: this is where a person finds out about them.
    ready, skipped = select(plan, pipeline.load_states(machine, plan['projects']), only, accept_flagged=True)
    _print_skipped(skipped)
    rows, failures = [], 0
    for entry, state in ready:
        log('\n%s (%s)' % (entry['name'], entry['id']))
        if (state.get('upload') or {}).get('state') == 'started':
            log('  skipped: its real upload has started')
            continue
        try:
            answer = site.replace(entry['id'], state['result']['archive'], entry['id'], dry_run=True,
                                  metadata_path=entry.get('metadata_sheet') or None)
            result = wait_for_job(site, answer['job_id'])
        except SiteError as error:
            result = {'state': 'failed', 'error': str(error)}
        state['dry_run'] = result
        pipeline.save_state(machine, entry['id'], state)
        failures += result.get('state') != 'finished'
        rows.append((entry, state))
        for concern in dry_run_concerns(result):
            log('  %s' % concern)
    text = dry_run_report(plan, rows)
    os.makedirs(machine.output_dir, exist_ok=True)
    path = os.path.join(machine.output_dir, DRY_RUN_FILE)
    with open(path, 'w') as handle:
        handle.write(text)
    log('\n' + text)
    log('Saved to %s' % path)
    return failures


def dry_run_report(plan, rows):
    lines = ['# Dry run on %s, to AC %s' % (plan['site'], plan['target_ac']), '',
             '| Project | Samples | Features | Record MB | Files shared | Files new | New MB | Archive MB |',
             '| --- | --- | --- | --- | --- | --- | --- | --- |']
    new_bytes = archive_bytes = 0
    attention = []
    for entry, state in rows:
        result = state['dry_run']
        attention += ['%s: %s' % (entry['name'], item) for item in dry_run_concerns(result)]
        if result.get('state') != 'finished':
            lines.append('| %s | failed | | | | | | |' % entry['name'])
            continue
        files = result['files']
        new_bytes += files['new_bytes']
        archive_bytes += files['archive_bytes']
        record = result['record']['bytes']
        lines.append('| %s | %s | %s | %s | %d | %d | %.1f | %.1f |' % (
            entry['name'], result['samples'], result['features'],
            '%.2f' % (record / 1e6) if record is not None else '?', files['shared'], files['new'],
            files['new_bytes'] / 1e6, files['archive_bytes'] / 1e6))
    lines += ['', 'The site would store %.2f GB: %.2f GB of new files and %.2f GB of whole archives.'
              % ((new_bytes + archive_bytes) / 1e9, new_bytes / 1e9, archive_bytes / 1e9), '',
              '## Check before uploading']
    lines += ['- %s' % item for item in attention] or ['Nothing flagged.']
    return '\n'.join(lines) + '\n'
