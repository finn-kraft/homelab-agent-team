# Operational resilience and disaster recovery

This runbook treats PostgreSQL as workflow truth, Git as source-code truth, and
worktrees as replaceable execution surfaces. Recovery is deliberately
operator-led: preserve evidence first, diagnose second, and mutate state only
after the target and consequence are clear.

## Routine preparation

Install PostgreSQL client tools (`pg_dump`, `pg_restore`, `createdb`, `dropdb`,
and `psql`) on the control host. Create `/etc/agent-team/pg_service.conf` with
two named profiles:

```ini
[agent_team_backup]
host=127.0.0.1
port=5432
dbname=agent_team
user=agent_team_backup

[agent_team_admin]
host=127.0.0.1
port=5432
dbname=postgres
user=agent_team_restore_verifier
```

Supply passwords through `/etc/agent-team/pgpass`, a systemd credential, or an
equivalent root/operator-managed secret. Set mode `0600` on both files. Do not
commit either file, paste passwords into unit files, or put a connection URL on
the command line. The backup role needs read access to the agent database. The
verification role needs permission to create and drop disposable databases;
production restore access should be a separate, time-limited profile.

Install the example backup service and timer from `deploy/`, create
`/var/backups/agent-team` owned by the service user with mode `0700`, then check:

```text
systemctl start agent-db-backup.service
systemctl status agent-db-backup.service
journalctl -u agent-db-backup.service --since today
systemctl enable --now agent-db-backup.timer
systemctl list-timers agent-db-backup.timer
```

`agent-db-backup create` writes a PostgreSQL custom archive atomically, mode
`0600`, plus a SHA-256/size metadata file. The example service then performs a
real restore into a uniquely named scratch database, checks for restored public
tables, and drops that database. A successful `pg_dump` exit alone is not a
valid backup. Monitor timer failures and periodically copy verified archives to
separate storage with independent retention.

The services emit one-line JSON to journald. Review and install
`deploy/journald-agent-team.conf.example` to bound disk use and retention; do
not add logrotate for the same journal stream. Reload/restart journald only in a
normal maintenance window for the host.

## Recovery order

### 1. Contain and preserve evidence

Stop the orchestrator first so it cannot acquire new leases. Stop the Control
Center if writes must also be frozen. Record service status, copy the relevant
journal window, note the database and repository hosts, and preserve suspect
worktree directories. Do not run cleanup, prune, reset, or checkout commands.

### 2. PostgreSQL

Select a backup whose adjacent `.dump.json` metadata is present. Prove it again
before use:

```text
agent-db-backup verify /var/backups/agent-team/agent-team-TIMESTAMP.dump --admin-service agent_team_admin
```

Create a new empty target database; do not restore over the damaged production
database. Point a temporary libpq service profile at that empty target database,
then restore with an explicit safety acknowledgement:

```text
agent-db-backup restore /var/backups/agent-team/agent-team-TIMESTAMP.dump --target-service agent_team_recovery --confirm-empty-target
```

The tool checks the SHA-256 and archive catalog, refuses a non-empty target,
uses `--exit-on-error`, and never issues `DROP`, `--clean`, or ownership changes.
Run `agent-orchestrator init-db` against the recovered database only after the
restore succeeds, then use `agent-orchestrator audit` and inspect schema
readiness. Keep the old database untouched until the recovery is accepted.

### 3. Git repositories and worktrees

Verify each source repository with `git status`, `git fsck`, and a known remote
before considering a worktree repair. The source repository and committed
branches are authoritative; autonomous worktree changes may still contain
valuable uncommitted evidence.

Use the read-only diagnosis first:

```text
agent-orchestrator diagnose-worktree --repository /home/finn/work/align --worktree /home/finn/agent-worktrees/mission-1/package-2
```

The report gives registration, metadata, lock/prunable, and dirty-entry counts
without logging filenames or file contents. A dirty or unregistered directory
must be preserved for inspection. If the directory contains Git worktree
metadata and only registration paths are damaged, run the explicit
metadata-only repair and diagnose again:

```text
agent-orchestrator repair-worktree --repository /home/finn/work/align --worktree /home/finn/agent-worktrees/mission-1/package-2 --confirm
```

The repair path only invokes `git worktree repair`. It never runs git reset,
`git clean`, checkout, prune, force removal, or automatic reconstruction.
Background reconciliation now reports orphan candidates without deleting them;
the existing authenticated cleanup action remains the explicit operator path.

### 4. Configuration and secrets

Restore `.env`, libpq service profiles, `.pgpass`, OpenRouter credentials, TLS
material, and telemetry tokens from the secret/configuration store. Compare
names and permissions, not secret values, in tickets or logs. Confirm repository
allow-roots, worktree root, protected branches, and model endpoints before any
service starts. Do not commit recovered configuration or credentials.

### 5. Systemd services

Install/review the current units, run `systemd-analyze verify` on them, then
reload systemd. Start dependencies in this order: PostgreSQL, Ollama/router,
Control Center, and finally the orchestrator. Confirm each health/readiness view
before starting the next service. Keep automatic integration and push disabled
during recovery unless the incident plan explicitly requires them.

### 6. Workflow validation and controlled resume

Run the read-only workflow audit, inspect active missions/packages/jobs, check
human requests, leases, checkpoint evidence, and integration evidence. Reconcile
expired leases through existing operator actions rather than direct SQL. Resume
one low-risk package first, watch its Engineering → Review → Verification →
Checkpoint path, then expand gradually. Do not bulk-resume blocked work solely
because services are healthy.

## Restore drill and acceptance record

At least quarterly, restore the latest backup into an isolated database and
record the archive SHA-256, verification timestamp, restored table count,
schema-readiness result, workflow-audit result, duration, operator, and cleanup
confirmation. A drill passes only when the scratch restore, schema readiness,
durable mission/job reads, and application health checks all succeed. Never use
the production database as a drill target.
