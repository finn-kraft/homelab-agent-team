# Homelab Agent Team

`homelab-agent-team` is a persistent local software-development team. It is
designed for a durable goal such as:

> Continuously improve Align according to `docs/roadmap.md`.

PostgreSQL remembers work, Git preserves accepted progress, and
`agent-orchestrator run` keeps the team moving without a human invoking each
specialist command between steps.

## Responsibilities

| Component | Owns | Does not own |
| --- | --- | --- |
| Planner | What safe, concrete step happens next; semantic goal completion | Editing, committing, scheduling |
| EngineeringAgent | Inspecting a Work Package, implementing, testing, debugging, and recording commands/diff | Approval, commit, goal completion |
| Reviewer | Independent acceptance-criteria review | Silent fixes, merge, goal completion |
| Orchestrator | Deterministic next-state selection, leases, final verification, checkpoint hand-off | LLM planning or code reasoning |
| Routing Agent | Model/compute policy | Workflow state transitions |
| PostgreSQL | Durable jobs, steps, leases, events, reviews, commands, checkpoints | Source-code history |
| Git | Reviewer-approved local checkpoints | Automatic merge to `main` or deployment |

The Orchestrator is intentionally not another reasoning agent. Its normal
transition is deterministic:

```text
Planner → queued Work Package → EngineeringAgent → Reviewer
                               ├─ changes requested → same EngineeringAgent step
                               └─ approved → final verification → checkpoint
                                                        ↓
                                                     Planner again
```

One step completing never completes the high-level job by itself. After a
checkpoint, the job goes back to the Planner, which can choose the next
roadmap item or mark the overall goal complete with evidence.

## What is implemented

- A foreground `agent-orchestrator run` service and bounded
  `agent-orchestrator once` command.
- Reuse of the existing Planner, EngineeringAgent, Reviewer, PostgreSQL `jobs`, `steps`,
  `events`, `reviews`, `review_issues`, and `command_runs` contracts.
- Additive PostgreSQL migration for repository locks, verification runs,
  checkpoint recovery markers, model-routing audit records, and orchestration
  leases. No second orchestration database is created.
- Same-step revision cycles: a reviewer or verifier rejection returns the
  existing `step_id` to EngineeringAgent rather than creating a duplicate Planner step.
- Final deterministic verification after reviewer approval and before a
  commit. It runs `git diff --check`, scans candidate files for likely
  secrets, and runs only explicit allowlisted project verification commands.
- Controlled checkpoints: protected-branch refusal, exact reviewed file list,
  preservation of pre-existing human changes, no `git add .`, no force push,
  idempotent marker recovery, and persisted commit SHA.
- Ollama-first routing via the separate Routing Agent's new
  `POST /route/inference` API, with a safe local fallback if that service is
  down. OpenRouter is a configured backup, normally eligible only after
  repeated local failures (default fourth attempt).
- Pause, resume, cancel, inspection, events, structured logs, worker leases,
  and safe recovery of expired verification/checkpoint leases.
- Hard-bounded planner, Engineer, and reviewer prompts with truncated historical
  events, diffs, command output, and project documents. The planner can emit a
  small ordered package (up to `PLANNER_PACKAGE_STEPS`) so the team can execute
  several clear steps before another planning pass.
- Durable `phase_metrics` telemetry records phase duration, prompt size, model,
  provider, and crash status for operations and the Control Center.
- Worktree admission is crash-safe and periodically reconciles orphaned managed
  trees; per-repository concurrency, priority ordering, and optional queue
  backpressure prevent one repository from monopolizing the team.
- Prompt budgets support an approximate token ceiling and a context digest, while
  repository documents, diffs, command output, and prior events are explicitly
  delimited as untrusted evidence.
- Per-agent provider, capability, privacy, cloud-cost, and latency policies are
  enforced by the inference router. Backend breaker state persists across
  restarts and health probes are available without spending a model request.
- `agent-model-evals` runs fixed planner/Engineering/reviewer contract fixtures;
  verification also infers safe Python, Node, Rust, Go, or Make test commands
  when no project-specific Verification section exists.
- An authenticated, loopback-only Control Center backed by structured PostgreSQL APIs,
  including job controls, durable events, model routes, repository locks, Ollama model
  state, and optional trusted GPU telemetry.
- Root-level `transaction_manager.py`, `components.py`, and `plugins.py` compatibility
  primitives for optional integrations: explicit transaction boundaries, component
  lifecycle/health registration, and entry-point plugin discovery.

The full live Align integration and overnight soak test still require the
actual `/home/finn/work/align` checkout and PostgreSQL service. They are
operational validation steps, not silently claimed by this source package.

## Safety boundaries

- The dedicated worker branch is required. `main` and `master` are protected
  by default and never receive autonomous commits.
- The EngineeringAgent command policy rejects `git add`, `git commit`, `git push`, branch
  changes, history rewrites, and destructive Git cleanup. Checkpointing has
  separate narrow authority after review and verification.
- The checkpoint stages only the reviewer-approved files and refuses unknown,
  staged, or pre-existing human changes.
- No automatic merge, deployment, force push, or destructive production data
  operation is included.
- `needs_human` is used for consequential ambiguity, credentials, branch
  mismatch, destructive financial schema/data work, and unresolved recovery.
  `blocked` is used for technical/environment failures.
- Secrets stay in environment/secret management. Command environments and
  persisted output redact or omit common database, cloud, and token values.

## Install

Requirements: Python 3.11+, Git, PostgreSQL 15+, a local Ollama service, and
the existing Homelab Routing Agent if centralized model policy is desired.

```bash
cd /home/finn/homelab-ai/dev-team
python -m venv .venv
. .venv/bin/activate
pip install -e .
cp .env.example .env
chmod 600 .env
```

Edit `.env` through your normal secret-management process. It must contain a
restricted PostgreSQL application URL, repository allow-roots, and worker
identities. Do not use a database-owner account or commit `.env`.

The default model transport budget is intentionally fail-fast (`OLLAMA_RETRIES=0`
and a 60-second local timeout), with OpenRouter available as the immediate
availability fallback. Adjust `OLLAMA_TIMEOUT_SECONDS`, `OLLAMA_RETRIES`,
`OPENROUTER_TIMEOUT_SECONDS`, `OPENROUTER_RETRIES`, and
`OPENROUTER_CIRCUIT_SECONDS` for your hardware and network. If OpenRouter
returns an authentication or billing error, its tier is temporarily isolated
and the router walks back to another cloud tier or local Ollama instead of
surfacing a circuit-open failure to the workflow.

Apply the **additive** agent-team schema migration once with a migration-capable
database role:

```bash
set -a
. ./.env
set +a
agent-orchestrator init-db
```

The running service's role needs only the grants it actually uses on the
agent-team workflow tables. A migration role may have broader DDL authority;
it should not be the runtime identity.

## Create the Align job

Before creating it, make sure the real checkout already exists and is on the
dedicated branch. The agents refuse to switch branches themselves.

```bash
cd /home/finn/work/align
git switch agents/autonomous-align
git config user.name "Align Autonomous Agent"
git config user.email "agent@homelab.local"

cd /home/finn/homelab-ai/dev-team
set -a; . ./.env; set +a
agent-orchestrator create-job \
  "Continuously improve Align according to docs/roadmap.md." \
  --repository /home/finn/work/align \
  --branch agents/autonomous-align \
  --priority 10
```

Planner receives the repository's `AGENTS.md`, `WORKER.md` when present, and
`docs/roadmap.md` as read-only authoritative context. It may update the
roadmap only when materially implemented work changes a status; planning alone
does not count as completion.

## Run and operate it

For a transparent single transition:

```bash
agent-orchestrator once
agent-orchestrator status
agent-orchestrator inspect JOB_ID
```

For persistent autonomous operation:

```bash
agent-orchestrator run
```

`run` remains in the foreground, writes structured stdout/stderr logs, sleeps
when idle (default ten seconds), and handles `SIGTERM` at a safe boundary. Do
not simultaneously run `planner-agent run`, `engineering-agent run`, or
`reviewer-agent run` against the same jobs; those legacy independent workers
can bypass coordinator timing.

Controls act on durable state:

```bash
agent-orchestrator pause JOB_ID
agent-orchestrator resume JOB_ID
agent-orchestrator cancel JOB_ID
```

A paused or cancelled job is not newly claimed. Resuming reloads PostgreSQL
and Git evidence; no in-memory decision is trusted after a restart.

## Verification and checkpoints

After a reviewer approves a step, the Orchestrator runs:

1. `git diff --check` limited to the approved paths;
2. a local candidate secret scan; and
3. only explicit, allowlisted commands from `ORCHESTRATOR_VERIFICATION_COMMANDS`
   or backtick-delimited commands under `## Verification` in the repository's
   `WORKER.md` / `AGENTS.md`.

It never extracts arbitrary shell code from instructions or model output. A
failed check records bounded redacted evidence and returns the *same* step to
EngineeringAgent. No commit occurs.

On success, checkpointing verifies the expected worker branch, refuses
protected branches, requires a clean index, validates the exact approved file
set, checks for secrets again, stages only those paths, and makes a local
commit with a persistent `Autonomous-Step` marker. If the service crashes
after `git commit` but before PostgreSQL updates, it finds that marker rather
than committing twice. `AUTO_PUSH=false` is the safe default; never enable it
until the branch/push policy is independently reviewed.

## Inference routing

The specialist agents call the Routing Agent for model policy, not workflow
policy:

```text
POST http://127.0.0.1:8090/route/inference
```

The request carries caller, task type, attempt, complexity, and privacy data.
The response records provider, model, location, and rationale. If the Routing
Agent is unavailable, the agent continues with configured local Ollama. If
Ollama itself is unavailable, configured OpenRouter may be used as an
availability fallback; normal escalation remains configurable and local-first.
Model-route audit rows include caller, provider, model, reason, attempt,
latency, usage, fallback, and optional estimated cloud cost.

## Systemd

Copy and adapt [`deploy/agent-orchestrator.service.example`](deploy/agent-orchestrator.service.example), then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agent-orchestrator
sudo journalctl -u agent-orchestrator -f
```

The service intentionally has no interactive prompts and works as a foreground
process, so it is also suitable for a future Kubernetes Deployment. Durable
state is PostgreSQL plus Git; memory is disposable.

The Control Center has a separate example unit at
[`deploy/agent-control-center.service.example`](deploy/agent-control-center.service.example).
Set the Control Center password with `agent-control-center set-password`, keep it bound
to `127.0.0.1` behind an authenticated TLS reverse proxy (or bind to the server's
private LAN address and firewall port 8080):

```bash
agent-control-center
```

GPU data must come from a trusted, bearer-authenticated read-only JSON endpoint on the
Ollama host. The dashboard never accepts SSH details or arbitrary commands.

For a persistent full stack, install the routing, orchestrator, and Control Center units
plus [`deploy/agent-team.target.example`](deploy/agent-team.target.example). Then the
entire team follows one lifecycle command:

```bash
sudo systemctl enable --now agent-team.target
```

See [`docs/STATE_MACHINE_AUDIT.md`](docs/STATE_MACHINE_AUDIT.md) for transition
ownership, leases, recovery behavior, confirmed root causes, and remaining constraints.
Complete project-catalog, authentication, SSE, human-response, telemetry, and deployment
instructions are in [`docs/CONTROL_CENTER.md`](docs/CONTROL_CENTER.md).
The copy-ready install, startup, systemd, health-check, and update commands are in
[`docs/RUN_THE_TEAM.md`](docs/RUN_THE_TEAM.md).

## Configuration

See [`.env.example`](.env.example). The principal values are:

| Variable | Meaning |
| --- | --- |
| `DATABASE_URL` | Restricted PostgreSQL runtime connection |
| `ORCHESTRATOR_WORKER_ID` | Stable coordinator identity recorded in leases/events |
| `ORCHESTRATOR_POLL_SECONDS` | Idle sleep interval (default `10`) |
| `ORCHESTRATOR_LEASE_SECONDS` | Work-claim lease duration |
| `ROUTER_URL` | Homelab Routing Agent endpoint, default `http://127.0.0.1:8090` |
| `OLLAMA_URL` | Local Ollama endpoint |
| `OPENROUTER_API_KEY` | Optional backup only; never commit it |
| `INFERENCE_ESCALATE_AFTER` | First normal cloud-eligible attempt (default `4`) |
| `OLLAMA_TIMEOUT_SECONDS` / `OLLAMA_RETRIES` | Local request timeout and retry budget (defaults `60` / `0`) |
| `OPENROUTER_TIMEOUT_SECONDS` / `OPENROUTER_RETRIES` | Cloud request timeout and retry budget (defaults `90` / `1`) |
| `OPENROUTER_CIRCUIT_SECONDS` | Paid-backend breaker cooldown after authentication/billing failures (default `60`) |
| `ENGINEERING_MAX_CONTEXT_CHARS`, `PLANNER_MAX_CONTEXT_CHARS`, `REVIEWER_MAX_CONTEXT_CHARS` | Hard prompt character budgets |
| `ENGINEERING_MAX_PROMPT_TOKENS`, `PLANNER_MAX_PROMPT_TOKENS`, `REVIEWER_MAX_PROMPT_TOKENS` | Approximate token ceilings (`0` disables) |
| `PLANNER_PACKAGE_STEPS` | Maximum ordered steps emitted before replanning (default `3`) |
| `PLANNER_DECISION_RETRIES` | Short planning repair budget (default `1`; two total planning calls) |
| `ENGINEERING_TURN_LIMIT` | Optional compatibility cap; `0`/unset means progress-based Engineering with no overall turn cutoff |
| `ENGINEERING_MAX_STAGNATION_EPISODES` | Safety stop after repeated no-progress/escalation episodes (default `6`) |
| `PROTECTED_BRANCHES` | Comma-separated autonomous-commit deny list |
| `AUTO_COMMIT` | Enables reviewer-approved local checkpoints |
| `AUTO_PUSH` | Off by default; no force/history rewrite is ever permitted |
| `MISSION_PACKAGE_LIMIT` | Maximum unchecked roadmap items materialized per mission pass (default `3`) |
| `ORCHESTRATOR_MAX_CONCURRENT_PER_REPOSITORY` | Active Engineering jobs allowed per source repository (default `1`) |
| `ORCHESTRATOR_QUEUE_BACKPRESSURE` | Maximum ready package backlog (`0` disables) |
| `ORCHESTRATOR_WORKTREE_CLEANUP_SECONDS` | Managed worktree reconciliation interval (default `60`) |
| `*_ALLOWED_PROVIDERS`, `*_REQUIRED_CAPABILITIES` | Per-agent routing allowlists and capability requirements |
| `*_MAX_CLOUD_COST`, `*_MAX_LATENCY_SECONDS` | Per-agent cloud spend and recent-latency guardrails (`0` disables) |
| `AUTO_INTEGRATE` | Opt-in integration of verified package commits into a non-protected mission branch |

V2 operations are also available from the orchestrator CLI: `missions`,
`expand-mission`, `packages`, `human-queue`, `answer-human`, and
`integrate-package`. The Control Center exposes the same mission/package and human
queue read models after running the current additive migration (recorded in the
database as `orchestrator-0011`). Operator actions are tracked with durable
operation IDs and can be inspected from the Control Center job detail view.
The dashboard's `/api/telemetry` path is independent of the workflow stream and polls
Ollama/GPU data once per second, including loaded and installed model details.

## Tests

Run deterministic tests without a live LLM or production database:

```bash
pip install -e ".[test]"
pytest -q
```

Run the fixed model contract fixtures with:

```bash
agent-model-evals
```

The tests mock specialist agents/routing decisions and use temporary Git
repositories for checkpoint safety. Before enabling an unattended job, run a
bounded real integration against `/home/finn/work/align` on
`agents/autonomous-align` and prove at least two consecutive roadmap steps:

```text
Planner → EngineeringAgent → Reviewer → verification → checkpoint → Planner → second step
```

Never run that integration on `main`, and stop on `needs_human` rather than
trying to automate a missing decision or production-data approval.
