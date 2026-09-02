"""Campaign Identification Agent — LevelShift Content to Campaign Phase 1, Agent 1.

Captures campaign requests (intake form / quarterly plan / event calendar), normalizes
them into a validated, classified campaign brief, and routes the brief to the BU
Campaign Lead for explicit human approval. Model: claude-sonnet-5 (Azure OpenAI is the
dev/test substitute behind the shared provider interface).
"""

__version__ = "0.1.0"

AGENT_ID = "campaign_identification"
AGENT_TYPE = "decision"
PROCESS_NAME = "content-to-campaign"
MODEL_ID = "claude-sonnet-5"
MAX_OUTPUT_TOKENS = 8_000
RUN_TIMEOUT_S = 120.0
RISK_TIER = "medium"
DATA_CLASSIFICATION = "confidential"
SYSTEM_PROMPT_VERSION = "1.0.0"
BRIEF_TEMPLATE_VERSION = "0.1.0-draft"
CONFIDENCE_THRESHOLD = 0.6
MAX_GAP_ROUNDS = 2
