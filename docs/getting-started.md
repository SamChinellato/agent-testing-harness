# Getting Started

This guide takes you from a fresh clone to a running dashboard with live agents in about 10 minutes.

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.12+ | Required |
| [uv](https://docs.astral.sh/uv/) | latest | Used for all Python dependency management and running scripts |
| Node.js | 18+ | Only needed to rebuild the frontend after UI changes |
| npm | 9+ | Same as above |
| An OpenAI-compatible LLM endpoint | — | Any endpoint that accepts `POST /v1/chat/completions` with a Bearer token |

> **Using OpenShift AI MaaS?** You need a MaaS API key per agent (or per team). See the [Configuration Reference](configuration.md) for endpoint URL formats.

---

## 1. Clone and install

```bash
git clone <repo-url>
cd maas-agent-demo
uv sync
```

This installs all Python dependencies into a local `.venv` using the lockfile.

---

## 2. Configure your environment

Copy the example file and edit it:

```bash
cp .env.example .env
```

At minimum, set:

```bash
# .env
MAAS_ENDPOINT=https://your-llm-gateway/namespace/model/v1
MAAS_API_KEY=your-api-key
```

The full list of options is in the [Configuration Reference](configuration.md). For a quick first run, these two are all you need.

---

## 3. Build the frontend

The dashboard UI is a pre-built React app served as static files by the FastAPI backend. Build it once:

```bash
cd dashboard/frontend
npm install
npm run build
cd ../..
```

> You only need to rebuild after making changes to the UI source in `dashboard/frontend/src/`. Normal usage — running agents, adjusting limits, watching metrics — does not require a rebuild.

---

## 4. Start the dashboard

```bash
uv run dashboard
```

Open **http://localhost:8000** in your browser. You will see the dashboard with "No agents connected". The dashboard is now running and waiting for agents to register.

To stop it at any time, press **Ctrl+C**. It exits within 1 second regardless of how many browser tabs are open.

---

## 5. Start your first agent

In a new terminal, run:

```bash
MAAS_API_KEY=your-api-key \
  DISPLAY_NAME="My First Agent" \
  TEAM_ID="My Team" \
  uv run agent
```

Within a second or two, a lane for your agent will appear in the dashboard. It starts **idle** — no model calls are made until you click **Start** in the UI.

The agent stays running in the terminal. Press **Ctrl+C** to stop it.

---

## 6. Start the agent from the dashboard

Click **Start** on the agent's card in the dashboard. You will see:

- The status badge changes from `IDLE` to `RUNNING`
- The connectivity dot turns green
- The chart begins plotting token consumption
- Log entries appear in the table at the bottom

Click **Stop** to pause the agent without killing the terminal process. Click **Start** again to resume — it picks up where it left off.

---

## Starting multiple agents

Each agent runs in its own terminal. Every agent must have a unique `USER_ID`. They can share a `MAAS_API_KEY` or use separate keys depending on your subscription setup.

```bash
# Terminal 1 — Triage agent
MAAS_API_KEY=key-a \
  USER_ID=agent-1 \
  TEAM_ID="Banking Ops" \
  DISPLAY_NAME="Dispute Triage Bot" \
  AGENT_TYPE=triage \
  AGENT_MODE=paced \
  uv run agent

# Terminal 2 — Research agent
MAAS_API_KEY=key-a \
  USER_ID=agent-2 \
  TEAM_ID="Banking Ops" \
  DISPLAY_NAME="Policy Research Agent" \
  AGENT_TYPE=research \
  AGENT_MODE=paced \
  uv run agent

# Terminal 3 — Assistant agent
MAAS_API_KEY=key-b \
  USER_ID=agent-3 \
  TEAM_ID="Risk & Compliance" \
  DISPLAY_NAME="Compliance Assistant" \
  AGENT_TYPE=assistant \
  AGENT_MODE=paced \
  uv run agent
```

Each agent registers itself with the dashboard on startup and gets its own lane.

---

## Adding a new agent type

To plug in your own agent logic:

1. Create a new file in `maasagentharness/agents/my_agent.py` — see [Writing an Agent](writing-an-agent.md) for the exact pattern.
2. Import and call it from `maasagentharness/runner.py` under a new `elif agent_type == "my_agent":` branch.
3. Start with `AGENT_TYPE=my_agent` in your environment.

You do not need to modify the dashboard, harness, telemetry, or pacer — they are fully generic.

---

## Rebuilding the frontend after UI changes

If you modify anything in `dashboard/frontend/src/`:

```bash
cd dashboard/frontend && npm run build
```

Then restart `uv run dashboard`.

---

## Quick reference: what runs where

```
Terminal 1              Terminal 2, 3, ...          Browser
─────────────────       ──────────────────────      ──────────────
uv run dashboard        uv run agent                localhost:8000
│                       │
│ serves React UI       │ registers on startup
│ :8000                 │ polls /api/workers/{id}/desired
│                       │ posts /events
│                       │
└───────────────────────┴──> same FastAPI process
```

---

## Troubleshooting

**Agent registers but no data appears on the chart**
The agent is idle. Click **Start** on its card in the dashboard.

**`ValueError: Neither MAAS_ENDPOINT nor MAAS_HOST is configured`**
Your `.env` is missing or the variable name is wrong. Check `.env.example` for the correct names.

**`429 Too Many Requests` immediately on start**
Your token limit is lower than the agent's estimated token usage per call. Reduce `LIMIT_TPM`, switch to `AGENT_MODE=paced`, or raise the subscription limit on your gateway.

**Dashboard won't start: `address already in use`**
Another process is on port 8000. Kill it with `lsof -ti:8000 | xargs kill` or set a different port: `uvicorn maasagentharness.dashboard.app:app --port 8001`.

**Agent says `Could not register with dashboard`**
The dashboard isn't running yet, or `DASHBOARD_URL` is wrong. Start the dashboard first, or set `DASHBOARD_URL=http://localhost:8000` explicitly.
