# Getting a shell on the dev and prod servers

## Why this document exists

Several places in `README.md` say "SSH into the EC2 instance — this requires a PEM
key." That has not been true for a while. There is no PEM key: the dev and prod
instances have no long-lived SSH key associated with them, nothing is distributed
to team members, and `~/.ssh` on the maintainer's laptop is empty of anything
relevant. Taken at face value the README reads as "you need a credential that does
not exist," and the reasonable conclusion — reached more than once — is that the
servers are simply unreachable without asking someone for a file.

They are reachable. Access is **EC2 Instance Connect**, which replaces the stored
key with a temporary one minted per command and authorised by your AWS login. This
document is how you use it.

Nothing here is a secret, which is why it lives in the public repository. The
mechanism is stock AWS, and the commands below contain no account number, no
instance ID, and no key material — they look those up at runtime through
credentials you have to already hold. Running them without access to the AWS
account gets you an `AccessDenied` and nothing else. What actually gates entry is
your IAM permission to call `ec2-instance-connect:SendSSHPublicKey` on those
instances, plus the security group; publishing the procedure grants nobody
anything they did not already have.

## How Instance Connect works

The usual SSH model puts a public key in the instance's `authorized_keys` once and
leaves it there forever, and the matching private key becomes a file that has to
be stored, shared, and eventually rotated. Instance Connect inverts that. You
generate a throwaway keypair on the spot, hand the public half to an AWS API, and
AWS writes it into the instance's `authorized_keys` on your behalf — where it stays
for **about sixty seconds** before being removed. You then SSH in the ordinary way
during that window.

Two consequences shape everything below:

- **There is no key to lose.** Authority comes from your AWS session, so access is
  granted and revoked in IAM, not by passing a `.pem` around. Someone leaving the
  project is handled by removing their AWS access.
- **Sixty seconds is not long enough to work in.** It is long enough to *establish*
  a connection — an open session survives the key's removal — but not long enough
  to be worth babysitting. The practical pattern is to push a fresh key before
  every command, which is what the helper script does. Do not try to keep one
  blessed session alive and reuse it.

## One-time setup

You need the AWS CLI v2 and an SSO profile for the account that owns the
instances. The profile is named `amprepo` throughout this document; if you call
yours something else, set `AWS_PROFILE_AMPREPO` and the script picks it up.

```console
$ aws configure sso
SSO session name: <anything>
SSO start URL:    <ask a maintainer — it is the organisation's AWS access portal>
SSO region:       us-west-2
Account:          <select the account holding the amprepo instances>
Role:             AdministratorAccess
Default client region: us-east-1
Profile name:     amprepo
```

Confirm it worked:

```console
$ aws sts get-caller-identity --profile amprepo
```

The SSO session expires — daily, typically. When it does, every command below
fails with `Token has expired and refresh failed`, and the fix is always the same:

```console
$ aws sso login --profile amprepo
```

That step opens a browser and cannot be automated or performed on your behalf. It
is the one manual gate in the whole process, and it is deliberate.

## The helper script

Save this as `amprepo-ssh.sh` somewhere on your `PATH` and `chmod +x` it.

```bash
#!/bin/bash
# amprepo-ssh.sh dev|prod [command...]
#
# Pushes a ~60s Instance Connect key and runs a command on the chosen server.
# Nothing is hardcoded: the instance id, availability zone, and public IP are all
# resolved from the Name tag at call time.
set -euo pipefail

PROFILE=${AWS_PROFILE_AMPREPO:-amprepo}
REGION=us-east-1
KEY=${HOME}/.ssh/amprepo-eic

case "${1:?usage: amprepo-ssh.sh dev|prod [command...]}" in
  dev)  NAME_TAG='DEV-amprepo-*' ;;
  prod) NAME_TAG='amprepo-graviton-PROD' ;;
  *)    echo "first argument must be dev or prod" >&2; exit 2 ;;
esac
shift

read -r ID AZ IP < <(aws ec2 describe-instances --profile "$PROFILE" --region "$REGION" \
  --filters "Name=tag:Name,Values=$NAME_TAG" "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].[InstanceId,Placement.AvailabilityZone,PublicIpAddress]' \
  --output text)

[ -f "$KEY" ] || ssh-keygen -t ed25519 -N '' -C amprepo-eic -f "$KEY" >/dev/null

aws ec2-instance-connect send-ssh-public-key --profile "$PROFILE" --region "$REGION" \
  --instance-id "$ID" --instance-os-user ubuntu --availability-zone "$AZ" \
  --ssh-public-key "file://${KEY}.pub" >/dev/null

exec ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o LogLevel=ERROR "ubuntu@$IP" "$@"
```

The keypair at `~/.ssh/amprepo-eic` is generated on first run and is not a
credential in the usual sense — it grants nothing on its own, since the public
half only reaches a server when your AWS session authorises the push. Deleting it
costs one `ssh-keygen` on the next run.

Two details worth knowing, because they are what makes the lookup necessary
rather than decorative:

- **The public IP changes.** These instances have no Elastic IP, so a stop/start
  moves them. Any address you write down will eventually be wrong, and a stale
  address usually presents as a connection timeout rather than an obvious error.
- **`send-ssh-public-key` requires the availability zone**, and dev and prod are
  in different ones. Resolving it from the same `describe-instances` call keeps
  the two servers from needing two divergent copies of the script.

`StrictHostKeyChecking=no` with `UserKnownHostsFile=/dev/null` is here because the
changing IP would otherwise trip the host-key warning on every reconnect. It is a
real, if small, weakening of the SSH trust model, accepted on the grounds that the
connection is to an address the AWS API just handed us.

## Usage

With no command, you get an interactive shell:

```console
$ amprepo-ssh.sh dev
```

With a command, it runs and exits — this is the form to prefer, since it keeps
each invocation inside its own key window:

```console
$ amprepo-ssh.sh dev 'cd /home/ubuntu/AmpliconRepository-dev && git rev-parse --abbrev-ref HEAD'
$ amprepo-ssh.sh prod 'docker ps --format "{{.Names}} {{.Status}}"'
```

Quote the whole command. It is passed to a remote shell, so unquoted pipes and
redirections are interpreted locally.

## Where things live on the servers

| | dev | prod |
|---|---|---|
| Name tag | `DEV-amprepo-graviton-testing` | `amprepo-graviton-PROD` |
| Checkout | `/home/ubuntu/AmpliconRepository-dev` | `/home/ubuntu/AmpliconRepository-prod` |
| Build checkout | — | `/home/ubuntu/ampRepo_for_docker_build/AmpliconRepository` |
| Container | `amplicon-dev` | `amplicon-prod` |
| Restart | `./stop-and-start-repo.sh` | `/home/ubuntu/stop-and-start-repo.sh` (or `./stop-server.sh` then `./start-server.sh`) |
| Tracks | a branch | a release tag |

The checkout is bind-mounted into the container, so a source-only change needs
only a restart. Rebuild the image only when `requirements.txt` or the `Dockerfile`
changed — and on prod, rebuild from the separate build checkout. See
[the deployment section of the README](../README.md#deploy) for the full release
procedure.

## Both servers restart themselves every day

**`12 7 * * * /home/ubuntu/stop-and-start-repo.sh`** is in `ubuntu`'s crontab on
prod. The site goes down and comes back at **07:12 UTC every day**. Measured
2026-08-31; it is not in any other document, and it was surprising to find.

**Dev does the same at 00:15 UTC**, via `15 0 * * *` and its own copy of the
same script. Measured 2026-09-01, and found the hard way: a 90-minute
maintenance job launched on dev at 23:46 UTC was killed 29 minutes in, having
deleted 17 of 70 documents. `docker inspect` showed `RestartCount=0` and
`OOMKilled=false` with a fresh `StartedAt`, which is the signature of a
deliberate `docker stop`/`docker start`, not a crash — the restart script stops
the container, so nothing increments the restart counter.

**The two windows are 7 hours apart, and neither host checks for running work.**
Before starting anything on either box that will take more than a few minutes,
check the clock against that host's window; if it does not fit, either wait or
run the job so it survives — `docker exec -d` writing to a log inside the
container is not enough on its own, because the restart stops the container and
takes the process with it.

**Why it is there — measured 2026-09-02.** It predates gunicorn. Until
2026-01-27 (`6a33a1c3`) `run-manage-py.sh` ran `manage.py runserver`: one
process, serving every request, for as long as the container lived, with
nothing to bound its memory. A daily restart was the only bound there was.
Prod's `stop-and-start-repo.sh` still carries the fossil of that era in its own
first line — a commented-out `docker exec ... stop-server.sh` above the note
"now just do a docker stop since we switched to gunicorn", dated 2026-02-03.
The restart was carried through the gunicorn migration rather than
re-justified. Related but not the same thing: containers were also given an
8 GiB cap and `unless-stopped` on 2026-08-25 after an unbounded container took
the host down.

**What now bounds worker memory instead.** `gunicorn_config.py` sets
`max_requests = 2000` with jitter, so every worker is replaced after roughly
2000 requests. This is not theoretical: prod's `gunicorn.log` for 2026-08-30 to
09-02 holds 108 "Autorestarting worker after current request" lines and exactly
108 "Worker exiting" lines — every non-restart worker replacement in those four
days was a clean recycle, with zero `WORKER TIMEOUT` and zero crashes. At
73k–151k requests/day that is 18–36 recycles/day across 9 workers — each worker
replaced 2–4 times a day, a **mean process age of 6–12 hours**, restart or no
restart. (That is arithmetic on the recycle rate, not a measured age
distribution; the probe records `age_s` per worker, so the distribution is
there to be read once a series spans a full day.) Removing the daily restart
would not slow recycling down — it would speed it up slightly, since each
restart currently discards every worker's partial progress toward its next
2000. The master
process is the exception: it is never recycled, and only the restart replaces
it. Measured on dev the same day, the master does not grow — flat at 111 MB RSS
/ 63 MB USS across an hour of sampling and three large analyses.

**What the restart is actually still doing.** Not curing a leak. A worker's
memory floor rises when it serves a heavy request and does not come back down.
Measured on dev 2026-09-02, driving three co-amplification analyses of a
2095-sample project through one worker: idle 257 MiB RSS → peak **1.99 GiB**
→ settles at **609 MiB** and stays there. The container went 1.78 GiB → 4.72 GiB
peak → 2.49 GiB, against an 8 GiB cap, for **one** worker analysing at a time.
That retained floor is what a restart clears; `max_requests` clears it too, but
only after 2000 more requests, which on a quiet dev box can be days.

**The failure that actually happened, and that the restart did not prevent.**
Prod's kernel journal (this boot, back to 2026-01-02) holds exactly one
container out-of-memory event: **2026-08-29 02:03:21 UTC**,
`constraint=CONSTRAINT_MEMCG` — the container's own 8 GiB limit, not the host —
killing a gunicorn worker (pid 3247682) that held **anon-rss 3,512,240 kB
(3.35 GiB)**. Dev's journal, covering 2026-02-05 onward, holds none. One event
in eight months is a tail risk, not a routine failure, and it is worth saying so
plainly. But its shape matters: a single worker at 3.35 GiB against an 8 GiB
ceiling is a **concurrent peak**, not gradual creep, and no restart schedule
reaches it — the box had restarted at 07:12 the previous morning. It is not in
`gunicorn.log`, which only reaches back to 2026-08-30 and contains no `SIGKILL`
and no `WORKER TIMEOUT` at all; that is why it had not been noticed. The cap
did its job — a killed worker is contained and gunicorn respawns it, which is
what the cap was added on 2026-08-25 to buy.

**Is 8 GiB the right cap?** Both boxes are `t4g.2xlarge`: 8 vCPU, 31 GB RAM, no
swap. Measured 2026-09-03: the app container is capped at 8 GiB and **neo4j at
18 GiB** — 26 GiB of committed caps against ~30.9 GiB of host, leaving ~5 GiB
for the OS and page cache. Live at that moment: prod app 2.19 GiB, prod neo4j
2.44 GiB; dev app 1.66 GiB, dev neo4j 12.51 GiB. So the container that has
actually been OOM-killed holds the smaller allowance, and on prod the larger
one is using an eighth of its share. Do not simply raise the app's number —
the caps are what stop one container taking the host down, and the total is
already committed. Collect a neo4j series with `memory_probe.py` (it samples
any process) and re-measure the split per host; dev and prod differ sharply
enough that the right number is unlikely to be the same on both.

**`/tmp` inside the container is disk, not RAM** — checked 2026-09-03 because
it matters for where scratch space is charged. `/tmp` is the overlay
filesystem (106 GB, 47 GB free), so the Django file cache
(`/tmp/django_cache`, 220 KB over 52 entries on dev), the throttle cache, and
every `tempfile.NamedTemporaryFile` in the tar and CSV-export paths cost disk
and not memory. The one RAM-backed mount is `/dev/shm` at Docker's default
**64 MB**, which `gunicorn_config.py` uses deliberately
(`worker_tmp_dir = "/dev/shm"`) for per-worker heartbeat files of a few bytes
each. 64 MB is ample for that; it would only bite if something in the stack
ever reached for bulk shared memory, and the symptom would look like a puzzling
I/O error rather than an out-of-memory one. (On a laptop `/tmp` is often tmpfs
— it is a 30 GB one on the maintainer's — so a harness writing multi-GB scratch
there charges it to memory and corrupts the measurement. Point `TMPDIR` at real
disk when running `leak_repro.py --scenario aggregate` locally.)

It is not a Python-level leak. Running the same analysis repeatedly in one
process, `tracemalloc` reports 0.0 MiB of tracked growth across 40,059
allocation sites while USS climbs — Python frees the objects and glibc keeps
the pages. `malloc_trim()` hands about 39 MiB per iteration back to the kernel,
which is the direct confirmation.

Re-aggregation, long the other suspect, is **not** a significant contributor:
re-aggregating the 1.7 GB / ~2000-sample PCAWG archive three times in one
process cost +69, +28 and +13 MiB — a decaying curve, 88 s per run. The
aggregator streams to disk and is frugal.

**Tooling.** `memory_probe.py` samples per-worker RSS/PSS/USS plus the cgroup
total, one row per process per sample, and is restart-proof (`--once` from
cron, appending to a bind-mounted CSV); `memory_probe.py --report` reads the
series back. `leak_repro.py` drives one suspect operation in a loop and
separates a leak from allocator retention. Both landed 2026-09-02, replacing
`memory_monitor.py`, `diagnose_memory.py`, `memory_tracker.py`,
`memory_profiler.py` and `install_profiling_tools.sh` — a stalled earlier
attempt that could not answer this question: it saved its samples only on
Ctrl-C, summed RSS across forked workers (which double-counts the shared image
— nine dev workers at ~255 MB RSS sum to 2.31 GB against a cgroup reading
1.65 GiB), and ran `objgraph`/`pympler` inside the monitoring process rather
than the target, with neither package in `requirements.txt`.
`docs/gunicorn-worker-concurrency-todo.md` covers the adjacent worker-model
question.

**What to keep in mind while it exists:**

- The two things below about background tasks apply to it, and nothing checks.
  **An import or edit still running at 07:12 is lost**, because prod's restart
  path does no task check — see the next section. That is a real hazard, not a
  theoretical one, and it is the strongest argument for eventually replacing the
  restart with a fix.
- Anything living only in a container's writable layer survives a restart and is
  lost on the next image rebuild, so a daily restart gives false confidence that
  a hand-applied fix is permanent.
- When reasoning about uptime, "the process has been up for N days" is never
  true on prod. Check `docker inspect --format '{{.State.StartedAt}}'` rather
  than assuming.

## Checking for background tasks before restarting

Restarting while a project import or edit is running will lose that work. Prod's
`stop-server.sh` does not check for you; dev's `stop-and-start-repo.sh` does, and
refuses to stop while tasks are running.

The obvious check — `manage.py shell` — has a side effect worth avoiding: on these
servers any `manage.py` invocation runs the app's startup hooks, which sweep
orphaned `tmp/` directories and kick off an S3 static sync. Query Mongo directly
instead. Note that `python` is not on the container's `PATH`; it must be the full
interpreter path:

```console
$ amprepo-ssh.sh prod 'docker exec amplicon-prod /opt/venv/bin/python -c "
import os, pymongo
db = pymongo.MongoClient(os.environ[\"DB_URI_SECRET\"])[os.environ[\"DB_NAME\"]]
print(\"running:\", db.background_tasks.count_documents({\"state\": \"running\"}))"'
```

The credentials are already in the container's environment, so nothing needs to be
supplied. `pymongo` prints a `UserWarning` about being connected to a DocumentDB
cluster — the database genuinely is DocumentDB, and the warning is informational,
not a failure. The line you want is the `running:` count.

## Verifying the site after a restart

Bare `curl` against the public URL returns **403**. This is not an outage — it is
the bot gating, which filters on user-agent, and curl's default announces itself
honestly. Three ways around it, in increasing order of effort:

```console
$ amprepo-ssh.sh dev 'curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/'
$ curl -s -o /dev/null -w "%{http_code}\n" https://dev.ampliconrepository.org/api/v1/projects/
$ curl -sI -A "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36" \
    https://dev.ampliconrepository.org/
```

The first bypasses the gate by originating on the host, behind the load balancer.
The second works because `/api/v1/projects/` is not gated, which makes it the
quickest liveness probe. The third is the one to use when you need the real page
and its headers.

And the logs, which is where a failed start actually explains itself:

```console
$ amprepo-ssh.sh dev 'tail -50 /home/ubuntu/AmpliconRepository-dev/logs/gunicorn_error.log'
```

## Troubleshooting

**`Token has expired and refresh failed`** — the SSO session lapsed. `aws sso
login --profile amprepo`. This is by far the most common failure and it can
surface from any of the AWS calls, including the `describe-instances` lookup.

**`Permission denied (publickey)`** — usually the sixty seconds elapsed between
the push and the connection. Just run it again. If it persists, check that the
`send-ssh-public-key` call is actually succeeding rather than being swallowed by
the `>/dev/null`.

**Connection times out** — the instance is stopped, or the security group does not
permit port 22 from your address. Confirm the instance is running and check what
IP was resolved:

```console
$ aws ec2 describe-instances --profile amprepo --region us-east-1 \
    --filters "Name=tag:Name,Values=amprepo-*" \
    --query 'Reservations[].Instances[].{Name:Tags[?Key==`Name`]|[0].Value,State:State.Name,IP:PublicIpAddress}' \
    --output table
```

**`AccessDenied` on `SendSSHPublicKey`** — your role lacks the permission for that
instance. This is an IAM change, not something the script can work around.

**SSM is not an alternative.** `aws ssm start-session` looks like it should work
and is the answer in a lot of AWS documentation, but no instance in this account
is registered with SSM — the agent is not running and there is no instance
profile granting it. It fails with `TargetNotConnected`, which reads like a
transient problem and is not one. Instance Connect is the path.
