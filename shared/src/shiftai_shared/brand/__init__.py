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

BRAND_RULES_VERSION = "0.2.0"
_RULES_FILE = "rules_v0_2_0.json"

# One committed JSON per business-unit brand. The active pack is a deployment
# decision (bridge/CLI reads C2C_BRAND_PACK) — agent code stays pack-agnostic.
BRAND_PACKS: dict[str, str] = {
    "levelshift": _RULES_FILE,
    "demandblue": "rules_demandblue_v0_1_0.json",
}
DEFAULT_BRAND_PACK = "levelshift"


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


class Practice(BaseModel):
    """One LevelShift practice from the Brand Playbook's practice-messaging
    section: marketing-approved positioning AND proof points — the committed,
    versioned claim source content generation may cite without a local file
    repository."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)
    id: str
    name: str
    tagline: str
    offer: str
    pain: str
    benefit: str
    proof_point: str = Field(alias="proofPoint")


class BrandRules(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="ignore")

    rules_pack_id: str = Field(alias="rulesPackId")
    brand_name: str = Field(default="LevelShift", alias="brandName")
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
    # v0.2.0 additions (older packs default to empty — backward compatible).
    style_rules: list[str] = Field(default_factory=list, alias="styleRules")
    industries: list[str] = Field(default_factory=list)
    practices: list[Practice] = Field(default_factory=list)


def load_brand_rules(pack: str = DEFAULT_BRAND_PACK) -> BrandRules:
    """Load + validate a packaged rules JSON. Read-only — no save exists.

    ``pack`` selects the business-unit brand pack (see ``BRAND_PACKS``); an
    unknown name fails loudly rather than silently falling back."""
    name = pack.strip().lower() or DEFAULT_BRAND_PACK
    filename = BRAND_PACKS.get(name)
    if filename is None:
        raise ValueError(f"unknown brand pack {pack!r}; available: {sorted(BRAND_PACKS)}")
    raw = resources.files("shiftai_shared").joinpath(f"brand/{filename}").read_text("utf-8")
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

    # Style: em/en dashes are banned in generated content (house style) — the
    # LLM-favorite punctuation is the clearest "generated" tell.
    dash = next((d for d in (chr(0x2014), chr(0x2013)) if d in text), None)
    if dash is not None:
        findings.append(
            LintFinding(
                rule_id="no_em_dash",
                severity="error",
                term=dash,
                detail="Em/en dashes are banned; use commas, colons, or separate sentences.",
            )
        )
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
    block = (
        f"{rules.brand_name} Brand Rules (pack {rules.rules_pack_id} v{rules.version}).\n"
        f"Positioning: {rules.positioning}\n"
        f"Tone: {rules.tone}\n"
        f"Voice:\n{voice}\n"
        f"Terminology rules:\n{terminology}\n"
        f"Word choice: do not overuse {', '.join(words.overuse_flagged)}; "
        f"avoid {', '.join(words.avoid_terms)}; no urgency/fear phrases such as "
        f"{', '.join(words.urgency_fear_flagged)}.\n"
    )
    if rules.style_rules:
        block += "Style rules (hard requirements):\n" + "\n".join(
            f"- {s}" for s in rules.style_rules
        ) + "\n"
    if rules.industries:
        block += f"Industries served: {', '.join(rules.industries)}.\n"
    if rules.practices:
        practices = "\n".join(
            f"- {p.name}: {p.tagline} Offer: {p.offer} Pain solved: {p.pain} "
            f"Benefit: {p.benefit} Approved proof point: {p.proof_point}"
            for p in rules.practices
        )
        block += (
            "Practices (marketing-approved positioning and proof points — the ONLY "
            "pre-approved factual claims; cite them as source 'brand-playbook:"
            "<practice id>'):\n" + practices + "\n"
        )
    block += f"Buyer personas:\n{personas}\nContent self-check:\n{checks}"
    return block
