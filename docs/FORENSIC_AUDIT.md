# Autonomous Workflow Forensic Audit

## Scope and evidence

This audit traces the implementation in `agent_core`, `planner_agent`,
`coder_agent`, `reviewer_agent`, `orchestrator`, the routing client, PostgreSQL
store, systemd units, and the supplied production logs. The live database and
worker hosts are not directly accessible from the development session, so the
production timeline below is based on the sanitized `agent-orchestrator
inspect` output supplied by the operator.

## Executive finding

The first abnormal transition is Planner output, not GPU utilization or the
Orchestrator loop. The local model repeatedly returned a `blocked` object with
`blocker: null` and the explanation `Unknown step decision provided`. No
implementation Step was created, so Coder had nothing claimable. A second,
independent defect allowed a malformed object-valued `decision` to raise
`TypeError: unhashable type: 'dict'`, which was logged as `planning_crashed`.

After a Step was eventually created, Coder received an invalid file action and
attempted to access `/home/finn/work/align/repository`. That was a model/tool
contract error; it crashed the coding attempt instead of being returned as a
correctable observation. This is now handled as a recoverable action error.

## Health ratings

| Component | Rating | Evidence |
|---|---|---|
| Planner | DEGRADED | Ollama was reachable, but repeated invalid blocked responses prevented Step creation. Object-valued decisions previously crashed parsing. |
| Coder | DEGRADED | It claims valid Steps, but invalid model paths previously escaped as process-level crashes. |
| Reviewer | INTERMITTENT | Contract and retry paths are covered by tests; no live review was available for this audit. |
| Verification | DEGRADED | Deterministic service exists and is unit-tested; live command environment was not available. |
| Checkpoint | DEGRADED | Git checkpoint service is present and tested; no disposable live commit was run here. |
| Orchestrator | DEGRADED | Continuous loop and lease recovery exist, but planner crashes and malformed decisions delayed progress. |
| Routing | INTERMITTENT | Ollama endpoint responded at `192.168.10.193:11434`; routing/OpenRouter live escalation was not exercised here. |

## Handoff matrix

| Handoff | Rating | Reason |
|---|---|---|
| Planner → Coder | BROKEN in observed runs | Planner did not persist a valid `create_step`; therefore no queued Step existed. |
| Coder → Reviewer | INTERMITTENT | Durable transition exists, but coder action crashes could prevent it. |
| Reviewer → Coder | PASS by deterministic tests | `changes_requested` returns the same logical Step with feedback and bounded attempts. |
| Reviewer → Verification | PASS by implementation/tests | Approved review is claimable by Orchestrator verification. |
| Verification → Checkpoint | PASS by implementation/tests | Successful verification creates a checkpoint claim. |
| Checkpoint → Planner | PASS by implementation/tests | Completed Steps leave the Job eligible for replanning. |

## Production timeline (supplied Job 2)

1. Job created at 07:32:12.
2. Planner claimed it at 07:32:20.
3. Planner returned malformed `blocked` output at 07:32:28; no Step was created.
4. Repeated planner retries at 07:37:56, 07:43:53, 07:44:22, 07:44:54,
   07:45:26, and 07:45:56 remained non-progressing.
5. At 07:38:42, an object-valued decision caused `unhashable type: 'dict'`.
6. Once a Step existed, Coder attempt 2 failed at 08:42:43 while trying to
   open `/home/finn/work/align/repository`.

The correct classification is `invalid_model_output` for the planner events
and `invalid_coder_action` for the coding event—not a genuine human gate.

## Configuration finding

The supplied `.env.example` selected `llama3.2:latest` for all three agents,
despite `qwen2.5-coder:7b-instruct` being installed on the Ollama host. The
repository default is now the instruction-following Qwen model. OpenRouter is
available as a configured planner/reviewer fallback when its API key is
present.

## Repairs already made

- Planner parser rejects non-string decision values without crashing.
- Invalid coder file actions are returned to the model as bounded corrective
  observations instead of crashing the coding lease.
- Retryable planner blockers remain scheduled rather than permanently blocked.
- Control Center exposes blocker details and management actions.
- Local model defaults now target `qwen2.5-coder:7b-instruct`.

## Remaining live acceptance work

The supplied host evidence does not prove three autonomous commits because the
development session cannot connect to the production PostgreSQL database,
Align checkout, Ollama process, or systemd services. On the worker host, the
next acceptance run must start only `agent-orchestrator run`, then verify three
reviewed/verified commits and one deliberate reviewer-change loop. The
operator runbook contains the required service and log commands.
