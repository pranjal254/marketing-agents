"""Dev runner for the Campaign-in-a-Box Orchestrator.

Production invocation is event-driven via ShiftAI Execution Studio (planning on
brief approval; packaging on all-assets-confirmed); this CLI drives the same agent
locally against the same working directory Agent 1 used, so an approved brief from
Agent 1's store is the natural input.

Usage:
  python -m c2c_campaign_box.cli plan --campaign CAMPAIGN_ID
  python -m c2c_campaign_box.cli confirm --campaign ID --kind pack --actor-id lead@x
  python -m c2c_campaign_box.cli confirm --campaign ID --kind plan --actor-id lead@x
  python -m c2c_campaign_box.cli confirm-asset --campaign ID --asset flagship_blog \
      --file draft.docx --actor-id reviewer@x
  python -m c2c_campaign_box.cli package --campaign ID
  python -m c2c_campaign_box.cli reopen --campaign ID --assets a,b --gate quality-gate \
      --actor-id gate@x
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from shiftai_shared.brand import load_brand_rules
from shiftai_shared.config import SharedSettings, load_settings
from shiftai_shared.context_store import SqliteContextStore
from shiftai_shared.control_plane import KillSwitch, RateBreaker
from shiftai_shared.llm import build_provider
from shiftai_shared.resilience import SqliteIdempotencyStore
from shiftai_shared.semrush import SemrushClient
from shiftai_shared.telemetry import JsonlSink

from c2c_campaign_box.agent_config import load_orchestrator_config
from c2c_campaign_box.intel import IntelSource
from c2c_campaign_box.orchestration import CampaignBoxOrchestrator, OrchestratorDeps
from c2c_campaign_box.repository import LocalRepositoryIndex
from c2c_campaign_box.workspace import LocalCampaignWorkspace

AGENT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = AGENT_ROOT / "config" / "campaign_in_a_box.json"


def intel_source_from_settings(settings: SharedSettings) -> IntelSource | None:
    """SemRush only when a key is configured — otherwise intel-library-only
    fallback mode (flagged in every pack and telemetry record)."""
    if settings.semrush_api_key is None:
        return None
    return SemrushClient(
        settings.semrush_api_key.get_secret_value(), database=settings.semrush_database
    )


def build_orchestrator(workdir: Path) -> CampaignBoxOrchestrator:
    settings = load_settings()
    workdir.mkdir(parents=True, exist_ok=True)
    config = load_orchestrator_config(DEFAULT_CONFIG)
    deps = OrchestratorDeps(
        provider=build_provider(settings),
        store=SqliteContextStore(str(workdir / "context-store.sqlite")),
        workspace=LocalCampaignWorkspace(str(workdir / "workspace")),
        repository=LocalRepositoryIndex(
            str(workdir / "repository"), config.fitness_weights
        ),
        intel_source=intel_source_from_settings(settings),
        sink=JsonlSink(str(workdir / "telemetry.jsonl")),
        kill_switch=KillSwitch(),
        rate_breaker=RateBreaker(window_minutes=60, max_auto_executions=50),
        idempotency=SqliteIdempotencyStore(str(workdir / "idempotency.sqlite")),
        config=config,
        settings=settings,
        brand_rules=load_brand_rules(),
    )
    return CampaignBoxOrchestrator(deps)


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="campaign-in-a-box")
    parser.add_argument("--workdir", default=str(AGENT_ROOT / ".dev-run"))
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="run the planning pass from an approved brief")
    p_plan.add_argument("--campaign", required=True)

    p_confirm = sub.add_parser("confirm", help="Marketing Lead pack/plan confirmation")
    p_confirm.add_argument("--campaign", required=True)
    p_confirm.add_argument("--kind", required=True, choices=["pack", "plan"])
    p_confirm.add_argument("--actor-id", required=True)
    p_confirm.add_argument("--actor-role", default="marketing-lead")
    p_confirm.add_argument("--deltas", default=None, help="path to deltas JSON (modification)")

    p_asset = sub.add_parser(
        "confirm-asset", help="register a content-confirmed asset (Agent 4 stand-in)"
    )
    p_asset.add_argument("--campaign", required=True)
    p_asset.add_argument("--asset", required=True)
    p_asset.add_argument("--file", required=True)
    p_asset.add_argument("--actor-id", required=True)

    p_pkg = sub.add_parser("package", help="run the deterministic packaging module")
    p_pkg.add_argument("--campaign", required=True)

    p_reopen = sub.add_parser("reopen", help="re-open assets after gate findings")
    p_reopen.add_argument("--campaign", required=True)
    p_reopen.add_argument("--assets", required=True, help="comma-separated asset ids")
    p_reopen.add_argument("--gate", required=True)
    p_reopen.add_argument("--actor-id", required=True)

    args = parser.parse_args(argv)
    orchestrator = build_orchestrator(Path(args.workdir))

    if args.command == "plan":
        _print(orchestrator.plan_campaign(args.campaign).model_dump())
    elif args.command == "confirm":
        deltas = (
            json.loads(Path(args.deltas).read_text(encoding="utf-8")) if args.deltas else None
        )
        _print(
            orchestrator.confirm(
                args.campaign,
                args.kind,
                decision="modified" if deltas else "confirmed",
                actor_id=args.actor_id,
                actor_role=args.actor_role,
                deltas=deltas,
            ).model_dump()
        )
    elif args.command == "confirm-asset":
        path = Path(args.file)
        asset = orchestrator.register_confirmed_asset(
            args.campaign,
            args.asset,
            filename=path.name,
            content=path.read_bytes(),
            actor_id=args.actor_id,
        )
        _print(asset.model_dump())
    elif args.command == "package":
        _print(orchestrator.run_packaging(args.campaign).model_dump())
    else:
        _print(
            orchestrator.reopen_assets(
                args.campaign,
                [a.strip() for a in args.assets.split(",") if a.strip()],
                requesting_gate=args.gate,
                actor_id=args.actor_id,
            ).model_dump()
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
