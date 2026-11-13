# amprepo_waf

Reads, checks and changes the AWS WAF web ACL in front of the site (`Amplicon_WAF`,
us-east-1, REGIONAL). It needs only the `aws` CLI and an AWS login that can read and
write WAF; no Python packages, no Django, no database.

## Why it exists

Until 2026-11 every WAF change was made by hand: `get-web-acl`, edit the JSON, then
`update-web-acl`. Two traps came out of that, written down only in notes nobody would find:

- **`update-web-acl` replaces the whole ACL.** A field you leave out is deleted. Leaving
  out `CustomResponseBodies` breaks `RateLimitApiV1`, which uses the `api-rate-limited`
  body. AWS also adds new fields over time.
- **Match strings are base64** in the CLI's JSON input, exactly as `get-web-acl` emits them.

This tool closes both. It sends back everything `get-web-acl` returned, apart from a
short list of read-only fields. The list of fields it sends comes from the installed
CLI's own `update-web-acl` skeleton, so a field AWS adds later is carried forward without
a code change. A field it has never seen stops the run instead of being dropped. The ACL
goes back through the CLI unchanged, so the base64 is never re-encoded by hand; use
`acl.b64()` for a new match string.

And there is one ACL for **both prod and dev**. A change cannot be staged; it is live on
prod the moment it is sent.

## Commands

Run from the repository root with an AWS login (`--profile`, default `$AWS_PROFILE` or `amprepo`).

```bash
python -m amprepo_waf show                  # rules in evaluation order, overrides, bodies
python -m amprepo_waf check                 # the checks below, against the live ACL
python -m amprepo_waf list-edits            # the named changes
python -m amprepo_waf edit <name>           # dry run: checks, AWS validation, diff
python -m amprepo_waf probe                 # requests against the site, with expected results
```

None of those writes anything. Applying does:

```bash
python -m amprepo_waf probe --expect-no-hint            # before
python -m amprepo_waf edit <name> --apply \
    --record-dir <durable directory> --lock-held-by <holder>
python -m amprepo_waf probe --expect-hint               # after; WAF changes take ~1 min
```

`--apply` is a **prod write**: it needs its own explicit ask and prod's operator lock (see
AGENTS.md). The tool runs where your AWS login is, and the lock lives in prod's database, so
the tool cannot check the lock itself. Take it on the prod box, and say who holds it with
`--lock-held-by`. `--no-lock "<reason>"` is for emergencies; the reason goes in the record.

The record directory gets a new subdirectory with `before.json`, `proposed.json`,
`diff.txt`, `meta.json` and `after.json`. Put it somewhere durable, not a scratch
directory, and not this public repository: the ACL shows exactly which paths and clients
get through. To undo:

```bash
python -m amprepo_waf restore <record>/before.json --apply --record-dir ... --lock-held-by ...
```

`restore` goes through the same checks. AWS's lock token protects against a concurrent
writer: if someone changed the ACL between the read and the write, the update fails.

## What `check` enforces

Every `edit` and `restore` must produce an ACL that passes these, or nothing is sent.

- **Rule order** (`acl.ORDER`, each with its reason). The ones that have mattered:
  `AllowCurlToAPIEndpoints` before KnownBadInputs, because that group inspects bodies and
  would reject aggregator uploads. KnownBadInputs before `AllowDiscoveryFiles`, so a Log4j
  payload aimed at `/robots.txt` is still blocked. Bot Control before `BlockUnverifiedAI`,
  whose labels exist only after Bot Control runs.
- Every custom response body a rule uses is defined.
- Bot Control's `CategoryAI` stays at Count. It blocked the verified ChatGPT fetcher that a
  person's question sends. `BlockUnverifiedAI` blocks the impostors instead.
- Every Bot Control override names a sub-rule that still exists. A Block override is only
  allowed on a sub-rule that AWS still ships as Block. Bot Control's version is not pinned,
  so AWS can change its defaults; this check is how we find out.

If a change you want trips one of these, change the check in `acl.py` in the same commit
and say why. The checks are the record of what the site depends on.

## Adding a change

Write a function in `edits.py` with `@edit('<name>', '<what and why>')`. It gets a copy of
the live ACL and the Bot Control defaults, and edits the copy in place. It must be
idempotent: run again on the result, it changes nothing. Add a test in
`tests/test_edits_and_apply.py`, run the dry run against the live ACL, and get the
change reviewed before `--apply`. Edits stay in the file after they ship, as the record
of what was done.

## Tests

```bash
python -m pytest amprepo_waf
```

These tests use neither AWS nor the site. The ACL in `tests/conftest.py` is built by hand
in the live ACL's shape, not copied from it. CI runs them as their own step.
