# V2 Autonomous Engineering Architecture

V2 is developed alongside the proven V1 state machine until its acceptance
tests pass. V1 remains the rollback path; no existing jobs or tables are
rewritten by the initial migration.

## Execution invariant

The durable execution unit is a Work Package. An Engineering Worker owns the
package from repository inspection through implementation, testing, debugging,
and review preparation. Reviewer, deterministic Verification, Checkpoint, and
Integration remain independent authorities. There is no Planner-to-Coder
implementation handoff in V2.

## Durable hierarchy

`missions` own a bounded rolling-wave decomposition. `work_packages` reference
one mission, objective, acceptance criteria, constraints, roadmap evidence,
dependencies, repository, branch, worktree, and starting commit.
`engineering_sessions` resume a package after process death and record model
tier, problem episode, progress, stagnation, turn count, lease, and last known
test result. `engineering_actions` provide an append-only operational trace.

## State ownership

- Mission Manager owns package creation and dependency scheduling.
- Engineering Worker owns package implementation and candidate readiness.
- Reviewer owns only the approval/changes-requested verdict.
- Verification owns deterministic safety and test results.
- Checkpoint owns Git commits; Integration owns mission-branch merges.
- Human Queue is reserved for credentials, irreversible operations, and
  unresolved product choices.

## Delivery sequence

1. Engineering Worker with direct Reviewer revision loop.
2. Verification and checkpoint handoff.
3. Progress/stagnation escalation and circuit breakers.
4. Durable crash recovery and isolated worktrees.
5. Mission persistence, dependency-aware scheduling, and Human Queue.
6. Control Center mission UX, concurrency, and Integration Manager.

Every stage requires an isolated deterministic test before the next stage is
enabled. V2 will not be selected by default until a disposable repository
produces multiple reviewed and verified commits with no manual agent calls.

## Current implementation status

The mission boundary is now operational: `MissionManager` parses unchecked
roadmap items into an idempotent rolling wave, the coordinator materializes the
next wave before claiming work, and `IntegrationManager` can merge verified
package commits into a non-protected mission branch using a detached temporary
worktree. The `human_queue` and `mission_integrations` tables are included in
`orchestrator-0005`, with CLI and Control Center read/write access.

Mission controls are an additive projection over that state machine. Dependency
visibility is calculated from `work_packages.dependencies`; Pause/Resume/Cancel update
the mission plus linked jobs/packages transactionally and emit per-job events. In-flight
phase leases and repository locks remain authoritative until their safe boundary or
expiry. `control_operation_events` is the append-only audit of every operation lifecycle
(migration `orchestrator-0013`).

Human Queue history is split into a current projection (`human_queue`) and an
append-only lifecycle (`human_queue_events`). Creation evidence is immutable;
subsequent observations, ownership, answers, resolution, and outcomes are appended.
The existing job/step human-gate transitions remain authoritative. Shared recursive
redaction is applied before untrusted model, repository, or operator content reaches
either record (migration `orchestrator-0014`).

Mission metrics are a read model, not workflow state. Package progress uses the same
independent completion evidence as roadmap gating. Quality/revision counts come from
Review, Verification, and Checkpoint attempts; latency comes from mission/package
timestamps and `phase_metrics`; model/provider/token/cost totals come from
`llm_invocations`. Cancelled packages are excluded from the delivery denominator,
while blocked/failed packages remain included and retries remain one package.

`AUTO_INTEGRATE` remains opt-in. The remaining acceptance gate is live: run one
coordinator against a disposable repository and observe at least three packages,
including one deliberate Reviewer revision, reach verified checkpoints without
manual `once` calls. That requires the operator's PostgreSQL, Ollama, repository,
and service environment and is not reproducible inside the unit-test sandbox.
