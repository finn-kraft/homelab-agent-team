# coder-agent

`coder-agent` is a persistent implementation worker designed to participate in a
planner → coder → reviewer → test loop on repositories hosted on your own machine.
It consumes structured steps from PostgreSQL, works only inside explicitly allowed
Git repositories, records every model action and command result, and hands a diff to
an independent reviewer. It does **not** mark its own work complete or silently push.

## What this MVP includes

- Atomic PostgreSQL job claiming with worker leases and crash recovery
- Persistent attempts, events, command output, model identity, diffs, and review state
- Repository allow-roots plus `..` and symlink-escape protection
- A command allowlist, no shell interpretation, timeouts, and captured exit status
- Protection for pre-existing human changes
- Ollama by default, with optional OpenRouter escalation on later attempts
- Structured JSON actions rather than free-form tool execution
- A successful-command gate and likely-secret scan before review
- `WORKER.md`/`AGENTS.md` repository instructions loaded for every step
- Retry progression to `needs_human` instead of an endless loop

Branch creation, pushing, PR creation, human approvals, planning, and independent
review are deliberately separate services. This keeps the coder's authority narrow.

## Quick start

Requirements: Python 3.11+, Git, Docker (for the included PostgreSQL setup), and
Ollama or an OpenRouter key.

```bash
docker compose up -d
python -m venv .venv
. .venv/bin/activate
pip install -e .
cp .env.example .env
```

Export the values from `.env`, then initialize the database:

```bash
coder-agent init-db
```

Create a job and a concrete implementation step. The repository must already be on
the requested dedicated worker branch; branch switching is intentionally outside the
model's authority.

```sql
INSERT INTO jobs (goal, repository, branch, priority)
VALUES ('Add a health endpoint', '/srv/repos/my-project',
        'worker/health-endpoint', 10)
RETURNING id;

INSERT INTO steps (
  job_id, sequence, repository, branch, title, objective, rationale,
  acceptance_criteria, constraints
) VALUES (
  1,
  1,
  '/srv/repos/my-project',
  'worker/health-endpoint',
  'Implement the health endpoint',
  'Implement the HTTP health endpoint',
  'Provide a bounded, testable service entry point',
  '["GET /health returns 200", "existing tests pass"]',
  '["Do not change authentication behavior", "Do not hard-code credentials"]'
);
```

Run one claim for easy inspection, or start the long-running worker:

```bash
coder-agent run-once --json
coder-agent run
```

When implementation and verification succeed, the step becomes `review`. A reviewer
should inspect the actual diff and update the step to either:

```sql
UPDATE steps
SET status = 'changes_requested',
    reviewer_feedback = '{
      "verdict":"changes_requested",
      "issues":[{
        "severity":"high",
        "file":"src/api.py",
        "problem":"invalid input is accepted",
        "requested_change":"return 400 for malformed input"
      }]
    }'
WHERE id = 1;
```

or, after tests and review, create a small commit from the explicit `files_changed`
list and set the step to `complete` with its SHA. That final checkpoint belongs to an
orchestrator or reviewer with separately approved Git authority.

## Configuration

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `CODER_WORKSPACES` | Allowed repository roots, separated by the OS path separator |
| `CODER_WORKER_ID` | Stable identity used for leases and events |
| `CODER_MODEL` | Ollama coding model |
| `OLLAMA_URL` | Ollama server URL |
| `OPENROUTER_API_KEY` | Enables optional cloud escalation; never stored in job data |
| `OPENROUTER_MODEL` | Cloud model selected after repeated attempts |

The agent strips common credentials from subprocess environments and redacts likely
credentials from recorded output. For production, inject secrets through your secret
manager and isolate the worker further with a dedicated OS user or container.

## State transitions

```text
queued ──claim──> running ──implementation+verification──> review
                    │                                      │
                    ├──recoverable failure──> failed        ├──approved──> complete
                    ├──genuine blocker─────> blocked       └──feedback──> changes_requested
                    └──retry ceiling───────> needs_human                    │
                                                                            └──claim──> running
```

Only `queued` and `changes_requested` are automatically claimable. Pausing the job
prevents new claims. Expired running leases are visible for an orchestrator to recover;
they are not concurrently reclaimed while still marked running.

## Production hardening still recommended

- Run each worker in its own Git worktree to eliminate checkout contention.
- Add an approval service for branch creation, pushes, deletes, and migrations.
- Add a separate reviewer worker and an orchestrator that commits only approved diffs.
- Replace regex-only secret detection with a dedicated scanner such as Gitleaks.
- Apply container-level network, CPU, memory, and filesystem restrictions.
- Add repository locks keyed by canonical checkout path if checkouts are shared.

Run the local safety tests with `pytest -q`.
