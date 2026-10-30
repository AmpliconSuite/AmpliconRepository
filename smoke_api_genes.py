#!/usr/bin/env python3
"""Read-only smoke test of gene resolution in the feature API, against a running site.

The contract itself is pinned in-process by tests/test_api_features.py and
tests/test_gene_ids.py.  This checks a deployed server: that its gene map, its
search index and its code agree, which only a live corpus can show.  Every
check is an invariant that holds on any corpus -- equal counts for the names of
one gene, the documented 400 codes, no 500s, bounded response times -- so the
same run is meaningful locally, on dev, and anywhere else.

Only GETs to /api/v1/features/... and /api/gene-suggest/; neither records a
view or a download.  The anonymous API limit is 60 requests a minute, so the
run waits out each 429 it gets and takes about two minutes.

    python smoke_api_genes.py --base-url http://127.0.0.1:8000

Exit status is the number of failed checks.
"""

import argparse
import json
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

SLOW_SECONDS = 10.0


class Probe:
    def __init__(self, base_url):
        self.base = base_url.rstrip('/')
        self.failures = 0
        self.slowest = (0.0, '')

    def get(self, path, **params):
        url = self.base + path + ('?' + urlencode(params, quote_via=quote) if params else '')
        while True:
            status, data, elapsed = self._fetch(url)
            if status != 429:
                break
            time.sleep(float(data.get('retry_after') or 30) + 1)
        if elapsed > self.slowest[0]:
            self.slowest = (elapsed, url)
        return status, data, elapsed

    @staticmethod
    def _fetch(url):
        request = Request(url, headers={'Accept': 'application/json', 'User-Agent': 'smoke_api_genes'})
        start = time.monotonic()
        try:
            with urlopen(request, timeout=120) as response:
                status, body = response.status, response.read()
        except HTTPError as error:
            status, body = error.code, error.read()
        elapsed = time.monotonic() - start
        try:
            data = json.loads(body)
        except ValueError:
            data = {'_raw': body[:200].decode('utf-8', 'replace')}
        return status, data, elapsed

    def check(self, label, ok, detail=''):
        print(('PASS ' if ok else 'FAIL ') + label + (f'  [{detail}]' if detail else ''))
        if not ok:
            self.failures += 1

    def count(self, path='/api/v1/features/', **params):
        # The samples endpoint has no count_only; one row is the cheapest page.
        shape = {'count_only': 'true'} if path == '/api/v1/features/' else {'limit': 1}
        status, data, elapsed = self.get(path, **shape, **params)
        return status, data.get('count'), data, elapsed


def run(probe):
    features, samples = '/api/v1/features/', '/api/v1/features/samples/'

    # One gene under every name: same rows, same samples.
    for names in (['MIEN1', 'C17orf37', 'mien1', ' MIEN1 ', '84299', 'GeneID:84299'],
                  ['ERBB2', 'HER2', '2064'], ['MYC', '4609']):
        for path in (features, samples):
            got = {n: probe.count(path, gene_any=n)[:2] for n in names}
            statuses = {s for s, _c in got.values()}
            counts = {c for _s, c in got.values()}
            probe.check(f'{path} {names[0]} under {len(names)} names', statuses == {200} and len(counts) == 1,
                        ', '.join(f'{n!r}={c}' for n, (_s, c) in got.items()))

    # gene_terms says what each term meant, and annotated_in where AC has it.
    status, data, _ = probe.get(features, gene_any='C17orf37', limit=1)
    term = (data.get('gene_terms') or [{}])[0]
    probe.check('gene_terms for C17orf37', status == 200 and term.get('gene_id') == 84299
                and term.get('symbol') == 'MIEN1' and term.get('matched_as') == 'ac_name'
                and term.get('annotated_in') == ['hg19', 'hg38'], json.dumps(term))
    rows = data.get('results') or []
    probe.check('rows carry gene_ids', all(84299 in (r.get('gene_ids') or []) or r.get('reference_build') not in ('hg19', 'hg38')
                                           for r in rows), f'{len(rows)} rows')

    # A symbol means its own gene even when it is another gene's alias.
    status, data, _ = probe.get(features, gene_any='PDK1', limit=1)
    term = (data.get('gene_terms') or [{}])[0]
    probe.check('PDK1 is PDK1, PDPK1 reported', status == 200 and term.get('symbol') == 'PDK1'
                and [a.get('symbol') for a in term.get('also_alias_of', [])] == ['PDPK1'], json.dumps(term))

    # Errors are 400s with their documented codes, for both gene parameters and every endpoint.
    for path in (features, samples):
        for param in ('gene_any', 'gene_all'):
            for value, code in (('NEU', 'ambiguous_gene'), ('FLJ12345', 'gene_not_tracked'),
                                ('MYC,NOSUCHGENE', 'gene_not_tracked'), ('MYC,NEU', 'ambiguous_gene')):
                status, _count, data, _ = probe.count(path, **{param: value})
                probe.check(f'{path} {param}={value} -> 400 {code}', status == 400 and data.get('code') == code,
                            f"{status} {data.get('code')}")

    # Hostile or malformed text: a 400, never a 500, and quick.
    # Kept under gunicorn's 4094-byte request line, which answers a longer URL with its own 400.
    for value in ('MYC*', 'MYC&EGFR', 'MYC|EGFR', 'x' * 3000, 'MYC✓', '<script>', "'; DROP", 'GeneID:',
                  'GeneID:0', '0', '99999999999999999999999', '-1', '%00', '\\', '.*', '(', 'Myc ecDNA'):
        status, data, elapsed = probe.get(features, gene_any=value, count_only='true')
        probe.check(f'gene_any={value[:24]!r} -> 400', status == 400 and elapsed < SLOW_SECONDS,
                    f"{status} {data.get('code')} {elapsed:.2f}s")

    # Empty lists mean "no gene filter", as before.
    total = probe.count(features)[1]
    for value in ('', ',', ' , '):
        status, count, _d, _e = probe.count(features, gene_any=value)
        probe.check(f'gene_any={value!r} is no filter', status == 200 and count == total, f'{count} vs {total}')

    # AND across a sample, and on one amplicon, never exceed either gene alone.
    _s, cdk4, *_ = probe.count(samples, gene_any='CDK4')
    _s, mdm2, *_ = probe.count(samples, gene_any='MDM2')
    status, both, *_ = probe.count(samples, gene_all='CDK4,MDM2')
    status2, same, *_ = probe.count(samples, gene_all='CDK4,MDM2', same_amp='true')
    probe.check('gene_all CDK4,MDM2 <= each alone, same_amp <= gene_all',
                status == status2 == 200 and same <= both <= min(cdk4, mdm2), f'{cdk4} {mdm2} {both} {same}')
    _s, alias_both, *_ = probe.count(samples, gene_all='CDK4,HDM2')   # HDM2 is an alias of MDM2
    probe.check('gene_all resolves aliases', alias_both == both, f'{alias_both} vs {both}')

    # Many terms: bounded time.
    many = 'MYC,EGFR,ERBB2,CDK4,MDM2,CCND1,MYCN,PDGFRA,KIT,MET,KRAS,AKT2,CCNE1,FGFR1,FGFR2,AR,TERT,SOX2,PVT1,CDK6'
    for param in ('gene_any', 'gene_all'):
        status, _c, _d, elapsed = probe.count(samples, **{param: many})
        probe.check(f'{param} with 20 genes', status == 200 and elapsed < SLOW_SECONDS, f'{status} {elapsed:.2f}s')

    # Non-human results keep matching by spelling (no mouse Gene IDs yet).
    # Skipped where the caller can see no mm10 rows (dev's only mm10 project is private).
    status, mouse_rows, *_ = probe.count(features, reference_build='mm10')
    if status != 200 or not mouse_rows:
        print('SKIP MYC on mm10: no mm10 results visible here')
    else:
        status, myc_mouse, *_ = probe.count(features, gene_any='MYC', reference_build='mm10')
        probe.check('MYC on mm10 still matches by spelling', status == 200 and myc_mouse > 0,
                    f'{myc_mouse} of {mouse_rows} mm10 rows')

    # Autocomplete.
    for q, expect in (('AR', 'AR'), ('C17orf3', 'MIEN1'), ('HER2', 'ERBB2'), ('MYC', 'MYC')):
        status, data, elapsed = probe.get('/api/gene-suggest/', q=q)
        symbols = [s['symbol'] for s in data.get('suggestions', [])]
        probe.check(f'suggest {q!r} starts with {expect}', status == 200 and symbols[:1] == [expect]
                    and elapsed < 2, f'{symbols[:3]} {elapsed * 1000:.0f} ms')
    for q in ('', 'MY', 'x' * 3000, '<script>', '%', 'MYC*'):
        status, data, _ = probe.get('/api/gene-suggest/', q=q)
        probe.check(f'suggest {q[:12]!r} is empty, not an error', status == 200 and data.get('suggestions') == [],
                    f'{status} {len(data.get("suggestions") or [])}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    args = parser.parse_args(argv)
    probe = Probe(args.base_url)
    run(probe)
    print(f'\n{probe.failures} failed; slowest {probe.slowest[0]:.2f}s {probe.slowest[1][:140]}')
    return probe.failures


if __name__ == '__main__':
    sys.exit(main())
