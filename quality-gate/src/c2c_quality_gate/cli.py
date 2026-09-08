"""Dev CLI for the Quality Gate & Approval Agent — local bindings only.

Human decisions (review outcomes, disputes) are carried in as arguments with an
explicit identity; the CLI never fabricates an approver.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from c2c_campaign_box.workspace import LocalCampaignWorkspace
from shiftai_shared.brand import load_brand_rules
from shiftai_shared.config import load_settings
from shiftai_shared.context_store import SqliteContextStore
from shiftai_shared.control_plane import KillSwitch, RateBreaker
from shiftai_shared.llm import build_provider
from shiftai_shared.resilience import SqliteIdempotencyStore
from shiftai_shared.telemetry import JsonlSink

from c2c_quality_gate.agent_config import load_quality_gate_config
from c2c_quality_gate.orchestration import QualityGateAgent, QualityGateDeps

AGENT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = AGENT_ROOT / "config" / "quality_gate.json"


class _PrintSignals:
    """Dev stand-in: outbound signals are printed, never executed."""

    def assets_failed(self, campaign_id: str, findings_by_asset: dict[str, list[str]]) -> None:
        print(f"[signal] assets_failed {campaign_id}: {json.dumps(findings_by_asset)}")

    def package_returned(
        self, campaign_id: str, asset_ids: list[str], notes: str, actor_id: str
    ) -> None:
        print(f"[signal] package_returned {campaign_id}: {asset_ids} by {actor_id}")

    def package_approved(self, campaign_id: str, manifest_version: int) -> None:
        print(f"[signal] package_approved {campaign_id} m{manifest_version}")


def build_agent(workdir: Path, config_path: Path, brand_pack: str) -> QualityGateAgent:
    settings = load_settings()
    workdir.mkdir(parents=True, exist_ok=True)
    return QualityGateAgent(
        QualityGateDeps(
            provider=build_provider(settings),
            store=SqliteContextStore(str(workdir / "context-store.sqlite")),
            workspace=LocalCampaignWorkspace(str(workdir / "box-workspace")),
            sink=JsonlSink(str(workdir / "telemetry.jsonl")),
            kill_switch=KillSwitch(),
            rate_breaker=RateBreaker(window_minutes=60, max_auto_executions=100),
            idempotency=SqliteIdempotencyStore(str(workdir / "idempotency.sqlite")),
            config=load_quality_gate_config(config_path),
            settings=settings,
            brand_rules=load_brand_rules(brand_pack),
            signals=_PrintSignals(),
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="c2c-quality-gate")
    parser.add_argument("--workdir", default=".gate-run")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--brand-pack", default="levelshift")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="gate a campaign's package manifest")
    p_run.add_argument("campaign_id")

    p_sweep = sub.add_parser("sweep", help="SLA reminder/escalation sweep")
    p_sweep.add_argument("campaign_id")

    p_verify = sub.add_parser("verify-locks", help="hash-verify locked versions")
    p_verify.add_argument("campaign_id")

    args = parser.parse_args(argv)
    agent = build_agent(Path(args.workdir), Path(args.config), args.brand_pack)
    if args.command == "run":
        outcome = agent.run_gate(args.campaign_id)
        print(json.dumps(outcome.model_dump(), indent=2))
    elif args.command == "sweep":
        print(json.dumps(agent.sweep(args.campaign_id).model_dump(), indent=2))
    else:
        print(json.dumps({"violated": agent.verify_locks(args.campaign_id)}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
