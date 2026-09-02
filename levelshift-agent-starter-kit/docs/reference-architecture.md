# Autonomous Process Execution — Reference Architecture

**Condensed from:** *Autonomous Process Execution Reference Architecture Specification* (Document owner: Manoj — CTO, LevelShift). That document is the authority; this file restates it for daily use inside the kit. Platform-portable: Agentforce, Copilot/Foundry, LangChain/LangGraph, AWS Bedrock, custom.

---

## 1. The design goal

A modular, three-plane architecture for **any agent built to autonomously execute a business process**:

> Standing up a new agent should mean authoring a new **Business Capability Layer configuration** — never writing new Control Plane logic or new Execution Engine mechanics.

The test for whether an implementation has stayed true to the design:

- No plane contains code or logic referencing a specific business domain, **except** the Business Capability Layer.
- No plane contains logic specific to one target platform, **except** the platform-binding layer.

## 2. The three planes

### 2.1 Control Plane
**Purpose:** guarantee operational safety and governance uniformly, with zero knowledge of what business decision is being made.

| Component | Function |
|---|---|
| Kill switch / autonomy pause | Forces every case for a scope (agent, process, or client) into human review, independent of confidence or envelope logic |
| Rate breaker | Trips automatically on anomalous aggregate auto-execution volume, independent of any single case being within bounds |
| Audit trail | Append-only record of every decision, its basis, and the config/model versions active at the time |
| Version registry | Tracks config, prompt, and model versions so past decisions remain explainable after later changes |
| Drift monitor | Compares current decision distribution against historical baseline on a schedule, flags for human review |
| Injection guard | Treats all free-text intake as untrusted data, never as instructions, before it reaches the reasoning layer |
| Backtest harness | Runs a proposed config or model change against historical cases before it goes live — no live-execution capability |
| Cost governor | Tracks cumulative spend against budget; alerts or throttles on divergence |
| RBAC | Least-privilege access to source systems, config editing, and case review, scoped per role |
| SLA monitor | Tracks time-to-resolution per escalation tier; escalates the queue itself on breach |

**Interface contract:** the Control Plane consumes a small fixed set of generic events emitted by the Execution Engine — `decision_made`, `action_taken`, `case_escalated`, `config_loaded`, `latency_ms` — and emits a small fixed set of generic controls — `pause`, `resume`, `reject_action`, `flag_case`. It never receives or inspects domain-specific fields. (STS v2 telemetry is the durable, queryable form of exactly these events — see `docs/telemetry-standard.md`.)

### 2.2 Business Capability Layer
**Purpose:** the sole swappable surface. Everything domain-specific about a given agent lives here and only here.

| Agent type | Config contents |
|---|---|
| **Decision agent** | Intake schema, policy rules, action-class taxonomy, authority envelope, routing map, reason codes |
| **Enrichment agent** | Source schema, enrichment rules, output schema, confidence threshold for insufficient-data handling |
| **Orchestrator agent** | Stage/process definition, transition rules, handoff conditions, control-scope hierarchy for cascading Control Plane actions to child agents |

Every configuration is **versioned and read-only at runtime** — the Execution Engine reads it per case but never writes it; changes only happen through a design-time editor, keeping every past decision traceable to the exact config version active when it was made.

### 2.3 Execution Engine
**Purpose:** mechanically carry a case from intake to resolution, for any domain, by reading its behavior entirely from the active Business Capability Layer config.

| Component | Function |
|---|---|
| Orchestration / state machine | Layer 1 (Classifier) → Layer 2 (Policy) → Novelty check → Layer 3 (Reasoning) → Layer 4 (Action) or Escalation, per case |
| Case-state memory | Working memory for a single case in progress — required by orchestrator-type agents |
| Episodic memory (precedent store) | Past resolved cases with similarity/novelty scoring and freshness weighting — required by decision-type agents |
| Tool / action interface | Standard, provider-agnostic contract for executing actions — MCP where the target system supports it |
| Reasoning interface | Pluggable Layer 3 slot — hosted API (Claude), self-hosted model, or a deterministic rubric — selected by config, never hardcoded |
| Context Package assembler | Builds the structured handoff package on escalation |

The reasoning interface must never assume a specific provider anywhere in its code path.

## 3. Interfaces between planes

| Interface | Direction | Contract |
|---|---|---|
| Execution Engine → Control Plane | Events out | Generic lifecycle events only — no domain fields |
| Control Plane → Execution Engine | Controls in | `pause` / `resume` / `reject_action` / `flag_case` — checked before every Layer 4 action fires |
| Business Capability Layer → Execution Engine | Config read | Versioned, read-only document, loaded per case |
| Execution Engine → Business Capability Layer | **None** | The engine never writes config |

## 4. Platform portability

The Control Plane and Business Capability Layer are expressed as **platform-neutral specifications** at design time. The Execution Engine is the one plane that is inherently platform-bound — it is implemented differently per target (Agentforce, Copilot Studio, LangGraph, Bedrock AgentCore, or custom build).

- Where a platform has a **native equivalent** for a Control Plane component (e.g. Agentforce Trust Layer audit, Bedrock AgentCore Policy), bind to it and record that you did — gaps stay visible.
- Where it has **none** (most platforms, for most controls), the requirement is met by an external wrapper.
- The Business Capability config is written once, platform-neutral, then translated into the platform's native authoring format at build time.
- Platform capability tables go stale quickly — re-verify against current vendor documentation before any client-facing use.

Each built agent records its **PlatformBinding**: target platform, natively covered controls, gap fills, translation notes, and who bound it when. The agent spec template has a section for exactly this.

## 5. Platform selection rubric (applied once per engagement)

| Factor | Pushes toward a named platform | Pushes toward custom build |
|---|---|---|
| Client's core system of record | Salesforce → Agentforce; Microsoft → Copilot/Foundry; AWS → Bedrock | No dominant platform, or multi-platform estate |
| Governance requirements vs native coverage | Native trust/policy layer covers most controls | Requirements exceed native enforcement |
| Business-logic customization | Fits the platform's authoring model | Capability-layer complexity exceeds it |
| Multi-agent / cross-platform coordination | Single platform owns the process | Process spans systems no platform owns |
| Portability priority | Lock-in acceptable | Avoiding vendor dependency is a priority |

This kit's `docs/technical-build-spec.md` covers the **custom-build path**. For platform-native builds, the spec template's Control Plane Binding section captures the native-vs-wrapper mapping, and telemetry still conforms to STS v2.
