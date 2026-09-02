"""Campaign-in-a-Box Orchestrator — LevelShift Content to Campaign Phase 1, Agent 2.

From an approved campaign brief, assembles the campaign foundation in one pass
(sourced intel, audience & offer pack, reuse-scanned asset checklist, outlines,
back-planned calendar, standardized workspace), routes pack + plan to the Marketing
Lead for confirmation, and — once every checklist asset is content-confirmed —
assembles the Campaign-in-a-Box package manifest via its deterministic packaging
module (no LLM). Model: claude-opus-5 for the planning pass (Azure OpenAI is the
dev/test substitute behind the shared provider interface).
"""

__version__ = "0.1.0"

AGENT_ID = "campaign_in_a_box"
AGENT_TYPE = "orchestrator"
PROCESS_NAME = "content-to-campaign"
MODEL_ID = "claude-opus-5"
MAX_OUTPUT_TOKENS = 16_000
PLANNING_TIMEOUT_S = 1200.0  # 20 minutes per planning pass (spec Timeout)
PACKAGING_TIMEOUT_S = 300.0  # 5 minutes per packaging run (spec Timeout)
RISK_TIER = "medium"
DATA_CLASSIFICATION = "confidential"
SYSTEM_PROMPT_VERSION = "1.0.0"
PACK_TEMPLATE_VERSION = "0.1.0-draft"
