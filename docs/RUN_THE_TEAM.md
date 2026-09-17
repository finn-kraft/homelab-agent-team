# Run the Agent Team and Control Center

This is the operator runbook for one persistent local stack:

```text
PostgreSQL ─┬─ Orchestrator (embeds Planner, Coder, and Reviewer)
            └─ Control Center web UI
Ollama ── Routing Agent ── Orchestrator
```

Do **not** also start the legacy `planner-agent run`, `coder-agent run`, or
`reviewer-agent run` loops. `agent-orchestrator run` constructs and coordinates all
three specialists.

There is no Node.js or frontend build step. The dashboard is packaged static HTML,
CSS, and JavaScript and is served by `agent-control-center`.

## 1. Install once

Use a stable checkout; do not run production services from a temporary worktree.
The example service files use `/home/finn/homelab-ai/dev-team`:

```bash
git clone https://github.com/finn-kraft/homelab-agent-team.git /home/finn/homelab-ai/dev-team
cd /home/finn/homelab-ai/dev-team
git switch feat/reliability-control-center

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install -e ./homelab-routing-agent/routing-agent

cp .env.example .env
chmod 600 .env
cp homelab-routing-agent/routing-agent/config.example.json \
  homelab-routing-agent/routing-agent/config.json
```

If the branch has already been merged, use the repository's default branch instead of
`feat/reliability-control-center`.

## 2. Configure `.env`

At minimum, verify these values:

```bash
DATABASE_URL=postgresql://agent_team_app@127.0.0.1:5432/agent_team

PLANNER_ALLOWED_REPOSITORIES=/home/finn/work
CODER_WORKSPACES=/home/finn/work
REVIEWER_ALLOWED_REPOSITORIES=/home/finn/work

ROUTER_URL=http://127.0.0.1:8090
OLLAMA_URL=http://192.168.10.193:11434
PLANNER_MODEL=llama3.2:latest
CODER_MODEL=llama3.2:latest
REVIEWER_MODEL=llama3.2:latest

CONTROL_CENTER_HOST=127.0.0.1
CONTROL_CENTER_PORT=8080
CONTROL_CENTER_TOKEN=REPLACE_WITH_A_LONG_RANDOM_VALUE
CONTROL_CENTER_PROJECTS='[{"id":"align","name":"Align","repository":"/home/finn/work/align","branch":"agents/autonomous-align","roadmap":"docs/roadmap.md"}]'
```

Generate the dashboard token with:

```bash
openssl rand -hex 32
```

Keep JSON environment values inside outer single quotes. Without them, loading `.env`
from a shell removes the JSON double quotes.

The safe default is `AUTO_PUSH=false`. The team creates reviewed local checkpoint
commits on the dedicated worker branch but does not push or merge them automatically.

## 3. Start PostgreSQL and Ollama

For a quick homelab/development database, use the included Compose service:

```bash
cd /home/finn/homelab-ai/dev-team
docker compose up -d postgres
docker compose ps
```

That container uses the development credentials below, so change `DATABASE_URL` in
`.env` to match it:

```bash
DATABASE_URL=postgresql://coder:coder@127.0.0.1:5432/coder
```

For a long-lived installation, provision a restricted PostgreSQL application role and
database through your normal database administration process instead of using the
example `coder` password.

On the Ollama host, ensure the service and configured models are ready:

```bash
sudo systemctl enable --now ollama
ollama pull llama3.2:latest
curl -fsS http://127.0.0.1:11434/api/tags
```

If Ollama is on another machine, run those commands there and make sure `OLLAMA_URL`
is reachable from the agent server.

## 4. Initialize the workflow database

Run this once after first install and again after pulling a release that contains an
additive migration:

```bash
cd /home/finn/homelab-ai/dev-team
set -a
. ./.env
set +a
.venv/bin/agent-orchestrator init-db
```

Use a migration-capable database identity for this step. The normal services should
run with the restricted application identity.

## 5. Prepare each repository

The checkout and dedicated branch must already exist. The agents intentionally refuse
to switch branches themselves:

```bash
git -C /home/finn/work/align switch agents/autonomous-align
git -C /home/finn/work/align config user.name "Align Autonomous Agent"
git -C /home/finn/work/align config user.email "agent@homelab.local"
git -C /home/finn/work/align status --short
```

The service user must be able to read and write the checkout, and its path must be
inside all three repository allowlists in `.env`.

## 6. Test the stack in the foreground

Use three terminals for the first run.

Terminal 1 — routing API:

```bash
cd /home/finn/homelab-ai/dev-team/homelab-routing-agent/routing-agent
set -a; . /home/finn/homelab-ai/dev-team/.env; set +a
/home/finn/homelab-ai/dev-team/.venv/bin/python -m uvicorn \
  routing_agent.api:app --host 127.0.0.1 --port 8090
```

Terminal 2 — all development agents through the Orchestrator:

```bash
cd /home/finn/homelab-ai/dev-team
set -a; . ./.env; set +a
.venv/bin/agent-orchestrator run
```

Terminal 3 — Control Center:

```bash
cd /home/finn/homelab-ai/dev-team
set -a; . ./.env; set +a
.venv/bin/agent-control-center --host 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080` from the same machine and enter
`CONTROL_CENTER_TOKEN`. For access from another machine, put the loopback listener
behind your authenticated TLS reverse proxy; do not expose the unauthenticated routing
API publicly.

## 7. Install one-command system services

Review the `User`, `WorkingDirectory`, `EnvironmentFile`, and `ExecStart` paths in all
three `.service.example` files first. Then install them and the grouping target:

```bash
cd /home/finn/homelab-ai/dev-team
sudo install -m 0644 deploy/homelab-routing-agent.service.example \
  /etc/systemd/system/homelab-routing-agent.service
sudo install -m 0644 deploy/agent-orchestrator.service.example \
  /etc/systemd/system/agent-orchestrator.service
sudo install -m 0644 deploy/agent-control-center.service.example \
  /etc/systemd/system/agent-control-center.service
sudo install -m 0644 deploy/agent-team.target.example \
  /etc/systemd/system/agent-team.target

sudo systemctl daemon-reload
sudo systemctl enable --now agent-team.target
```

After that, the agent team and dashboard start together at boot. The routine lifecycle
commands are:

```bash
sudo systemctl start agent-team.target
sudo systemctl stop agent-team.target
sudo systemctl restart agent-team.target
sudo systemctl status agent-team.target
```

Follow all three logs together:

```bash
sudo journalctl -u homelab-routing-agent \
  -u agent-orchestrator -u agent-control-center -f
```

If PostgreSQL is the included Compose container, its `restart: unless-stopped` policy
brings it back after reboot once Docker starts. If PostgreSQL is a native service,
enable it separately:

```bash
sudo systemctl enable --now postgresql
```

## 8. Validate the running system

```bash
curl -fsS http://127.0.0.1:8090/health

curl -fsS -X POST http://127.0.0.1:8090/route/inference \
  -H 'Content-Type: application/json' \
  -d '{"request":"startup check","caller_agent":"operator","task_type":"healthcheck","attempt":1,"complexity":"medium"}'

curl -fsS http://127.0.0.1:8080/health

cd /home/finn/homelab-ai/dev-team
set -a; . ./.env; set +a
curl -fsS -H "Authorization: Bearer $CONTROL_CENTER_TOKEN" \
  http://127.0.0.1:8080/api/overview

.venv/bin/agent-orchestrator status
```

All four checks should return JSON without an HTTP error.

## 9. Create work

The easiest route is **New job** in the dashboard. The equivalent command is:

```bash
cd /home/finn/homelab-ai/dev-team
set -a; . ./.env; set +a
.venv/bin/agent-orchestrator create-job \
  "Continuously improve Align according to docs/roadmap.md." \
  --repository /home/finn/work/align \
  --branch agents/autonomous-align \
  --priority 10
```

Inspect or control it from the UI, or use:

```bash
.venv/bin/agent-orchestrator status
.venv/bin/agent-orchestrator inspect JOB_ID
.venv/bin/agent-orchestrator pause JOB_ID
.venv/bin/agent-orchestrator resume JOB_ID
.venv/bin/agent-orchestrator cancel JOB_ID
```

Jobs waiting for a human decision must be answered through the dashboard. `resume` is
reserved for paused or technically blocked jobs and cannot bypass a human gate.

## 10. Update later

```bash
cd /home/finn/homelab-ai/dev-team
git pull --ff-only
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install -e ./homelab-routing-agent/routing-agent
set -a; . ./.env; set +a
.venv/bin/agent-orchestrator init-db
sudo systemctl restart agent-team.target
```

The durable state lives in PostgreSQL and the accepted source changes live in Git, so
restarting the processes does not erase job progress.
