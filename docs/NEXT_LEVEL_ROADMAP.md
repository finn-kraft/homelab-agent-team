# Homelab Agent Team — Next-Level Roadmap

This is the prioritized follow-on plan after the V2 foundation. The project
already has durable PostgreSQL workflow state, an EngineeringAgent-compatible
implementation loop, independent review, deterministic verification and
checkpointing, mission/work-package primitives, fast Ollama telemetry, and an
authenticated Control Center.

The most important remaining proof is a live unattended run against PostgreSQL,
Ollama, Git, and systemd that completes several real packages and recovers from
deliberate failures.

## Current implementation milestone

The first five reliability slices are now implemented alongside V1:

- Durable job phases are updated when Planner decisions are applied, and stale
  planner leases are released with an audit event.
- The automated Git acceptance fixture now runs three packages consecutively,
  exercises a Reviewer revision on the first package, and creates a distinct
  checkpoint commit for each package.
- EngineeringAgent command execution polls at safe boundaries, terminates the
  complete process group on pause/cancel, and persists `cancelled` evidence in
  `command_runs` (migration `orchestrator-0009`). The follow-on prompt telemetry
  and context-digest columns are included in migration `orchestrator-0010`.
- `engineering-agent` is the canonical installed entry point and durable event
  identity; the old Coder names remain only as compatibility shims during the
  migration.

The remaining part of item 2 is the operator-run soak against the real
PostgreSQL/Ollama/systemd deployment. The fixture proves the transition logic
without pretending that a sandbox run is a production-host acceptance test.

The next five slices (items 6–10) are now implemented as well: schema readiness
is exposed by `/health`, Engineering is the canonical durable hand-off with V1
aliases, sessions restore their last action/model/tier/test state, reviewer
revisions emit a same-session hand-off, and stagnation tracks semantic worktree
changes, repeated failures, missing verification, and action oscillation.

The following ten slices (items 11–20) are implemented in this checkout: managed
worktree reconciliation and branch-collision recovery; crash-safe package
claiming with per-repository admission, priority ordering, and queue
backpressure; one canonical V1/V2 admission/state vocabulary; token-aware,
hashed prompt snapshots; untrusted-input wrappers for repository data; per-agent
routing/privacy/cost/latency policies; persisted circuit breakers and health
probes; fixed-fixture model evaluations; environment-aware verification
discovery; and process-group termination with bounded, hashed command evidence.

Items 21–25 are implemented in this checkout as the operator-control
milestone: checkpoint failure-injection coverage, protected mission-branch
integration with conflict escalation, durable operation IDs, recovery actions,
and live lease / heartbeat / phase / model-call observability. Item 33
(disposable PostgreSQL integration and failure-injection coverage) was completed
by the operator and is recorded as complete below.

Items 26–27 are implemented as the observability and session-hardening
milestone: persisted one-second telemetry history with dashboard charts and
alerts, correlation-aware resumable SSE, role-aware expiring sessions, write
rate limiting, HSTS guidance, and a read-only secret-redaction audit.

Item 28 is implemented with a single completion gate: roadmap entries count as
complete only when the package, independent review, verification, checkpoint,
and mission-branch integration all have durable evidence. Integration commits
the exact roadmap checkbox and records the resulting branch commit.

Item 29 adds a dependency projection sourced directly from Work Package state and
durable mission Pause, Resume, and Cancel controls. Controls propagate to linked jobs
and packages, preserve in-flight safe boundaries and repository locks, reconcile after
restart, and retain an append-only operation audit.

Item 30 retains the full Human Queue lifecycle in an append-only event stream while
keeping the existing queue row as the current projection. Creation evidence, later
observations, ownership, answer, resolution, and outcomes survive restart and are
available in the Control Center, with recursive redaction at every request writer.

## P0 — reliability before unattended operation

1. Fix durable phase truth so status, phase, current step, and the dashboard
   cannot disagree.
2. Complete a live three-package acceptance run with a deliberate reviewer
   revision and no manual `once` calls.
3. Add planning and phase watchdogs, heartbeats during long calls, and stale
   planner-lease recovery.
4. Make pause and cancel stop in-flight command work at safe process boundaries,
   with explicit durable acknowledgement.
5. Classify model, tool, test, repository, database, and human-gate failures
   separately with bounded retry and escalation policies.
6. Validate schema readiness at startup and expose migration-required health
   state instead of allowing silent operator-action failures.

## P1 — finish the V2 execution model

7. Remove the remaining Coder/Engineering dual identity across modules, events,
   database labels, logs, CLI names, and metrics.
8. Make Engineering sessions fully resumable from the last durable action,
   observation, model tier, test result, and reviewer issue.
9. Prove direct Reviewer → same Engineering session revision cycles, including
   repeated issues and process crashes.
10. Improve stagnation detection with semantic diff, repeated-failure,
    no-verification, and oscillation signals.
11. Add worktree cleanup, orphan detection, branch-collision recovery, and
    crash-safe package claiming.
12. Support concurrent jobs with per-repository limits, fairness, priorities,
    and queue backpressure.
13. Unify the remaining V1 and V2 transition paths behind one authoritative
    state machine while retaining a controlled compatibility migration.

## P1 — model and inference quality

14. Use token-aware prompt budgets and bounded, hashed context snapshots.
15. Treat repository documents and command output as untrusted input and add
    prompt-injection defenses.
16. Add per-agent routing policy, capability checks, privacy rules, latency
    history, and cloud cost budgets.
17. Persist circuit-breaker state across restarts and improve backend health
    probing.
18. Build fixed-fixture model evaluations for planning, implementation, review,
    repair, malformed output, and wrong-path behavior.

## P1 — verification and Git safety

19. Make verification environment-aware across Python, Node, Rust, Go, and
    project-specific scripts.
20. Terminate command process groups and persist bounded, reproducible test
    artifacts.
21. [x] Add checkpoint fault-injection tests for commit-before-database-update,
    staging, push, and integration failures.
22. [x] Finish safe mission-branch integration with explicit conflict and human
    escalation states.

## P2 — Control Center and operations

23. [x] Return operation IDs and show submitted, accepted, applied, and failed
    states for every job action.
24. [x] Add retry, recover-lease, rerun-verification, refresh-planning, and
    worktree-cleanup operator actions.
25. [x] Show heartbeat age, lease ownership, phase duration, model-call age,
    current command, and stale-data warnings.
26. [x] Add historical GPU/Ollama/inference charts, event correlation IDs, replayable
    SSE updates, and alerting.
27. [x] Add stronger session management, roles, write rate limits, TLS guidance,
    secret redaction audits, and production authentication options.

## P2 — missions and roadmap management

28. [x] Mark roadmap items complete only after review, verification, checkpoint, and
    integration evidence.
29. [x] Add dependency visualization and mission pause/resume/cancel controls.
30. [x] Expand human-queue history with evidence, ownership, answer, and outcome.
31. Add mission-level progress, quality, latency, and cost metrics.

## P3 — maintainability and polish

32. Add CI for tests, linting, type checking, packaging, and migration checks.
33. [x] Add disposable PostgreSQL integration tests and failure-injection suites.
34. Remove obsolete compatibility copies after migration is complete.
35. Add structured logging, rotation, backup/restore, disaster recovery, and
    corrupted-worktree runbooks.
36. Add accessibility, keyboard-navigation, search, pagination, and responsive
    Control Center testing.

## Recommended execution order

The first implementation milestone is items 1–5 above. After that, complete
worktree/recovery and schema-readiness hardening, then build operator recovery
actions and model evaluations, and only then enable broad concurrency and
automatic mission integration.
