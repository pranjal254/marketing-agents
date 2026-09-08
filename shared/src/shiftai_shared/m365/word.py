"""Deterministic Word (.docx) document builder, no LLM anywhere in this module.

Generic building blocks only: a branded cover, numbered sections with intros,
key-value tables, N-column grids, and checklists. Which sections a given
document has (the domain template) lives in each agent package.

House style is enforced here as a safety net: em and en dashes are stripped from
every rendered string (brand rule no_em_dash), so a stray dash from any upstream
text never reaches a delivered document.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from importlib import resources

# House-style safety net: collapse any em/en dash (with surrounding spaces) to a
# comma; the brand rule bans the characters outright.
_DASH = re.compile(rf"\s*[{chr(0x2014)}{chr(0x2013)}]\s*")


def _clean(text: str) -> str:
    return _DASH.sub(", ", str(text))


def brand_logo_png() -> bytes | None:
    """The committed LevelShift wordmark for document headers, or None if the
    asset is unavailable (documents still render, just without the mark)."""
    try:
        return (
            resources.files("shiftai_shared")
            .joinpath("brand/assets/levelshift-wordmark.png")
            .read_bytes()
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None


@dataclass(frozen=True)
class DocSection:
    heading: str
    number: str | None = None  # "01", "02", … shown before the heading
    intro: str | None = None  # one grey lead-in line under the heading
    paragraphs: tuple[str, ...] = ()
    table_rows: tuple[tuple[str, str], ...] = ()  # (label, value) pairs
    table_header: tuple[str, str] | None = None
    # N-column grid (e.g. approval records, deliverables, metrics). Takes
    # precedence over the 2-col table_rows when present.
    columns: tuple[str, ...] = ()
    grid: tuple[tuple[str, ...], ...] = ()
    checklist: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocSpec:
    title: str
    subtitle: str | None = None
    kicker: str | None = None  # small uppercase line above the title
    meta: tuple[tuple[str, str], ...] = ()  # cover key/value pairs (borderless)
    sections: tuple[DocSection, ...] = field(default_factory=tuple)
    accent_hex: str = "080A52"  # Deep Phthalo Blue (LevelShift primary)
    logo: bool = True  # place the brand mark at the top of the cover


def _shade(cell: object, hex_fill: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tc_pr = cell._tc.get_or_add_tcPr()  # type: ignore[attr-defined]
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tc_pr.append(shd)


def _color_runs(paragraph: object, hex_color: str, *, bold: bool = False) -> None:
    from docx.shared import RGBColor

    rgb = RGBColor.from_string(hex_color)
    for run in paragraph.runs:  # type: ignore[attr-defined]
        run.font.color.rgb = rgb
        if bold:
            run.font.bold = True


def build_docx(spec: DocSpec) -> bytes:
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor

    accent = spec.accent_hex
    doc = Document()

    if spec.logo:
        logo = brand_logo_png()
        if logo is not None:
            # The LevelShift wordmark is a wide banner (~5.8:1); size it by width
            # so it reads as a header lockup, not a stretched square.
            para = doc.add_paragraph()
            para.add_run().add_picture(io.BytesIO(logo), width=Inches(1.9))

    if spec.kicker:
        kicker = doc.add_paragraph(_clean(spec.kicker).upper())
        for run in kicker.runs:
            run.font.size = Pt(9)
            run.font.bold = True
            run.font.color.rgb = RGBColor.from_string("009ADA")  # Electric Blue

    title = doc.add_heading(_clean(spec.title), level=0)
    _color_runs(title, accent, bold=True)
    if spec.subtitle:
        sub = doc.add_paragraph(_clean(spec.subtitle))
        for run in sub.runs:
            run.font.size = Pt(10)
            run.font.color.rgb = RGBColor.from_string("475467")

    # Cover meta: a clean borderless two-column key/value block.
    if spec.meta:
        meta_table = doc.add_table(rows=len(spec.meta), cols=2)
        for i, (label, value) in enumerate(spec.meta):
            lc = meta_table.cell(i, 0)
            lc.text = _clean(label).upper()
            for run in lc.paragraphs[0].runs:
                run.font.size = Pt(8)
                run.font.bold = True
                run.font.color.rgb = RGBColor.from_string("667085")
            meta_table.cell(i, 1).text = _clean(value)

    for section in spec.sections:
        head_text = f"{section.number}  {section.heading}" if section.number else section.heading
        heading = doc.add_heading(_clean(head_text), level=1)
        _color_runs(heading, accent, bold=True)
        if section.intro:
            intro = doc.add_paragraph(_clean(section.intro))
            for run in intro.runs:
                run.font.italic = True
                run.font.size = Pt(9.5)
                run.font.color.rgb = RGBColor.from_string("667085")
        for text in section.paragraphs:
            doc.add_paragraph(_clean(text))

        if section.columns and section.grid:
            n_cols = len(section.columns)
            table = doc.add_table(rows=len(section.grid) + 1, cols=n_cols)
            table.style = "Table Grid"
            for c, label in enumerate(section.columns):
                cell = table.cell(0, c)
                cell.text = _clean(label)
                _shade(cell, accent)
                for run in cell.paragraphs[0].runs:
                    run.font.bold = True
                    run.font.color.rgb = RGBColor.from_string("FFFFFF")
                    run.font.size = Pt(9)
            for r, row in enumerate(section.grid):
                for c in range(n_cols):
                    table.cell(r + 1, c).text = _clean(row[c]) if c < len(row) else ""
        elif section.table_rows:
            n_rows = len(section.table_rows) + (1 if section.table_header else 0)
            table = doc.add_table(rows=n_rows, cols=2)
            table.style = "Table Grid"
            offset = 0
            if section.table_header:
                for c in (0, 1):
                    cell = table.cell(0, c)
                    cell.text = _clean(section.table_header[c])
                    _shade(cell, accent)
                    for run in cell.paragraphs[0].runs:
                        run.font.bold = True
                        run.font.color.rgb = RGBColor.from_string("FFFFFF")
                        run.font.size = Pt(9)
                offset = 1
            for i, (label, value) in enumerate(section.table_rows):
                table.cell(i + offset, 0).text = _clean(label)
                table.cell(i + offset, 1).text = _clean(value)

        for item in section.checklist:
            para = doc.add_paragraph(f"☐  {_clean(item)}")
            para.paragraph_format.space_after = Pt(2)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
