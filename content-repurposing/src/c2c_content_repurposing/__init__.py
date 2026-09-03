"""Content Repurposing Agent — LevelShift Content to Campaign Phase 1, Agent 3.

The production engine of Phase 1: drafts one flagship asset per campaign from the
approved outline and audience & offer pack (sourced claims only — unverifiable
sections become gap notes, never plausible prose), stages it for the human review
cycle, and — only after a human confirms the flagship — fans out channel-native
derivatives from the flagship's verified claim inventory, recording claim lineage
per derivative. Model: claude-opus-5 (Azure OpenAI is the dev/test substitute
behind the shared provider interface). This agent writes drafts into the campaign
workspace only; no publish/post/send code path exists anywhere in the package.
"""

__version__ = "0.1.0"

AGENT_ID = "content_repurposing"
AGENT_TYPE = "decision"
PROCESS_NAME = "content-to-campaign"
MODEL_ID = "claude-opus-5"
FLAGSHIP_MAX_TOKENS = 32_000  # spec: 32K flagship, streaming
DERIVATIVE_MAX_TOKENS = 8_000  # spec: 8K per derivative
FLAGSHIP_TIMEOUT_S = 1200.0  # 20 minutes flagship (spec Timeout)
DERIVATIVE_TIMEOUT_S = 300.0  # 5 minutes per derivative (spec Timeout)
FANOUT_TIMEOUT_S = 2700.0  # 45 minutes full fan-out run (spec Timeout)
RISK_TIER = "medium"
DATA_CLASSIFICATION = "confidential"
SYSTEM_PROMPT_VERSION = "1.0.0"
