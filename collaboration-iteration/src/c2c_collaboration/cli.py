"""Local dev runner for the review cycle. Every gate below carries YOUR identity —
the agent never confirms or resolves anything on its own.

  python -m c2c_collaboration.cli stage    <workdir> <cid> <asset_id>
  python -m c2c_collaboration.cli feedback <workdir> <cid> <asset_id> <email> <role> <text...>
  python -m c2c_collaboration.cli round    <workdir> <cid> <asset_id> <email>
  python -m c2c_collaboration.cli resolve  <workdir> <cid> <asset_id> <conflict> <email> <text...>
  python -m c2c_collaboration.cli confirm  <workdir> <cid> <asset_id> <email> <role>
  python -m c2c_collaboration.cli sweep    <workdir>

``workdir`` must be a bridge session directory. CLI signals print instead of
routing (the bridge binds the real neighbors).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from c2c_campaign_box.workspace import LocalCampaignWorkspace
from shiftai_shared.brand import load_brand_rules
from shiftai_shared.config import load_settings
from shiftai_shared.context_store import SqliteContextStore
from shiftai_shared.control_plane import KillSwitch, RateBreaker
from shiftai_shared.llm import build_provider
from shiftai_shared.resilience import SqliteIdempotencyStore
from shiftai_shared.telemetry import JsonlSink

from c2c_collaboration.agent_config import load_collaboration_config
from c2c_collaboration.orchestration import CollaborationAgent, CollaborationDeps

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "collaboration_iteration.json"


class _PrintSignals:
    """CLI stand-in: signals are printed, not routed (the bridge binds neighbors)."""

    def flagship_confirmed(self, campaign_id: str, actor_id: str, actor_role: str) -> None:
        print(f"[signal] flagship_confirmed {campaign_id} by {actor_id} ({actor_role})")

    def register_confirmed(
        self, campaign_id: str, asset_id: str, actor_id: str, actor_role: str
    ) -> None:
        print(f"[signal] register_confirmed {campaign_id}:{asset_id} by {actor_id}")

    def route_rework(
        self, campaign_id: str, asset_id: str, instruction: str, actor_id: str
    ) -> None:
        print(f"[signal] route_rework {campaign_id}:{asset_id}: {instruction[:120]}")


def build_agent(workdir: Path) -> CollaborationAgent:
    settings = load_settings()
    return CollaborationAgent(
        CollaborationDeps(
            provider=build_provider(settings),
            store=SqliteContextStore(str(workdir / "context-store.sqlite")),
            workspace=LocalCampaignWorkspace(str(workdir / "box-workspace")),
            sink=JsonlSink(str(workdir / "telemetry.jsonl")),
            kill_switch=KillSwitch(),
            rate_breaker=RateBreaker(window_minutes=60, max_auto_executions=100),
            idempotency=SqliteIdempotencyStore(str(workdir / "idempotency.sqlite")),
            config=load_collaboration_config(CONFIG_PATH),
            settings=settings,
            brand_rules=load_brand_rules(),
            signals=_PrintSignals(),
        )
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    command, workdir = argv[0], Path(argv[1])
    agent = build_agent(workdir)
    out: Any
    if command == "stage" and len(argv) >= 4:
        out = agent.on_draft_staged(argv[2], argv[3])
    elif command == "feedback" and len(argv) >= 7:
        out = agent.add_feedback(argv[2], argv[3], reviewer_id=argv[4],
                                 reviewer_role=argv[5], text=" ".join(argv[6:]))
    elif command == "round" and len(argv) >= 5:
        out = agent.run_review_round(argv[2], argv[3], actor_id=argv[4])
    elif command == "resolve" and len(argv) >= 7:
        out = agent.resolve_conflict(argv[2], argv[3], argv[4], actor_id=argv[5],
                                     decision=" ".join(argv[6:]))
    elif command == "confirm" and len(argv) >= 6:
        out = agent.confirm_content(argv[2], argv[3], actor_id=argv[4], actor_role=argv[5])
    elif command == "sweep":
        out = agent.sweep()
    else:
        print(__doc__)
        return 2
    print(json.dumps(out.model_dump(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
