"""Local dev runner: drive one campaign through flagship → confirm → fan-out
against the local bindings (the confirm step is YOU — the gate stays human even
on the command line).

  python -m c2c_content_repurposing.cli flagship  <workdir> <campaign_id>
  python -m c2c_content_repurposing.cli confirm   <workdir> <campaign_id> <actor_email>
  python -m c2c_content_repurposing.cli fanout    <workdir> <campaign_id>

``workdir`` must be a bridge session directory (context-store.sqlite +
box-workspace) so the agent reads the plan Agent 2 produced.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from c2c_campaign_box.workspace import LocalCampaignWorkspace
from shiftai_shared.brand import load_brand_rules
from shiftai_shared.config import load_settings
from shiftai_shared.context_store import SqliteContextStore
from shiftai_shared.control_plane import KillSwitch, RateBreaker
from shiftai_shared.llm import build_provider
from shiftai_shared.resilience import SqliteIdempotencyStore
from shiftai_shared.telemetry import JsonlSink

from c2c_content_repurposing.agent_config import load_repurposing_config
from c2c_content_repurposing.orchestration import ContentRepurposingAgent, RepurposingDeps

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "content_repurposing.json"


def build_agent(workdir: Path) -> ContentRepurposingAgent:
    settings = load_settings()
    return ContentRepurposingAgent(
        RepurposingDeps(
            provider=build_provider(settings),
            store=SqliteContextStore(str(workdir / "context-store.sqlite")),
            workspace=LocalCampaignWorkspace(str(workdir / "box-workspace")),
            sink=JsonlSink(str(workdir / "telemetry.jsonl")),
            kill_switch=KillSwitch(),
            rate_breaker=RateBreaker(window_minutes=60, max_auto_executions=100),
            idempotency=SqliteIdempotencyStore(str(workdir / "idempotency.sqlite")),
            config=load_repurposing_config(CONFIG_PATH),
            settings=settings,
            brand_rules=load_brand_rules(),
        )
    )


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    command, workdir_arg, campaign_id = argv[0], argv[1], argv[2]
    agent = build_agent(Path(workdir_arg))
    if command == "flagship":
        outcome = agent.draft_flagship(campaign_id)
    elif command == "confirm":
        if len(argv) < 4:
            print("confirm needs <actor_email>")
            return 2
        outcome = agent.confirm_flagship(campaign_id, actor_id=argv[3])
    elif command == "fanout":
        outcome = agent.run_fanout(campaign_id)  # type: ignore[assignment]
    else:
        print(f"unknown command {command!r}")
        return 2
    print(json.dumps(outcome.model_dump(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
