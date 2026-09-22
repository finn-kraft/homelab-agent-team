# Agent Team Control Center

The Control Center is a human-friendly operations layer over the existing Orchestrator
and PostgreSQL state. Closing the browser never stops work. It does not execute shell,
SQL, or arbitrary Git commands.

## Configure authorized projects

Configure the repository allowlist as JSON on one line. The Control Center password is
stored as a salted bcrypt hash in PostgreSQL, not in `.env` or source code. Set it once
or change it safely with:

```text
CONTROL_CENTER_PROJECTS='[{"id":"align","name":"Align","repository":"/home/finn/work/align","branch":"agents/autonomous-align","roadmap":"docs/roadmap.md"}]'
```

```bash
set -a; . ./.env; set +a
.venv/bin/agent-control-center set-password
```

Only catalogued project IDs can create jobs. Repository paths and branches are selected
server-side; clients cannot submit an arbitrary path. The service needs read-only
filesystem access to those checkouts for Git status, branch, roadmap presence, and the
latest commit. Workflow mutations remain PostgreSQL records consumed by Orchestrator.

## Run

```bash
set -a; . ./.env; set +a
agent-orchestrator init-db
agent-control-center
```

Install `deploy/agent-control-center.service.example` for persistent operation. Keep
the listener on loopback behind an authenticated TLS reverse proxy, or bind it to the
server's actual private LAN address and firewall port 8080 to your trusted network.
The page establishes an opaque HttpOnly SameSite session; the password is never sent
again after login.

`GET /health` is an unauthenticated readiness endpoint exposing only `ok` or `degraded`.
All `/api/*` endpoints require the HttpOnly session cookie and CSRF header for writes.
Sessions have configurable absolute and idle expiry (`CONTROL_CENTER_SESSION_TTL_SECONDS`
and `CONTROL_CENTER_SESSION_IDLE_SECONDS`), bind to the client address, and expose a
role (`admin`, `operator`, or read-only `viewer`) through `/api/session`. Authenticated
writes are rate limited per session; a `429` response includes `Retry-After`.
Cancel and Remove from queue require explicit confirmation. Remove is available for
any job that is not already cancelled: it stops new claims, releases active leases,
clears stale blockers and open human requests, and keeps the job's audit history while
removing it from the active queue.

## Live state

The browser uses an authenticated streaming `fetch` to `/api/stream`. The server emits
workflow snapshots every five seconds using Server-Sent Events, with monotonically
increasing event IDs and `Last-Event-ID` replay of missed workflow events. GPU and Ollama telemetry
has its own authenticated `/api/telemetry` endpoint and is polled by the dashboard every
second, so fast-changing utilization, temperature, power, VRAM, model allocation, and
Ollama process data do not wait for the slower workflow refresh. Reconnects read fresh
PostgreSQL state; terminal output is never scraped. Orchestrator writes a durable worker
heartbeat so online/offline is objective.

Telemetry samples are persisted at one-second resolution and are available at
`/api/telemetry/history`. The dashboard renders recent GPU utilization and temperature
history and raises alerts for high temperature, capacity utilization, unavailable
inference, and stale workflow leases. The read-only `/api/security/audit` endpoint
scans recent event, command, and operator records for credential patterns without
returning matched values.

Mission details include per-package completion evidence. A package remains
“roadmap pending” until its independent review is approved, verification passes,
the checkpoint has a commit, and that commit is integrated into the mission branch.
Integration then commits the exact referenced roadmap checkbox and refreshes the
mission status from the durable evidence.

The Mission detail screen also visualizes package prerequisites from the existing
`work_packages.dependencies` values. It does not maintain a separate graph. Operators
can Pause, Resume, or Cancel the mission from the same screen. These writes use
`POST /api/missions/<id>/<pause|resume|cancel>` and produce a durable control operation
with append-only submitted, accepted, applied, rejected, or failed history.

Pause changes the mission and its active jobs to paused immediately, so no new package,
Engineering, Review, Verification, or Checkpoint claim can begin. An Engineering
command already running receives the existing cooperative stop signal and returns at a
safe boundary. An atomic Review, Verification, or Checkpoint already underway may
finish persisting its evidence, but automatic mission integration will not start while
paused. Resume restores paused jobs to their existing durable phase; blocked packages
are requeued only through the same bounded recovery rule used by job Resume.

Cancel is idempotent and requires confirmation. It marks unfinished packages and jobs
cancelled without deleting sessions, commands, reviews, verification runs, checkpoint
records, events, or repository changes. A live phase retains its lease until it safely
returns; after release or expiry, the coordinator finalizes its Step as cancelled.
Repository locks are never force-deleted while live.

## Human decision history

`GET /api/human-queue?status=open` returns current requests. Use `status=resolved`
for answered/cancelled history or `status=all` for the combined view; `limit` is bounded
server-side. The Missions screen presents current and resolved requests separately and
expands each request into its append-only lifecycle.

`human_queue` is the current projection and retains its original question/context.
`human_queue_events` records creation, later evidence, ownership, answer, resolution,
and every outcome. A repeated Planner, Reviewer, or Integration gate appends
`evidence_observed` rather than replacing the original evidence. Answering atomically
records the owner, redacted answer, resolution, workflow-resume outcome, and existing
job/step transition. Cancelling a job or mission resolves its open requests but never
deletes their history. Migration `orchestrator-0014` backfills lifecycle history for
older request rows and includes both tables in schema readiness.

## Mission metrics

Mission detail includes a `metrics` object and renders the same evidence as progress,
quality, Verification, latency, model usage, and cost cards. No mutable metric counters
are stored. Refresh/restart recomputes from Work Packages, Reviews, Verification runs,
Checkpoint runs, Integrations, phase telemetry, and model invocations.

Metric semantics are explicit in the API response:

- The delivery denominator is every non-cancelled package. Blocked and failed packages
  remain included; cancelled packages are reported separately.
- Evidence completion requires package completion plus approved Review, passed
  Verification, completed Checkpoint with commit, and completed Integration with commit.
- A retried package remains one denominator item. Review attempts, requested changes,
  failed/blocked Verification attempts, and failed Checkpoints contribute to quality and
  revision signals.
- First-pass Review and Verification percentages use packages with at least one durable
  attempt as their denominator.
- Active mission elapsed time uses PostgreSQL `now()`; complete/cancelled mission elapsed
  time ends at its durable `updated_at`. Package delivery latency is reported only when
  Integration has a completion timestamp.
- Token totals accept the durable provider's input/output or prompt/completion naming.
  Cloud cost is the sum of persisted `estimated_cloud_cost` values.

The job page combines steps with reviews, verification runs, safe command metadata,
model routes, and checkpoint records. It shows the same-step `changes_requested` loop as
a revision, not a failed job. Human answers are accepted only for `needs_human`, appended
to durable job notes/events, and return the existing step to EngineeringAgent when appropriate.

## GPU and Ollama telemetry

Ollama model allocation comes from its read-only `/api/ps`; installed model names come
from `/api/tags` and the server version from `/api/version`. The API response preserves
both loaded and installed model records for inspection. Routing readiness comes
from `/health`. Optional GPU telemetry must be a narrowly scoped bearer-authenticated
JSON endpoint configured as `GPU_TELEMETRY_URL`; expected fields are:

```json
{"status":"online","name":"GTX 1070","utilization_percent":72,
 "vram_used_mb":5939,"vram_total_mb":8192,"temperature_c":61,"power_w":112,
 "context_length":8192}
```

Do not point this setting at SSH, a command runner, or a general host-management API.
If `GPU_TELEMETRY_URL` is unset, a fixed local `nvidia-smi` query is used when present.

For a complete install-to-boot command sequence, including the routing service and the
single `agent-team.target` lifecycle, see [`RUN_THE_TEAM.md`](RUN_THE_TEAM.md).

## Production exposure

Set `CONTROL_CENTER_COOKIE_SECURE=true` only when serving the dashboard over HTTPS;
the service then emits HSTS and marks the session cookie `Secure`. Keep the listener
on loopback behind a TLS reverse proxy where possible, or firewall a LAN binding to
trusted operator addresses. The built-in server is not a replacement for a mature
identity provider in multi-user deployments; use a reverse proxy and
`CONTROL_CENTER_ROLE=viewer` for read-only access.
