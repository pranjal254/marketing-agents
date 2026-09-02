"""Offline end-to-end demo: complete request -> brief routed -> BU Lead approves;
incomplete request -> targeted gap questions. Scripted mock LLM, no network.

Run:  ..\\.venv\\Scripts\\python samples\\demo_end_to_end.py
(For a real-model dev run use the CLI with LLM_PROVIDER=azure_openai.)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "tests"))

# ruff: noqa: E402  (sys.path bootstrap must precede the test-harness import)

from conftest import InMemoryWorkspace, scripted_provider  # scripted classification JSON
from shiftai_shared.business_capability import load_decision_config
from shiftai_shared.config import SharedSettings
from shiftai_shared.context_store import InMemoryContextStore
from shiftai_shared.control_plane import KillSwitch, RateBreaker
from shiftai_shared.resilience import InMemoryIdempotencyStore
from shiftai_shared.telemetry import InMemorySink

from campaign_identification.orchestration import AgentDeps, CampaignIdentificationAgent


def main() -> None:
    sink = InMemorySink()
    agent = CampaignIdentificationAgent(
        AgentDeps(
            provider=scripted_provider(),
            store=InMemoryContextStore(),
            workspace=InMemoryWorkspace(),
            sink=sink,
            kill_switch=KillSwitch(),
            rate_breaker=RateBreaker(window_minutes=60, max_auto_executions=100),
            idempotency=InMemoryIdempotencyStore(),
            config=load_decision_config(AGENT_ROOT / "config" / "campaign_identification.json"),
            settings=SharedSettings(_env_file=None, LLM_PROVIDER="mock"),
        )
    )

    print("=== 1. Complete request -> validated brief routed for approval ===")
    complete = json.loads(
        (AGENT_ROOT / "samples" / "sample-request-complete.json").read_text(encoding="utf-8")
    )
    outcome = agent.process_request(complete, "form")
    print(f"status={outcome.status}  action={outcome.action_class}  doc_ref={outcome.doc_ref}")
    assert outcome.brief is not None
    print("brief fields:", ", ".join(f.name for f in outcome.brief.fields))
    classification = outcome.brief.classification
    print("classification:", classification.model_dump() if classification else None)

    print("\n=== 2. BU Campaign Lead approves (the human gate) ===")
    final = agent.record_human_decision(
        outcome.case_id,
        "approved",
        actor_role="bu-campaign-lead",
        actor_id="bu.lead@levelshift.com",
    )
    print(f"final status={final.status}")

    print("\n=== 3. Incomplete request -> targeted gap questions, held awaiting_input ===")
    incomplete = json.loads(
        (AGENT_ROOT / "samples" / "sample-request-incomplete.json").read_text(encoding="utf-8")
    )
    gap_outcome = agent.process_request(incomplete, "form")
    print(f"status={gap_outcome.status}")
    assert gap_outcome.gap_request is not None
    for q in gap_outcome.gap_request.questions[:3]:
        print(f"  gap -> {q.field}: {q.question}")

    print("\n=== 4. Same request as (1) again -> fresh duplicate flagged for a human ===")
    dup = agent.process_request(complete, "form")
    print(f"status={dup.status}  action={dup.action_class}  reason={dup.escalation_reason}")

    print("\n=== STS v2 telemetry - approved case journey (every record schema-valid) ===")
    for r in sink.records:
        if r["shiftai.case.id"] != outcome.case_id:
            continue
        line = f"{r['shiftai.event.type']:15s}"
        if r["shiftai.event.type"] == "decision_made":
            line += (
                f" L{r.get('shiftai.decision.layer')}"
                f" class={r.get('shiftai.decision.action_class')}"
                f" conf={r.get('shiftai.decision.confidence')}"
            )
            if "shiftai.cost.amount" in r:
                line += f" cost=${r['shiftai.cost.amount']}"
        elif r["shiftai.event.type"] == "action_taken":
            line += f" key={r.get('shiftai.action.idempotency_key')}"
        elif r["shiftai.event.type"] == "human_gate":
            line += (
                f" decision={r.get('shiftai.hitl.decision')} label={r.get('shiftai.learn.label')}"
                f" scenario={r.get('shiftai.learn.scenario_hash')}"
            )
        elif r["shiftai.event.type"] in ("case_resolved", "run_summary"):
            line += f" outcome={r.get('shiftai.outcome')}"
        print(line)


if __name__ == "__main__":
    main()
