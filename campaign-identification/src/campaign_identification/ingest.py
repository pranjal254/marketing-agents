"""Uploaded-brief ingestion: .docx, .xlsx, .pdf and .md files become the same
normalized intake text a typed description uses.

Design: the parser is deterministic and does no field guessing — it faithfully
linearizes the requester's own document (headings, paragraphs, tables, sheets)
into compact markdown-ish text, and the existing L1 extraction layer pulls the
fields with quoted provenance exactly as it does for typed text. One code path
for field extraction, no invention here, and a hard text budget so the LLM
prompt stays fast and bounded no matter what is uploaded.
"""

from __future__ import annotations

import io
from collections.abc import Iterable

from pydantic import BaseModel

# Hard stop before any parse work: briefs are text, multi-MB files are exports
# with embedded media and would only add latency.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
# The normalized text budget: keeps the extraction prompt bounded (latency and
# cost) while comfortably fitting any real campaign brief.
TEXT_BUDGET_CHARS = 12_000
_MAX_SHEET_ROWS = 120  # a brief's tables are small; row caps guard against dumps
_MAX_SHEETS = 6
_MAX_CELLS_PER_ROW = 12
_MAX_PDF_PAGES = 30  # briefs are short; page cap bounds extraction latency

SUPPORTED_EXTENSIONS = (".docx", ".xlsx", ".pdf", ".md", ".markdown")

_FORMATS_MESSAGE = (
    "Only Word (.docx), Excel (.xlsx), PDF (.pdf) and Markdown (.md) briefs are "
    "supported. Export or save your brief in one of those formats and upload it again."
)


class BriefUploadError(ValueError):
    """A human-readable reason the file cannot be used as a brief (shown verbatim)."""


class ParsedBrief(BaseModel):
    kind: str  # "docx" | "xlsx" | "pdf" | "md"
    filename: str
    text: str  # normalized text for free_text_context
    paragraphs: int  # docx: paragraphs; xlsx: rows; pdf/md: lines kept
    tables: int  # docx: tables; xlsx: sheets; pdf: pages read; md: 0
    truncated: bool


def parse_brief_document(filename: str, content: bytes) -> ParsedBrief:
    """Parse an uploaded brief into normalized intake text. Raises BriefUploadError
    with a message fit for the requester on anything unusable."""
    name = (filename or "").strip()
    lowered = name.lower()
    if not content:
        raise BriefUploadError("The uploaded file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise BriefUploadError(
            "The file is over 5 MB. A brief that size usually carries embedded "
            "media — save the text itself (.docx, .xlsx, .pdf or .md) and upload that."
        )
    if lowered.endswith(".docx"):
        return _parse_docx(name, content)
    if lowered.endswith(".xlsx"):
        return _parse_xlsx(name, content)
    if lowered.endswith(".pdf"):
        return _parse_pdf(name, content)
    if lowered.endswith((".md", ".markdown")):
        return _parse_markdown(name, content)
    raise BriefUploadError(_FORMATS_MESSAGE)


class _Budget:
    """Accumulates lines under TEXT_BUDGET_CHARS; flags when input was cut."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.used = 0
        self.truncated = False

    def add(self, line: str) -> bool:
        """Append one line; returns False (and marks truncated) once full."""
        text = line.strip()
        if not text:
            return True
        cost = len(text) + 1
        if self.used + cost > TEXT_BUDGET_CHARS:
            self.truncated = True
            return False
        self.lines.append(text)
        self.used += cost
        return True

    def text(self) -> str:
        return "\n".join(self.lines)


def _row_line(cells: Iterable[object]) -> str:
    """One table/sheet row as a compact pipe line; empty rows collapse to ''."""
    values = [str(c).strip() for c in list(cells)[:_MAX_CELLS_PER_ROW] if c is not None]
    values = [v for v in values if v]
    return " | ".join(values) if values else ""


def _parse_docx(filename: str, content: bytes) -> ParsedBrief:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = Document(io.BytesIO(content))
    except Exception as exc:
        raise BriefUploadError(
            "This file could not be read as a Word document. If it is "
            "password-protected or an old .doc file, save it as a plain .docx."
        ) from exc

    budget = _Budget()
    paragraphs = 0
    tables = 0
    # iter_inner_content preserves document order (headings, prose, tables
    # interleaved) — the linearized text reads exactly like the brief.
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue
            style = (block.style.name if block.style is not None else "") or ""
            if style.lower().startswith("heading"):
                level = "".join(ch for ch in style if ch.isdigit()) or "1"
                text = f"{'#' * min(int(level), 4)} {text}"
            elif style.lower() == "title":
                text = f"# {text}"
            if not budget.add(text):
                break
            paragraphs += 1
        elif isinstance(block, Table):
            tables += 1
            for row in block.rows:
                line = _row_line(cell.text for cell in row.cells)
                if line and not budget.add(line):
                    break
            if budget.truncated:
                break

    text = budget.text()
    if not text:
        raise BriefUploadError(
            "No readable text was found in this Word document. If the brief "
            "lives in images or text boxes, paste the text into the description "
            "box instead."
        )
    return ParsedBrief(
        kind="docx",
        filename=filename,
        text=text,
        paragraphs=paragraphs,
        tables=tables,
        truncated=budget.truncated,
    )


def _parse_xlsx(filename: str, content: bytes) -> ParsedBrief:
    import openpyxl

    try:
        # read_only streams rows without materializing the sheet; data_only
        # yields computed values instead of formulas. Macros never execute.
        workbook = openpyxl.load_workbook(
            io.BytesIO(content), read_only=True, data_only=True
        )
    except Exception as exc:
        raise BriefUploadError(
            "This file could not be read as an Excel workbook. If it is "
            "password-protected or an old .xls file, save it as a plain .xlsx."
        ) from exc

    budget = _Budget()
    rows_kept = 0
    sheets_read = 0
    try:
        for sheet in workbook.worksheets:
            if sheets_read >= _MAX_SHEETS:
                budget.truncated = True
                break
            sheets_read += 1
            if not budget.add(f"## Sheet: {sheet.title}"):
                break
            sheet_rows = 0
            for row in sheet.iter_rows(values_only=True):
                if sheet_rows >= _MAX_SHEET_ROWS:
                    budget.truncated = True
                    break
                line = _row_line(row)
                if not line:
                    continue
                if not budget.add(line):
                    break
                sheet_rows += 1
                rows_kept += 1
            if budget.truncated:
                break
    finally:
        workbook.close()

    text = budget.text()
    if rows_kept == 0:
        raise BriefUploadError(
            "No readable rows were found in this workbook. Put the brief's "
            "content in cells (not charts or images) and upload it again."
        )
    return ParsedBrief(
        kind="xlsx",
        filename=filename,
        text=text,
        paragraphs=rows_kept,
        tables=sheets_read,
        truncated=budget.truncated,
    )


def _parse_markdown(filename: str, content: bytes) -> ParsedBrief:
    # Markdown is already the target shape — decode, budget, done. utf-8 first
    # (the overwhelming default), cp1252 fallback so Windows exports never fail.
    try:
        raw = content.decode("utf-8")
    except UnicodeDecodeError:
        raw = content.decode("cp1252", errors="replace")

    budget = _Budget()
    for line in raw.splitlines():
        if not budget.add(line):
            break
    text = budget.text()
    if not text:
        raise BriefUploadError("The Markdown file has no readable text.")
    return ParsedBrief(
        kind="md",
        filename=filename,
        text=text,
        paragraphs=len(budget.lines),
        tables=0,
        truncated=budget.truncated,
    )


def _parse_pdf(filename: str, content: bytes) -> ParsedBrief:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            # A blank owner password is common on exported PDFs; anything else
            # is genuinely locked and needs the requester to unlock it.
            try:
                reader.decrypt("")
            except Exception as exc:
                raise BriefUploadError(
                    "This PDF is password-protected. Remove the password (print "
                    "to PDF works) and upload it again."
                ) from exc
        total_pages = len(reader.pages)
    except BriefUploadError:
        raise
    except Exception as exc:
        raise BriefUploadError(
            "This file could not be read as a PDF. Re-export it and try again."
        ) from exc

    budget = _Budget()
    pages_read = 0
    for page in reader.pages[:_MAX_PDF_PAGES]:
        try:
            page_text = page.extract_text() or ""
        except Exception:  # one broken page never sinks the brief
            page_text = ""
        pages_read += 1
        for line in page_text.splitlines():
            if not budget.add(line):
                break
        if budget.truncated:
            break
    if total_pages > _MAX_PDF_PAGES:
        budget.truncated = True

    text = budget.text()
    if not text:
        raise BriefUploadError(
            "No selectable text was found in this PDF — it looks scanned. "
            "Upload the source document (.docx/.md) or an OCR'd copy instead."
        )
    return ParsedBrief(
        kind="pdf",
        filename=filename,
        text=text,
        paragraphs=len(budget.lines),
        tables=pages_read,
        truncated=budget.truncated,
    )
