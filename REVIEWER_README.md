# reviewer-agent

`reviewer-agent` is the independent acceptance gate in the local Planner → Coder →
Reviewer team. It reviews the actual task-owned Git diff, recorded commands, tests,
project instructions, security checks, and every acceptance criterion. It never edits,
commits, pushes, merges, or declares the overall job complete.

## Setup and operation

Use the shared PostgreSQL service and install the combined package:

```bash
docker compose up -d
python -m venv .venv
. .venv/bin/activate
pip install -e .
cp .env.example .env
reviewer-agent init-db
reviewer-agent run
```

Set `DATABASE_URL`, `REVIEWER_ALLOWED_REPOSITORIES`, `OLLAMA_URL`, and
`REVIEWER_MODEL`. OpenRouter is optional and is selected for repeated reviews, large
diffs, migrations, or financial-code changes.

Useful commands:

```bash
reviewer-agent once
reviewer-agent inspect 12
reviewer-agent inspect-step 17
reviewer-agent issues 17
```

## Evidence and safety

The worker claims only steps in `review`, using PostgreSQL row locks and expiring
leases. Paused and cancelled jobs are excluded. It scopes the diff to Coder's recorded
`files_changed`, runs fixed `git diff --check`, consumes recorded test commands, scans
the diff for likely secrets, detects dependency/migration/financial/test-deletion risk,
and loads bounded project instructions.

An LLM cannot override deterministic failures. Failing commands, malformed diffs, or
secret findings produce `changes_requested`. Invalid model JSON can never approve.
Every criterion must have a recognized status and evidence. Approval requires no
blocking issues and routes the same step to `verification`—never merge. Rejection sends
the same `step_id` to `changes_requested` for Coder revision.

Reviews and issues are persistent. Stable issue keys are derived from issue identity;
fixed issues are recorded as resolved and genuine later issues can be added. Model,
provider, usage, latency, diff hash, deterministic checks, matrix, verdict, and events
remain auditable in PostgreSQL.

Run `pytest -q` for the deterministic suite. Live Align integration requires its real
checkout on a safe worker branch plus PostgreSQL and Ollama; reviewer-agent never
merges or pushes that branch.

