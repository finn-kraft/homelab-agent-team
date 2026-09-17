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
