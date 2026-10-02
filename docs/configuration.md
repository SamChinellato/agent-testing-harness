# Configuration Reference

All configuration is loaded from a `.env` file in the project root, or from environment variables directly. Environment variables always take precedence over `.env` values.

Copy `.env.example` to `.env` as your starting point.

---

## Endpoint configuration

These tell the agent where to send model requests.

| Variable | Required | Default | Description |
|---|---|---|---|
| `MAAS_ENDPOINT` | Yes* | — | Full endpoint URL including `/v1`. Example: `https://gateway.example.com/namespace/granite-3.1-2b-instruct/v1`. Takes priority over `MAAS_HOST` + `MODEL_PATH` if both are set. |
| `MAAS_HOST` | Yes* | — | Gateway hostname only. Use with `MODEL_PATH` as an alternative to `MAAS_ENDPOINT`. |
| `MODEL_PATH` | No | — | Path appended to `MAAS_HOST`. Example: `/models-as-a-service/granite-3.1-2b-instruct`. |
| `MODEL_NAME` | No | `granite-3.1-2b-instruct` | Model name sent in the `"model"` field of every API request. Must match what the server expects. |
| `MAAS_API_KEY` | Yes | — | Bearer token sent in `Authorization: Bearer <key>` on every request. |

\* Either `MAAS_ENDPOINT` **or** `MAAS_HOST` must be set. If you provide `MAAS_ENDPOINT`, `MAAS_HOST` and `MODEL_PATH` are ignored.

### URL construction

The harness always appends `/chat/completions` to the base URL when making requests. It normalises your endpoint value by:

- Stripping trailing `/chat/completions` or `/chat` if you accidentally include them
- Ensuring the URL ends with `/v1`
- Prepending `https://` if no scheme is provided

So all of these are equivalent:

```bash
MAAS_ENDPOINT=https://gateway.example.com/ns/model/v1
MAAS_ENDPOINT=https://gateway.example.com/ns/model/v1/chat/completions
MAAS_ENDPOINT=gateway.example.com/ns/model/v1
```

---

## Agent identity

These control how the agent appears in the dashboard UI. None are required — the harness works fine without them.

| Variable | Required | Default | Description |
|---|---|---|---|
| `USER_ID` | No | `user-a` | Internal routing key. Must be **unique per agent**. Used to route control commands, telemetry events, and chart data. Can be any string: `agent-1`, `my-bot`, `dispute-triage`. |
| `TEAM_ID` | No | `team-alpha` | Team label shown under the agent name in the dashboard. Any string. `"Banking Ops"`, `"Risk & Compliance"`, `"QA Team"`. |
| `DISPLAY_NAME` | No | *(value of USER_ID)* | Human-readable agent name shown as the lane title. Example: `"Dispute Triage Bot"`. |
| `TASK_DESCRIPTION` | No | *(agent type default)* | One-line description shown on the agent card. Example: `"Processing dispute letters"`. |
| `AGENT_TYPE` | No | `triage` | Which agent logic to run. Built-in values: `triage`, `assistant`, `research`. Add new values by extending `runner.py`. |
| `AGENT_MODE` | No | `naive` | Starting behaviour mode. One of `naive`, `paced`, `runaway`. Can be changed live from the dashboard. See [Agent Modes](agent-modes.md). |

---

## Rate limiting and pacing

These control the client-side token budget enforced by the harness.

| Variable | Required | Default | Description |
|---|---|---|---|
| `LIMIT_TPM` | No | `2000` | Tokens per minute budget. In `paced` mode, the token bucket targets `LIMIT_TPM × PACED_TARGET_RATIO` TPM. Also sets the red reference line on the dashboard chart. |
| `PACED_TARGET_RATIO` | No | `0.90` | Fraction of `LIMIT_TPM` the paced token bucket targets. `0.90` means the agent self-limits to 90% of the declared limit, giving headroom to avoid hitting the server's hard limit. |

---

## Dashboard connection

| Variable | Required | Default | Description |
|---|---|---|---|
| `DASHBOARD_URL` | No | `http://localhost:8000` | URL of the running dashboard. Agents use this to register on startup and post telemetry after every model call. Set this if you run the dashboard on a non-default port or a remote host. |

---

## Example `.env` files

### Minimal — one agent, paced mode

```bash
MAAS_ENDPOINT=https://your-gateway/namespace/model/v1
MAAS_API_KEY=sk-your-key
AGENT_MODE=paced
LIMIT_TPM=2000
```

### Full — named agent with team, explicit identity

```bash
MAAS_ENDPOINT=https://your-gateway/namespace/model/v1
MAAS_API_KEY=sk-your-key

USER_ID=dispute-triage
TEAM_ID=Banking Ops
DISPLAY_NAME=Dispute Triage Bot
TASK_DESCRIPTION=Processing dispute letters
AGENT_TYPE=triage
AGENT_MODE=paced
LIMIT_TPM=3000
PACED_TARGET_RATIO=0.85
```

### Two agents sharing a key but different identities

Agent 1's environment:
```bash
MAAS_API_KEY=sk-shared-key
USER_ID=agent-triage
DISPLAY_NAME=Dispute Triage Bot
AGENT_TYPE=triage
AGENT_MODE=paced
```

Agent 2's environment:
```bash
MAAS_API_KEY=sk-shared-key
USER_ID=agent-research
DISPLAY_NAME=Policy Research Agent
AGENT_TYPE=research
AGENT_MODE=paced
```

---

## Passing configuration without a `.env` file

All variables can be passed inline as environment variables. This is useful for running multiple agents from a single `.env` while overriding specific values per-agent:

```bash
MAAS_API_KEY=sk-key USER_ID=bot-1 DISPLAY_NAME="Bot One" uv run agent
MAAS_API_KEY=sk-key USER_ID=bot-2 DISPLAY_NAME="Bot Two" uv run agent
```

Or using `export` in a shell session:

```bash
export MAAS_ENDPOINT=https://gateway.example.com/ns/model/v1
export MAAS_API_KEY=sk-key
DISPLAY_NAME="My Agent" uv run agent
```
