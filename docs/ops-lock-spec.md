# Operator Lock (`ops_lock`) — Specification

**Status:** decisions settled (§11), not implemented
**Date:** 2026-10-31
**Related:** `shutdown_mode.py` (the model this follows), `docs/backend-architecture.md` §7–§8

---

## 1 · Purpose

Several people and several agents now work against the same shared environments,
usually dev and occasionally prod. Each of them runs backfills, repairs, sweeps,
deploys, restarts and test runs that create and delete projects. Two of these
running at once against the same database can hurt each other:

- a sweeper reads a reference snapshot while a backfill rewrites the fields it
  depends on;
- a deploy restarts the container in the middle of someone's aggregation test;
- two agents each "verify" a fix by reading state that the other one is changing.

Nothing currently tells an operator that someone else is mid-change. Shutdown
mode keeps *users* out. It says nothing to other *operators*.

This spec adds one **advisory lock per environment** with a **lease**. An operator,
human or agent, takes the lock before writing to a shared environment, renews it
while working, and releases it when done. Anyone else who checks sees who holds
it, why, and until when, and waits.

This is a standard pattern: Terraform state locking, etcd and Consul leases, and
deploy locks are all versions of it. What follows is the smallest version that
fits this repository.

## 2 · Why not an on/off flag or an environment variable

| Approach | What goes wrong |
|---|---|
| Environment variable | An environment variable exists only inside one process. An agent in another shell, container or cloud session cannot see it. |
| On/off flag in MongoDB | A session that crashes, runs out of context, is reclaimed or is interrupted never clears it. The flag then blocks everyone indefinitely, and people learn to ignore it, which is worse than having no flag. |
| Read the flag, then set it | Two operators can both read "free" and both set it. |

The design below fixes each one. The lock lives in the target environment's own
MongoDB, so every operator can see it. It expires unless renewed. It is taken in
one atomic write.

An environment variable still has one job here (§5.3). It carries the holder's
identity into a child process, not the lock itself.

## 3 · Scope

### 3.1 What needs the lock

- Any write to a **shared** environment's MongoDB, GridFS or S3: the `--apply` or
  `--execute` mode of any script in §6, and `manage.py` commands that write.
- Deploys, container restarts, Neo4j restarts, and `shutdown_mode.py on/off`.
- Migrations and backfills.
- Test runs or manual testing against dev that create, edit or delete projects.

### 3.2 What does not

- Reads, reports, surveys and dry runs.
- Work against your own local MongoDB that no other agent shares. The tool still
  works locally, and agents sharing one machine's database should use it.
- Ordinary use of the website by users and admins (see §7).

### 3.3 One lock per environment

There is a single lock per database: `caper` (prod), `caper-dev` (dev), and each
local database. Named or scoped locks ("gridfs", "deploy") are deliberately left
out. They bring deadlocks and arguments about which operations overlap. They can
be added later if one coarse lock turns out to be the bottleneck. §9 lists the
signal that would justify it.

## 4 · Data model

One document in the existing `system_settings` collection, next to
`system_flags`:

```js
{
  _id:         'ops_lock',
  holder:      'claude:session_01C3qw3iHstcUVXJpdbAAx2n',  // or 'human:jluebeck'
  owner:       'jluebeck',            // the person accountable for the holder
  reason:      'testing rebuild_version_chains --apply on dev',
  host:        'ip-10-0-1-23 / amplicon-dev',
  acquired_at: ISODate,
  renewed_at:  ISODate,
  expires_at:  ISODate,
  token:       NumberLong             // increments on every acquire, see §5.5
}
```

When released, the document stays with `holder: null`. This keeps `token`
monotonic and leaves the previous holder visible in `status`.

Every acquire, renew failure, release, expiry takeover and break is appended to a
new `ops_lock_log` collection: `{at, event, db, holder, owner, reason, previous_holder, token}`.
It has no TTL. It is small, and "who was changing dev on Tuesday night" is
exactly the question it answers.

**Holder format.** Use `claude:<session id>` for Claude Code sessions,
`agent:<tool>:<id>` for other agents, and `human:<username>` for people. `owner`
is always a person.

## 5 · The tool: `ops_lock.py`

It goes at the repo root, next to `shutdown_mode.py`, and uses the same
conventions. `--expect-db` is required and aborts before any read if `DB_NAME`
differs. It reads and writes the primary. It uses the same exit codes: 0 done,
1 refused or timed out, 2 usage.

```
ops_lock.py --expect-db caper-dev status
ops_lock.py --expect-db caper-dev acquire --holder H --owner O --reason "..." [--ttl 1h]
ops_lock.py --expect-db caper-dev renew   --holder H [--ttl 1h]
ops_lock.py --expect-db caper-dev release --holder H
ops_lock.py --expect-db caper-dev wait    [--timeout 1800] [--interval 30]
ops_lock.py --expect-db caper-dev run     --holder H --owner O --reason "..." [--ttl 1h] -- <command...>
ops_lock.py --expect-db caper-dev break   --reason "..."
```

`--holder` defaults to `$OPS_LOCK_HOLDER` when set.

### 5.1 Acquire is one atomic write

```python
db.system_settings.find_one_and_update(
    {'_id': 'ops_lock',
     '$or': [{'holder': None},
             {'expires_at': {'$lte': '$$NOW'}},   # via $expr
             {'holder': me}]},
    [{'$set': {..., 'acquired_at': '$$NOW',
               'expires_at': {'$add': ['$$NOW', ttl_ms]},
               'token': {'$add': [{'$ifNull': ['$token', 0]}, 1]}}}],
    upsert=True, return_document=AFTER)
```

If another holder's unexpired lease exists, the filter misses, the upsert
collides on `_id`, and the result is a `DuplicateKeyError`. That error means
"held". The tool then prints the holder, owner, reason, age and time remaining,
and exits 1.

**Times come from the MongoDB server (`$$NOW`), never from the client.** Agents
run on laptops, cloud containers and EC2, and their clocks disagree. (`$$NOW` in
an update pipeline needs MongoDB ≥ 4.2. Check the deployed version before
implementing.)

Re-acquiring as the current holder counts as a renew. It does not raise an error.

### 5.2 Expiry and breaking

- **Expired lease.** `acquire` takes it, with no flag needed, but prints whose
  lease it replaced and logs `event: 'expired_takeover'`. It never takes one
  silently.
- **Unexpired lease held by someone else.** Only `break --reason` takes it. It
  needs a reason and is logged. On prod, `break` is its own explicit request to
  the user, the same as any other prod write. The normal response to a held lock
  is `wait`, or reporting to your user, not `break`.
- `release` only succeeds for the current holder. Releasing a lock you do not hold
  is an error, not a no-op, because it usually means your lease already expired
  and someone else took it.

### 5.3 `run --`: the default way to use it

`run` acquires the lock, starts the command with `OPS_LOCK_HOLDER` set in the
child's environment, renews the lease every `ttl/4` (15 min at the 1 h default)
in a background thread, and releases it in a `finally` block. The command's exit status becomes the tool's
exit status. Ctrl-C and SIGTERM are passed to the child, and the lock is still
released.

For scripted work, a forgotten release is then not possible. Manual
`acquire`/`renew`/`release` is for interactive sessions with several steps. Those
sessions should pick a TTL that matches the next stretch of work, not the whole
day.

### 5.4 `wait`

`wait` polls until the lock is free or expired, or until the timeout. It does not
acquire. Two waiters must not both take it the moment it frees. The caller runs
`acquire` afterwards, and the atomic write settles who gets it.

### 5.5 A lost lease

The known weakness of leases is that a holder can stall past its expiry (the
laptop sleeps, a worker hangs) while someone else takes the lock.
Two measures cover it:

1. **Holders check before each destructive step.** The `require()` helper in §6
   compares the holder and the `token` it saw at acquire time. If the token has
   changed, the lease was lost and the script stops.
2. **`run` does not kill its child when a renewal fails.** Interrupting a write
   halfway is worse than a short overlap. The tool logs `event: 'renew_failed'`,
   prints a loud warning, and exits non-zero after the child finishes.

This is not a full fencing scheme. The writes are not rejected at MongoDB.
That is a reasonable trade for an advisory lock among cooperating operators.

## 6 · Enforcement in the repo's own write tools

The lock stays advisory for the site itself. The repo's own write tools check it,
so the rule does not depend only on agents remembering it.

A small helper module, imported by each tool:

```python
ops_lock.require(db, purpose='sweep_gridfs_unreferenced --execute')
```

When `OPS_LOCK_HOLDER` names the current unexpired holder, the call passes.
Otherwise the tool refuses and prints the current lock state. `--no-lock "<reason>"`
skips the check, logs the reason to `ops_lock_log`, and prints it.

`require()` is called only in write mode, never in dry-run or report mode. The
first tools to get it:

| Tool | Write mode |
|---|---|
| `shutdown_mode.py` | `on`, `off` |
| `recover_deleted_version.py` | `--apply` |
| `sweep_gridfs_unreferenced.py`, `sweep_s3_unreferenced.py` | `--execute` / `--apply` |
| `cleanup_orphaned_projects.py`, `cleanup_failed_upload_residue.py`, `clear_stale_uploads.py` | non-dry-run |
| `purge-local-db.py` | `--execute` |
| `backfill_*.py`, `repair_*.py`, `rebuild_version_chains.py`, `dedupe_sample_level_copies.py`, `zero_carried_forward_views.py` | `--apply` / `--execute` |
| `local_upload_project.py`, `manage.py create_project` | always (they create projects) |

Before each tool is changed, confirm the exact write-mode flag from its argparse.

## 7 · Visibility on the site

- **Admin pages** show one line in the header when the lock is held: *"dev
  operator lock held by claude:session_… (jluebeck) since 14:02, expires 16:02:
  testing rebuild_version_chains"*. It costs one indexed `find_one` and appears
  only on admin pages.
- **Public pages show nothing.** Users are not affected by operator coordination.
  Shutdown mode exists for when they need to be.
- **The site never enforces the lock.** Admin web actions such as delete project,
  featured projects and prepare shutdown are not blocked. The admin-page line is
  the signal for humans in the browser. Revisit this only if an admin-page action
  ever collides with a locked operation.
- The dev landing gate (`dev_gate.py`) is not touched. It must not touch MongoDB.

## 8 · Relationship to shutdown mode and the deploy runbooks

They are separate mechanisms that compose:

| | Keeps out | Stored in |
|---|---|---|
| Shutdown mode | users (new uploads, with a banner) | `system_settings/system_flags.shutdown_pending` |
| Operator lock | other operators | `system_settings/ops_lock` |

The deploy runbooks in the wiki become:

1. `ops_lock.py acquire` (or wrap the whole deploy in `run --`)
2. `shutdown_mode.py wait`, then `on`
3. deploy
4. `shutdown_mode.py off`
5. `ops_lock.py release`

**Restores.** A database restore or wipe also replaces `system_settings`, and the
lock document with it. Anyone restoring dev or prod re-acquires the lock
immediately afterwards.

## 9 · Documentation

These changes ship in the same PR as the tool. The rule must not appear in
`AGENTS.md` before the tool exists.

- **`AGENTS.md`**: a short section near the top, next to the `config.sh` warning:
  > **Before any write to dev or prod**, run `ops_lock.py --expect-db <db> status`.
  > Wrap the work in `ops_lock.py run -- …`, or `acquire` it and `release` it when
  > done. If the lock is held, report the holder and reason to your user and use
  > `wait`. Do not `break` it or work around it.

  This is followed by the §3.1 list of what counts as a write.
- **`docs/backend-architecture.md`**: a row in the §7 toolkit and a line in §8.
- **`ops_lock.py` docstring**: the full operating notes, as in `shutdown_mode.py`.
- **Wiki deploy runbooks**: the steps in §8.
- **Each developer's user-level `CLAUDE.md`** (outside the repo): one line,
  "for AmpliconRepository work, follow the operator-lock rule in AGENTS.md". This
  covers agents started outside a checkout of this repo.
- **Later, optional:** a Claude Code `PreToolUse` hook that blocks
  `docker exec … amplicon-(dev|prod)` while the session does not hold the lock.
  Hold off until the basic version has been in use for a while.

**Signal for revisiting §3.3.** If `ops_lock_log` shows operators often waiting
on each other for work that touches unrelated data, add named locks.

## 10 · Tests

The tests go in a new `tests/test_ops_lock.py`, marked `integration` (live
MongoDB). The neighbouring `tests/test_shutdown_mode.py` was considered as a
home. It is the closest subject, but this is a separate tool with its own state
and log collection, so it gets its own file, mirroring that one.

Behaviours to cover:

- acquire on an empty or released lock succeeds, and `token` increments;
- acquire against an unexpired lease held by another holder exits 1, writes
  nothing, and prints the holder;
- acquire against an expired lease succeeds and logs `expired_takeover`;
- two concurrent acquires: exactly one wins;
- release by a non-holder is refused; release by the holder clears `holder`;
- `break` requires `--reason` and logs it;
- `run --` releases on success, on non-zero exit and on SIGTERM, and passes
  `OPS_LOCK_HOLDER` to the child;
- `require()` passes for the holder, refuses otherwise, and `--no-lock` logs;
- `--expect-db` mismatch aborts before any read.

To test expiry, give the tool a TTL in seconds instead of sleeping for minutes.

## 11 · Decisions

Settled 2026-10-31.

1. **The write tools refuse, from the first release.** On `caper` and
   `caper-dev`, a write tool run without the lock refuses. On local databases it
   only warns. A warning that agents can scroll past does not coordinate
   anything. `--no-lock "<reason>"` remains for emergencies, and its reason is
   logged.
2. **TTL: 1 h default, 6 h maximum.** `run` renews every 15 min (`ttl/4`).
   `acquire` and `renew` refuse a `--ttl` above 6 h, so a forgotten interactive
   lock clears within a working day.
3. **The deploy is always wrapped in `run --`.** That covers `git pull` and the
   container restart. The restart is the operation most likely to break someone
   else's test.
4. **Of the `manage.py` commands, only `create_project` goes through
   `require()` at first.** Others are added as they are found to write to shared
   environments.
