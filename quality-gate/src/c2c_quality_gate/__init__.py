"""Quality Gate & Approval Agent — LevelShift Content to Campaign Phase 1,
Agent 5.

Hybrid: a deterministic rules pass (terminology, banned terms, naming, AEO
named-mention, claim-marker resolution) + a Claude contextual pass (meaning-level
rules: BC/F&O independence in substance, Copilot scope, unsourced claims, tone,
brand voice) + a deterministic routing module (no LLM) that risk-classifies
gate-passed assets, sequences the retained human gates (Grammar/Quality Reviewer,
then BU Campaign Lead package sign-off), tracks SLAs, records every approval with
identity + hash, locks approved versions, and releases the approved
Campaign-in-a-Box reference as Phase 1's terminal output.

The gate flags and blocks — it NEVER edits content; approvals exist only as
identity-stamped human actions (no code path in this package approves anything —
static-tested); sequence integrity is structural; fail-closed everywhere: an
asset whose checks cannot complete never advances. Model: claude-sonnet-5
(Azure OpenAI is the dev/test substitute behind the shared provider interface).
"""

__version__ = "0.1.0"

AGENT_ID = "quality_gate_approval"
# The spec labels Agent 5 "Hybrid" (rules engine + AI + deterministic routing);
# the STS v2 kit taxonomy knows decision/enrichment/orchestrator — the gate's
# verdicts make it a decision agent there. The business config keeps "hybrid".
AGENT_TYPE = "decision"
PROCESS_NAME = "content-to-campaign"
MODEL_ID = "claude-sonnet-5"
MAX_OUTPUT_TOKENS = 8_000  # spec: per-asset report (contextual pass)
ASSET_TIMEOUT_S = 300.0  # spec: 5 minutes per asset check
PACKAGE_TIMEOUT_S = 1_800.0  # spec: 30 minutes per package
RISK_TIER = "high"  # release gate for market-facing content
DATA_CLASSIFICATION = "confidential"  # pre-release content + reviewer identities
SYSTEM_PROMPT_VERSION = "1.0.0"
