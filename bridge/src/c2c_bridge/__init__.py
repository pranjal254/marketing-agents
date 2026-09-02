"""Local dev bridge for the Campaign Identification agent.

Plays ShiftAI Execution Studio's role on a laptop: HTTP endpoints for intake, gap
answers and the human approval gate, plus a live SSE feed of STS v2 telemetry so the
Marketing Studio UI can show agents in action. Dev tool only — production invocation
and task routing belong to Execution Studio.
"""

__version__ = "0.1.0"
