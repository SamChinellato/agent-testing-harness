# MaaS Agent Testing Harness — Documentation

This harness lets you run one or more LangGraph agents against any OpenAI-compatible LLM endpoint, observe live token consumption, test rate-limiting behaviour, and compare how different agent architectures respond to throttling — all through a live browser dashboard.

It is intentionally generic. The banking scenario agents (Dispute Triage, Policy Research, Customer Assistant) are included as working examples, but the harness is designed to be reused with any agent and any model endpoint.

---

## Documentation Index

| Document | What it covers |
|---|---|
| [Getting Started](getting-started.md) | Install, configure, start the dashboard, connect your first agent |
| [Configuration Reference](configuration.md) | Every environment variable, what it does, and when to set it |
| [Agent Modes](agent-modes.md) | `naive`, `paced`, `runaway` — what each mode does and when to use it |
| [Dashboard Guide](dashboard.md) | What every control, metric, and indicator means |
| [Writing an Agent](writing-an-agent.md) | How to write a new agent that works with the harness |
| [How the Harness Works](harness-internals.md) | ModelHarness, TokenBucket, TelemetryEmitter — internals reference |
| [API Reference](api-reference.md) | All dashboard REST endpoints |
| [Redesign Spec](redesign-spec.md) | Root-cause review of the control/state/UI issues and the target architecture |
| [Phase 0 — Hotfixes](phase-0-hotfixes.md) | ✅ Landed. Independent, low-risk fixes — per-item spec and verification |
| [Phase 1–5 — Implementation Plan](phase-1-5-implementation-plan.md) | Schemas, API contracts, and task breakdown for the full control-plane/budgets/scenarios/metrics/UI rewrite |
