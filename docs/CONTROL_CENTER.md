# Agent Team Control Center

The Control Center is a human-friendly operations layer over the existing Orchestrator
and PostgreSQL state. Closing the browser never stops work. It does not execute shell,
SQL, or arbitrary Git commands.

## Configure authorized projects

Set a long random `CONTROL_CENTER_TOKEN` through the host secret mechanism. Configure
the repository allowlist as JSON on one line:

```text
CONTROL_CENTER_PROJECTS='[{"id":"align","name":"Align","repository":"/home/finn/work/align","branch":"agents/autonomous-align","roadmap":"docs/roadmap.md"}]'
```

Only catalogued project IDs can create jobs. Repository paths and branches are selected
server-side; clients cannot submit an arbitrary path. The service needs read-only
filesystem access to those checkouts for Git status, branch, roadmap presence, and the
latest commit. Workflow mutations remain PostgreSQL records consumed by Orchestrator.

## Run

```bash
set -a; . ./.env; set +a
agent-orchestrator init-db
agent-control-center --host 127.0.0.1 --port 8080
```

Install `deploy/agent-control-center.service.example` for persistent operation. Keep
the listener on loopback and publish it only through an authenticated TLS reverse proxy.
The page asks for the bearer token and keeps it only in page memory.

`GET /health` is an unauthenticated readiness endpoint exposing only `ok` or `degraded`.
All `/api/*` endpoints require the bearer token. Cancel requires explicit confirmation.

## Live state

The browser uses an authenticated streaming `fetch` to `/api/stream`. The server emits
structured snapshots every five seconds using Server-Sent Events. Reconnects read fresh
PostgreSQL state; terminal output is never scraped. Orchestrator writes a durable worker
heartbeat so online/offline is objective.

The job page combines steps with reviews, verification runs, safe command metadata,
model routes, and checkpoint records. It shows the same-step `changes_requested` loop as
a revision, not a failed job. Human answers are accepted only for `needs_human`, appended
to durable job notes/events, and return the existing step to Coder when appropriate.

## GPU and Ollama telemetry

Ollama model allocation comes from its read-only `/api/ps`. Routing readiness comes
from `/health`. Optional GPU telemetry must be a narrowly scoped bearer-authenticated
JSON endpoint configured as `GPU_TELEMETRY_URL`; expected fields are:

```json
{"status":"online","name":"GTX 1070","utilization_percent":72,
 "vram_used_mb":5939,"vram_total_mb":8192,"temperature_c":61,"power_w":112,
 "context_length":8192}
```

Do not point this setting at SSH, a command runner, or a general host-management API.

For a complete install-to-boot command sequence, including the routing service and the
single `agent-team.target` lifecycle, see [`RUN_THE_TEAM.md`](RUN_THE_TEAM.md).
