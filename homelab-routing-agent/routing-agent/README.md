# Homelab Routing Agent

A small, policy-driven "front door" for your agents. It accepts a natural-language
request, asks an LLM to classify it when one is configured, then selects the best
agent and execution location while respecting urgency, complexity, available
compute, electricity use, heating value, and your priorities.

## What it does now

- routes requests to `business`, `homelab`, `financial`, `research`, or `general`
- chooses local or cloud execution from live-ish compute profiles
- prefers local work when it is capable and policy allows it
- can deliberately use local compute for useful heat during a heating season
- exposes every reason behind a decision instead of hiding it in a model prompt
- supports an optional OpenAI-compatible classifier; it has a reliable keyword
  fallback so the router remains usable when cloud AI is unavailable

## Quick start

```bash
cd routing-agent
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
router "Create an estimate template for a deck-cleaning job"
```

To use an OpenAI-compatible endpoint for intent classification, set these before
running it:

```bash
export ROUTER_LLM_BASE_URL="https://your-endpoint/v1"
export ROUTER_LLM_API_KEY="..."
export ROUTER_LLM_MODEL="your-model"
```

The route is printed as JSON. Start the web endpoint with:

```bash
uvicorn routing_agent.api:app --host 0.0.0.0 --port 8090
```

## Next integration steps

1. Replace the example agent endpoints in `config.example.json` with your real
   OpenClaw/Kubernetes workers or MCP-backed agents.
2. Have the control center periodically update `compute_profiles` using Proxmox
   metrics, outdoor temperature, and electricity pricing.

This service intentionally decides *where work should go*; it does not yet send
the task to the destination agent. That handoff should be added once the actual
agent endpoints and authentication method are chosen.

## Internal development-agent inference routing

`POST /route` remains the front-door/domain route for natural-language user
requests.  `POST /route/inference` is a separate, backward-compatible endpoint
for Planner, Coder, and Reviewer model selection.  It accepts structured
metadata such as this:

```json
{
  "request": "Review candidate implementation",
  "caller_agent": "reviewer-agent",
  "task_type": "code_review",
  "attempt": 3,
  "complexity": "heavy",
  "privacy_sensitive": true,
  "needs_strong_model": true
}
```

The response identifies the backend without deciding the workflow transition:

```json
{
  "execution_target": "ollama",
  "provider": "ollama",
  "model": "llama3.2:latest",
  "location": "local",
  "reason": "privacy-sensitive work kept on local Ollama",
  "compute_profile": "k3s-gpu-worker",
  "escalation_permitted": false
}
```

The default policy keeps attempts 1-3 on a capable local Ollama target.  At
attempt 4, OpenRouter becomes eligible only after repeated local failure and
only for non-private work.  Cloud can still be used earlier if no safe capable
local target is available.  Configure the model names and threshold in
`config.json` (copy `config.example.json`); do not put API keys in that file.
