#!/usr/bin/env python
"""
download_refs.py

Fetch the AmpliconArchitect reference builds to a local directory and check
them against the md5sums stored beside them.

These are the one store this site depends on that no backup covers, and they
cannot be regenerated from anything here: 26.30 GiB across ten tarballs, in a
bucket with no versioning and no replication, measured 2026-09-02. Every
analysis the site serves was produced against them.

The bucket is public, so this needs no credentials and can run from any
machine -- which is the point. A copy inside AWS is not the copy that matters.

Downloads are resumable in the only sense that counts here: a file whose local
md5 already matches the published sum is skipped, so re-running after an
interrupted transfer costs one hash per completed file rather than 26 GiB.

Usage:
    python download_refs.py --out ~/amplicon-refs
    python download_refs.py --out ~/amplicon-refs --verify-only
"""

import argparse
import hashlib
import os
import sys
import urllib.request

BASE = 'https://refs.ampliconrepository.org'
PREFIX = 'data/module_support_files/AmpliconArchitect'

BUILDS = ('GRCh37', 'GRCh38', 'GRCh38_viral', 'hg19', 'mm10')
# Both flavours of each build: the plain reference and the indexed one that
# AmpliconArchitect actually reads. The indexed tarballs are the large ones.
NAMES = tuple('%s%s.tar.gz' % (b, s) for b in BUILDS for s in ('', '_indexed'))


def published_md5(name):
    """The md5 the bucket publishes for *name*, or None.

    The sums live in <build>_md5sum.txt beside each tarball. Returning None on
    any failure is deliberate: an unreadable sum must read as "cannot verify",
    never as "verified".
    """
    stem = name[:-len('.tar.gz')]
    url = '%s/%s/%s_md5sum.txt' % (BASE, PREFIX, stem)
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            text = response.read().decode('utf-8', 'replace').strip()
    except Exception as error:
        print('  cannot read %s (%s)' % (url, type(error).__name__))
        return None
    # The files hold "<md5>  <filename>"; take the first field of the first line.
    first = text.splitlines()[0].split()
    return first[0] if first else None


def local_md5(path):
    digest = hashlib.md5()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def fetch(name, out_dir):
    """Download *name* unless a verified copy is already there. Returns a verdict."""
    path = os.path.join(out_dir, name)
    expected = published_md5(name)

    if os.path.exists(path):
        actual = local_md5(path)
        if expected is None:
            return 'present, UNVERIFIED (no published md5)'
        if actual == expected:
            return 'already present and verified'
        print('  local copy does not match the published md5 -- refetching')

    url = '%s/%s/%s' % (BASE, PREFIX, name)
    tmp = path + '.part'
    print('  fetching %s' % url)
    urllib.request.urlretrieve(url, tmp)
    os.replace(tmp, path)

    if expected is None:
        return 'downloaded, UNVERIFIED (no published md5)'
    actual = local_md5(path)
    if actual != expected:
        return 'DOWNLOADED BUT md5 MISMATCH (%s != %s)' % (actual, expected)
    return 'downloaded and verified'


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', required=True,
                        help='directory to download into (created if absent)')
    parser.add_argument('--verify-only', action='store_true',
                        help='check what is already there and download nothing')
    args = parser.parse_args()

    out_dir = os.path.expanduser(args.out)
    os.makedirs(out_dir, exist_ok=True)
    print('reference builds -> %s\n' % out_dir)

    problems = []
    for name in NAMES:
        print('%s' % name)
        path = os.path.join(out_dir, name)
        if args.verify_only:
            if not os.path.exists(path):
                verdict = 'MISSING'
            else:
                expected = published_md5(name)
                if expected is None:
                    verdict = 'present, UNVERIFIED (no published md5)'
                elif local_md5(path) == expected:
                    verdict = 'verified'
                else:
                    verdict = 'md5 MISMATCH'
        else:
            try:
                verdict = fetch(name, out_dir)
            except Exception as error:
                verdict = 'FAILED (%s: %s)' % (type(error).__name__, error)
        print('  %s\n' % verdict)
        if verdict.split()[0].isupper() or 'UNVERIFIED' in verdict:
            problems.append('%s: %s' % (name, verdict))

    total = sum(os.path.getsize(os.path.join(out_dir, n))
                for n in NAMES if os.path.exists(os.path.join(out_dir, n)))
    print('%d of %d file(s) present, %.2f GiB' % (
        sum(1 for n in NAMES if os.path.exists(os.path.join(out_dir, n))),
        len(NAMES), total / 1024 ** 3))

    if problems:
        print('\nNOT a complete verified copy:')
        for problem in problems:
            print('  ' + problem)
        return 1
    print('\nComplete and verified against the published md5sums.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
