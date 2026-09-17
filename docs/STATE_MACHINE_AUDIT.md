# State-machine and reliability audit

The Orchestrator—not an LLM—owns workflow transitions. PostgreSQL is authoritative.

```text
job pending/running → Planner → step queued → Coder → review → Reviewer
                                      ↑                    ├─ changes_requested
                                      │                    └─ approved → verification
                                      │                                      ├─ failed ─┘
                                      │                                      └─ passed → checkpoint
                                      └──────── Planner ← step complete ← Git commit
```

| Durable state | Claiming component | Next-state owner |
|---|---|---|
| job `pending`, `planning`, `running` without active step | Planner | Planner |
| step `queued`, `changes_requested` | Orchestrator/Coder | Coder handoff |
| step `review` | Orchestrator/Reviewer | Reviewer verdict |
| step `verification` | Orchestrator | deterministic verifier |
| step `checkpoint` | Orchestrator | checkpoint service |
| expired `running`, `verification`, `checkpoint` | Orchestrator | recovery policy |
| job `paused`, `cancelled`, `complete` | not claimable | explicit control/terminal |

Planner uses a job lease; Coder, Reviewer, verification, and checkpoint use step leases.
Repository mutation is serialized by canonical-path `repository_locks` with heartbeats.
Checkpoint markers recover a commit made immediately before process death without making
a duplicate commit. Coder crash recovery requeues only a clean checkout; unclassified
working-tree changes block safely rather than overwriting human work.

## Root causes confirmed

1. Planner selected one backend before all structured-output repairs, so repairs could
   not reach the configured fourth-attempt escalation.
2. Exhausted malformed output and backend failures became permanent `blocked` jobs.
3. `needs_human` required a question but did not require a genuine hard gate.
4. Verification assumed a `python` executable even when the running interpreter was
   available only as `python3`.
5. Durable state existed but operators lacked a structured control/telemetry UI.

The repair loop now reroutes every attempt, schedules bounded durable retries instead of
terminal blocking, and validates hard-gate reasons. The end-to-end test runs the actual
Orchestrator, verifier, checkpoint service, subprocess tests, and Git, producing two
distinct approved commits. Specialist reasoning is deterministic in that test so it
does not depend on external model availability.

`reviewing` remains a legacy job status with ambiguous value because a `review` step is
already canonical. It should be removed only through a separately versioned migration.
Parallel jobs should eventually use separate Git worktrees; current repository locks
correctly serialize a shared checkout but reduce concurrency.
