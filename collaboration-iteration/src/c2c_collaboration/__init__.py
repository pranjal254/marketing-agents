"""Content Collaboration & Iteration Agent — LevelShift Content to Campaign
Phase 1, Agent 4.

Runs the human-in-the-loop review cycle between staged drafts and
content-confirmed: assigns reviewers from the workflow plan, consolidates
fragmented feedback into one de-duplicated, de-conflicted instruction set,
applies agreed textual revisions as new tracked versions (claim→source markers
protected in code), routes structural rework back to the Content Repurposing
Agent, and keeps a clean version history and iteration state per asset. Every
editorial decision stays human: conflicts are surfaced (never adjudicated) and
``content_confirmed`` exists only as a human action — no code path in this
package sets it autonomously. Model: claude-sonnet-5 (Azure OpenAI is the
dev/test substitute behind the shared provider interface).
"""

__version__ = "0.1.0"

AGENT_ID = "collaboration_iteration"
AGENT_TYPE = "decision"
PROCESS_NAME = "content-to-campaign"
MODEL_ID = "claude-sonnet-5"
MAX_OUTPUT_TOKENS = 16_000  # spec
RUN_TIMEOUT_S = 600.0  # 10 minutes per revision-application run (spec Timeout)
RISK_TIER = "medium"
DATA_CLASSIFICATION = "confidential"  # internal reviewer commentary — never quoted outside
SYSTEM_PROMPT_VERSION = "1.0.0"
