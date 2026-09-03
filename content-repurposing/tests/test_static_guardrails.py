"""Static guardrails, enforced as tests over the package SOURCE (like the shared
plane-isolation test): the agent has no publish/post/send surface and no
destructive workspace operations — structurally, not by convention."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "c2c_content_repurposing"

# Guardrail 5: never publishes, posts or sends anything, anywhere. No HTTP client,
# no mail, no social/marketing SDK may even be imported in this package.
FORBIDDEN_IMPORTS = re.compile(
    r"^\s*(import|from)\s+(requests|httpx|urllib3|aiohttp|smtplib|slack_sdk|tweepy|"
    r"linkedin|salesforce|simple_salesforce|pardot)\b",
    re.MULTILINE,
)

# Drafts are additive: no delete / move / overwrite primitives anywhere.
FORBIDDEN_CALLS = re.compile(
    r"\b(os\.remove|os\.unlink|os\.rename|shutil\.|\.unlink\(|\.rmdir\(|write_text\(|"
    r"write_bytes\()"
)


def _sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in SRC.rglob("*.py")}


def test_no_publish_or_send_surface_exists() -> None:
    for name, text in _sources().items():
        assert not FORBIDDEN_IMPORTS.search(text), f"forbidden connector import in {name}"


def test_no_destructive_file_operations() -> None:
    for name, text in _sources().items():
        assert not FORBIDDEN_CALLS.search(text), f"destructive file operation in {name}"


def test_workspace_writes_go_through_the_additive_protocol_only() -> None:
    """Every workspace side effect flows through ``workspace.upload`` (additive,
    conflict = fail) behind the idempotency store — the only write call sites are
    the orchestration module's ``_upload_once``."""
    sources = _sources()
    for name, text in sources.items():
        for match in re.finditer(r"workspace\.upload\(", text):
            assert name == "orchestration.py", (
                f"direct workspace.upload outside orchestration in {name} "
                f"(offset {match.start()})"
            )


def test_confirm_flagship_is_never_called_by_the_agent_itself() -> None:
    """Flagship-first sequencing is a state machine, not a convention: no module
    in the package invokes the human confirmation gate."""
    for name, text in _sources().items():
        stripped = re.sub(r'""".*?"""', "", text, flags=re.DOTALL)  # ignore docstrings
        calls = re.findall(r"(?:self\.)?confirm_flagship\(", stripped)
        if name == "orchestration.py":
            # only the definition exists, no self-invocation
            assert "self.confirm_flagship(" not in stripped
        elif name == "cli.py":
            # the CLI carries a HUMAN's identity into the gate — allowed
            continue
        else:
            assert not calls, f"confirm_flagship invoked in {name}"


def test_verbatim_system_prompt_is_versioned_and_untouched() -> None:
    prompt = (
        Path(__file__).resolve().parents[1]
        / "prompts"
        / "content-repurposing.system.v1.0.0.md"
    ).read_text(encoding="utf-8")
    # Spot-check the spec's exact language survived verbatim.
    assert "You are the Content Repurposing Agent" in prompt
    assert "leave a gap note instead of writing plausible content" in prompt
    assert "you never publish, post or send anything, anywhere" in prompt
    assert '"ShiftAI" always one word' in prompt
