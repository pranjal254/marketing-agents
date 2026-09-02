"""Dev runner for the Campaign Identification Agent.

Production invocation is event-driven via ShiftAI Execution Studio; this CLI drives
the same agent locally: process a request end-to-end, answer gaps, and record the
BU Campaign Lead decision. Provider comes from LLM_PROVIDER (mock | azure_openai |
anthropic) — dev environments use azure_openai (no Claude available in dev).

Usage:
  python -m campaign_identification.cli process --request sample.json [--source form]
  python -m campaign_identification.cli answer-gaps --case ID --answers a.json --actor-id someone
  python -m campaign_identification.cli approve --case CASE_ID --actor-id bu.lead@levelshift.com
  python -m campaign_identification.cli reject  --case CASE_ID --actor-id bu.lead@levelshift.com
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from shiftai_shared.business_capability import load_decision_config
from shiftai_shared.config import load_settings
from shiftai_shared.context_store import SqliteContextStore
from shiftai_shared.control_plane import KillSwitch, RateBreaker
from shiftai_shared.llm import build_provider
from shiftai_shared.resilience import SqliteIdempotencyStore
from shiftai_shared.telemetry import JsonlSink

from campaign_identification.orchestration import AgentDeps, CampaignIdentificationAgent
from campaign_identification.persistence import LocalWorkspace

AGENT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = AGENT_ROOT / "config" / "campaign_identification.json"


def build_agent(workdir: Path) -> CampaignIdentificationAgent:
    settings = load_settings()
    workdir.mkdir(parents=True, exist_ok=True)
    deps = AgentDeps(
        provider=build_provider(settings),
        store=SqliteContextStore(str(workdir / "context-store.sqlite")),
        workspace=LocalWorkspace(str(workdir / "workspace")),
        sink=JsonlSink(str(workdir / "telemetry.jsonl")),
        kill_switch=KillSwitch(),
        rate_breaker=RateBreaker(window_minutes=60, max_auto_executions=50),
        idempotency=SqliteIdempotencyStore(str(workdir / "idempotency.sqlite")),
        config=load_decision_config(DEFAULT_CONFIG),
        settings=settings,
    )
    return CampaignIdentificationAgent(deps)


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="campaign-identification")
    parser.add_argument("--workdir", default=str(AGENT_ROOT / ".dev-run"))
    sub = parser.add_subparsers(dest="command", required=True)

    p_process = sub.add_parser("process", help="process one campaign request")
    p_process.add_argument("--request", required=True, help="path to request JSON")
    p_process.add_argument(
        "--source", default="form", choices=["form", "plan", "calendar", "adhoc"]
    )

    p_answers = sub.add_parser("answer-gaps", help="submit requester gap answers")
    p_answers.add_argument("--case", required=True)
    p_answers.add_argument("--answers", required=True, help="path to answers JSON")
    p_answers.add_argument("--actor-id", required=True)

    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"{name} a routed brief (BU Campaign Lead)")
        p.add_argument("--case", required=True)
        p.add_argument("--actor-id", required=True)
        p.add_argument("--actor-role", default="bu-campaign-lead")
        p.add_argument("--notes", default=None)

    args = parser.parse_args(argv)
    agent = build_agent(Path(args.workdir))

    if args.command == "process":
        raw = json.loads(Path(args.request).read_text(encoding="utf-8"))
        outcome = agent.process_request(raw, args.source)
        _print(outcome.model_dump())
    elif args.command == "answer-gaps":
        answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))
        outcome = agent.submit_gap_answers(args.case, answers, actor_id=args.actor_id)
        _print(outcome.model_dump())
    else:
        outcome = agent.record_human_decision(
            args.case,
            "approved" if args.command == "approve" else "rejected",
            actor_role=args.actor_role,
            actor_id=args.actor_id,
            notes=args.notes,
        )
        _print(outcome.model_dump())
    return 0


if __name__ == "__main__":
    sys.exit(main())
