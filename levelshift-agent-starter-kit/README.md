# LevelShift Agent Starter Kit

**Version:** STS 2.0.0 · Kit 1.0.0
**Owner:** ShiftAI AiCoE
**Source of truth:** the CTO reference architecture (three-plane design) and the CTO Technical Build Spec, both included in `docs/`.

---

## What this is

This folder is the **standard starting point for every agent built at LevelShift**. You do not start an agent from a blank repo, a copied project, or an improvised prompt. You start from this kit.

It exists so that every agent — regardless of who builds it or which AI coding assistant they use — comes out with the same architecture, the same governance controls, and the same telemetry. The kit encodes the CTO's rule:

> *Standing up a new agent should mean authoring a new Business Capability Layer configuration — never new Control Plane logic and never new Execution Engine mechanics.*

## How to use it

1. **Download & unzip** this kit into a new project folder (one folder per agent or per workflow).
2. **Open the folder in VS Code** with the approved Claude extension. The included `CLAUDE.md` tells the coding agent exactly which rules it must follow — do not delete or weaken it.
3. **Author the agent spec** first, from `docs/agent-spec-template.md`. No code before the spec.
4. **Author the Business Capability Layer config** against the matching template in `schemas/` (decision / enrichment / orchestrator).
5. **Build** following `docs/technical-build-spec.md` and `checklists/build-checklist.md`, in the stated order.
6. **Emit telemetry** conforming to `schemas/sts-core.schema.v2.0.0.json` from day one. Validate your records against the schema and compare with `telemetry/fixtures.json`.
7. **Prove it** with the tests in `checklists/acceptance-criteria.md` — they are written as real tests, not a checklist.

## What's inside

| Path | What it is |
|---|---|
| `CLAUDE.md` | Binding instructions for the AI coding agent. The governance rules, machine-enforced. |
| `docs/reference-architecture.md` | The three-plane architecture: Control Plane, Business Capability Layer, Execution Engine. |
| `docs/technical-build-spec.md` | The CTO Technical Build Spec: pinned stack, repo layout, interfaces, DB schema, build order. |
| `docs/telemetry-standard.md` | ShiftAI Telemetry Standard (STS) v2 — the event and attribute contract every agent emits. |
| `docs/agent-spec-template.md` | The agent specification document template. Fill it in before writing code. |
| `schemas/sts-core.schema.v2.0.0.json` | JSON Schema that validates every telemetry record. |
| `schemas/decision-agent.template.json` | Business Capability config template — decision agents. |
| `schemas/enrichment-agent.template.json` | Business Capability config template — enrichment agents. |
| `schemas/orchestrator-agent.template.json` | Business Capability config template — orchestrator agents. |
| `prompts/layer3-reasoning.hbs` | The single Layer 3 reasoning prompt template. The only place that prompt text lives. |
| `telemetry/fixtures.json` | Valid example records: decision, action, escalation, failure, human gate. |
| `checklists/build-checklist.md` | The platform-agnostic build order and reference checklist. |
| `checklists/acceptance-criteria.md` | The acceptance tests every agent must pass before go-live. |

## Non-negotiables (summary — details in CLAUDE.md)

- **Plane isolation.** Control Plane code never imports Business Capability code and never contains a domain-specific string.
- **Config-driven agents.** A new agent is a new config + a spec — not new engine mechanics.
- **One prompt template.** Layer 3 reasoning prompts come from `prompts/layer3-reasoning.hbs`, filled at runtime. Never hand-write a new one per agent.
- **Append-only audit.** An audit record at every step; no update or delete path exists anywhere.
- **Kill switch before action.** Checked immediately before every Layer 4 action fires.
- **Idempotent actions.** Every side effect checks its idempotency key first.
- **Telemetry is not optional.** Every run emits STS v2 records; a run that emits nothing did not happen, and that is a defect.

## Getting help

Questions, gaps in the kit, or a workflow the templates don't fit → raise it with the AiCoE **before** improvising. The kit is versioned; improvements land here, not in one-off forks.
