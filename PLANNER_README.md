# planner-agent

`planner-agent` is the read-only strategic worker in the Homelab autonomous
development team. It repeatedly answers: where is the project now, what remains, and
what is the highest-value safe step that produces observable evidence?

It does not edit source files, execute development commands, commit, push, review code,
or schedule workers. Those responsibilities remain with coder-agent, reviewer-agent,
and the orchestrator. The boundary is enforced by exposing only a fixed, read-only
repository inspector to the planner.

## Architecture

```text
Orchestrator → planner-agent → PostgreSQL step → coder-agent
     ↑                                              ↓
     └──────── approved checkpoint ← reviewer ← diff/tests
                         │
                         └──────────→ planner-agent reassesses the overall goal
```

`agent_core` contains the shared PostgreSQL schema, status values, task contracts,
events, review contracts, and Ollama/OpenRouter routing. Both current agents consume
the same `jobs`, `steps`, `events`, and `command_runs` tables.

## Setup

Requirements are Python 3.11+, Git, PostgreSQL, and Ollama. Docker Compose provides a
local PostgreSQL 16 instance.

```bash
docker compose up -d
python -m venv .venv
. .venv/bin/activate
pip install -e .
cp .env.example .env
```

Set `DATABASE_URL` and `PLANNER_ALLOWED_REPOSITORIES`. Allowed repositories is an
OS-path-separated list of directories under which jobs may point. Configure Ollama:

```text
OLLAMA_URL=http://localhost:11434
PLANNER_MODEL=qwen2.5-coder:14b
```

Optionally set `OPENROUTER_API_KEY` and `OPENROUTER_PLANNER_MODEL`. The planner starts
locally and escalates after `PLANNER_ESCALATE_AFTER` failed attempts. Model, provider,
latency, usage, and escalation events are recorded with decisions.

Initialize the shared schema:

```bash
planner-agent init-db
```

## CLI

```bash
planner-agent create-job \
  "Continue implementing Align's migration roadmap" \
  --repository /srv/repos/align \
  --branch worker/align-migration \
  --priority 10

planner-agent once
planner-agent run
planner-agent inspect 1
planner-agent events 1
planner-agent pause 1
planner-agent resume 1
planner-agent cancel 1
```

`run` is the normal persistent mode. Planning jobs are acquired using row locks,
`SKIP LOCKED`, and expiring leases, so two planners cannot plan the same job at once.
State and events survive worker restarts. Resume always triggers a fresh database and
repository assessment.

## Job and step lifecycle

A pending job is claimed for planning only when it has no active implementation or
review step. The planner reads bounded repository evidence, project instructions,
roadmap, history, dirty state, completed steps, failures, test results, and reviewer
feedback. It then emits one validated JSON decision.

```text
pending → planning → running → coder → review → approved checkpoint
             ↑                                      │
             └──────────── reassess next step ──────┘
```

Reviewer `changes_requested` keeps the same logical step active and sends it back to
coder-agent; planner-agent does not manufacture a duplicate. Failed steps can be
retried, replaced with a genuinely different objective, or escalated. Pause and cancel
states cannot be claimed.

Overall `complete` is accepted only when there are multiple evidence items and every
recorded step has an approved reviewer verdict plus a resulting commit. This prevents
one successful coding step from being confused with the high-level goal.

## Example autonomous progression

For “Continue implementing Align's migration roadmap,” the first assessment might
create a bounded API-foundation step. After coder-agent changes it, tests pass,
reviewer approves it, and the orchestrator records a checkpoint, planner-agent runs
again. It rereads current state and may then create the next UI or integration step.
This repeats until repository evidence—not the plan itself—proves the original goal.

## Recovery and observability

Every planning claim and decision is append-only in `events`. Important event types
include `job_planning_started`, `step_created`, `model_escalated`, `job_needs_human`,
`job_blocked`, and `job_complete`. Jobs store the current step, phase, iteration count,
worker identity, and lease expiration for dashboard use.

If a worker dies, PostgreSQL retains its state. Another planner may claim the job after
the lease expires, reload current evidence, and reassess. It never trusts an old
in-memory plan.

## Tests

```bash
pytest -q
```

The deterministic suite uses mocked model output and covers first-step creation,
multi-step continuation, review reuse, failures and escalation, pause/resume/cancel,
restart and lease behavior, evidence-gated completion, safety-boundary validation,
roadmap refresh, dirty worktrees, and read-only repository inspection.

The requested live Align demonstration is intentionally not automatic: it needs the
actual Align checkout on a safe worker branch plus running PostgreSQL and Ollama. Once
those are supplied, the bounded goal can be created with the CLI above; no merge to
main is performed by planner-agent.

