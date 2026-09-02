"""LevelShift brand rules pack — versioned, read-only Business Capability content.

Derived from the marketing-owned brand documents (see ``sources`` in the JSON);
the JSON is the committed, versioned artifact — the source .docx/.pdf stay out of
git. Consumed by Agents 2, 3 and 5 for pack/outline language rules, and rendered
as a cacheable system-prompt block (Cross-Agent Standard A: prompt caching on the
rules pack and brand guidelines).

``lint_text`` is the deterministic language check (no LLM): terminology violations
and urgency/fear phrasing are errors; overuse/avoid words are warnings for the
human reviewer. It never edits text — it flags (Agent 5 pattern).
"""

from __future__ import annotations

import json
import re
from importlib import resources
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BRAND_RULES_VERSION = "0.1.0-draft"
_RULES_FILE = "rules_v0_1_0.json"


class VoiceRule(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    label: str
    do: str
    dont: str


class WordChoice(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    overuse_flagged: list[str] = Field(alias="overuseFlagged")
    avoid_terms: list[str] = Field(alias="avoidTerms")
    banned_terms: list[str] = Field(alias="bannedTerms")
    urgency_fear_flagged: list[str] = Field(alias="urgencyFearFlagged")


class TerminologyRule(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    rule: str


class Persona(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    id: str
    title: str
    pains: str
    key_message: str = Field(alias="keyMessage")


class Playbook(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    purpose: str
    structure: str
    voice: str
    word_count: str | None = Field(default=None, alias="wordCount")


class BrandRules(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="ignore")

    rules_pack_id: str = Field(alias="rulesPackId")
    version: str
    status: str
    sources: list[str]
    positioning: str
    tone: str
    voice: list[VoiceRule]
    word_choice: WordChoice = Field(alias="wordChoice")
    terminology: list[TerminologyRule]
    content_self_check: list[str] = Field(alias="contentSelfCheck")
    playbooks: dict[str, Playbook]
    personas: list[Persona]
    credentials: list[str]


def load_brand_rules() -> BrandRules:
    """Load + validate the packaged rules JSON. Read-only — no save exists."""
    raw = resources.files("shiftai_shared").joinpath(f"brand/{_RULES_FILE}").read_text("utf-8")
    return BrandRules.model_validate(json.loads(raw))


LintSeverity = Literal["error", "warning"]


class LintFinding(BaseModel):
    model_config = ConfigDict(frozen=True)
    rule_id: str
    severity: LintSeverity
    term: str
    detail: str


def _find_term(text_lower: str, term: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])", text_lower) is not None


def lint_text(text: str, rules: BrandRules) -> list[LintFinding]:
    """Deterministic brand-language check. Flags, never edits."""
    findings: list[LintFinding] = []
    lower = text.lower()

    # Terminology: "ShiftAI" is one word.
    if re.search(r"\bshift[\s-]+ai\b", lower):
        findings.append(
            LintFinding(
                rule_id="shiftai_one_word",
                severity="error",
                term="Shift AI",
                detail="Write 'ShiftAI' as one word.",
            )
        )
    # Copilot is D365-cloud-only.
    if "copilot" in lower and re.search(r"\bon[\s-]?prem(ise|ises)?\b", lower):
        findings.append(
            LintFinding(
                rule_id="copilot_d365_cloud_only",
                severity="error",
                term="Copilot + on-premise",
                detail="Copilot is D365-cloud-only; never position it for on-premise scenarios.",
            )
        )
    # BC/F&O independence: co-mention in one text is flagged for human review.
    bc = re.search(r"\bbusiness central\b|\bbc\b", lower) is not None
    fo = re.search(r"\bf&o\b|\bfinance (and|&) operations\b", lower) is not None
    if bc and fo:
        findings.append(
            LintFinding(
                rule_id="bc_fo_independent",
                severity="error",
                term="Business Central + F&O",
                detail="BC and F&O are strictly independent; never combine them in one claim.",
            )
        )

    for term in rules.word_choice.banned_terms:
        if _find_term(lower, term):
            findings.append(
                LintFinding(
                    rule_id="banned_term", severity="error", term=term, detail="Banned term."
                )
            )
    for term in rules.word_choice.urgency_fear_flagged:
        if _find_term(lower, term):
            findings.append(
                LintFinding(
                    rule_id="urgency_fear",
                    severity="error",
                    term=term,
                    detail="No urgency or fear framing (spec tone rule).",
                )
            )
    for term in rules.word_choice.overuse_flagged:
        if _find_term(lower, term):
            findings.append(
                LintFinding(
                    rule_id="overuse_term",
                    severity="warning",
                    term=term,
                    detail="Flagged as overused in the brand playbook.",
                )
            )
    for term in rules.word_choice.avoid_terms:
        if _find_term(lower, term):
            findings.append(
                LintFinding(
                    rule_id="avoid_term",
                    severity="warning",
                    term=term,
                    detail="The brand playbook says to avoid this term.",
                )
            )
    return findings


def brand_prompt_block(rules: BrandRules) -> str:
    """Render the rules pack as a stable, cacheable system-prompt block."""
    voice = "\n".join(f"- {v.label}: DO {v.do} DON'T {v.dont}" for v in rules.voice)
    terminology = "\n".join(f"- {t.rule}" for t in rules.terminology)
    personas = "\n".join(
        f"- {p.title}: pains: {p.pains} message: {p.key_message}" for p in rules.personas
    )
    checks = "\n".join(f"- {q}" for q in rules.content_self_check)
    words = rules.word_choice
    return (
        f"LevelShift Brand Rules (pack {rules.rules_pack_id} v{rules.version}).\n"
        f"Positioning: {rules.positioning}\n"
        f"Tone: {rules.tone}\n"
        f"Voice:\n{voice}\n"
        f"Terminology rules:\n{terminology}\n"
        f"Word choice: do not overuse {', '.join(words.overuse_flagged)}; "
        f"avoid {', '.join(words.avoid_terms)}; no urgency/fear phrases such as "
        f"{', '.join(words.urgency_fear_flagged)}.\n"
        f"Buyer personas:\n{personas}\n"
        f"Content self-check:\n{checks}"
    )
