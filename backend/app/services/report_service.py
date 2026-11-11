"""Report assembly and export (Markdown, PDF, DOCX)."""
from __future__ import annotations

import io
import re
from pathlib import Path

import markdown as md
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from fpdf import FPDF
from fpdf.fonts import FontFace

from ..schemas import Reference, Report, ReportSection

SECTION_HEADINGS: list[tuple[str, str]] = [
    ("executive_summary", "Executive Summary"),
    ("research_objectives", "Research Objectives"),
    ("research_methodology", "Research Methodology"),
    ("detailed_findings", "Detailed Findings"),
    ("analysis_and_discussion", "Analysis and Discussion"),
    ("contradictory_evidence", "Contradictory Evidence"),
    ("limitations", "Limitations"),
    ("conclusion", "Conclusion"),
]


def _reference_line(ref: Reference) -> str:
    details = ", ".join(x for x in (ref.publisher, ref.published_date) if x)
    return f"{details + '. ' if details else ''}Retrieved {ref.retrieved_date}."


def build_markdown(title: str, sections: list[ReportSection], references: list[Reference]) -> str:
    parts = [f"# {title}"]
    parts += [f"## {s.heading}\n\n{s.content.strip()}" for s in sections]
    refs = [f"{r.number}. [{r.title}]({r.url}) - {_reference_line(r)}" for r in references]
    parts.append("## References\n\n" + ("\n".join(refs) if refs else "_No sources were cited._"))
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------- PDF

# (regular, bold, italic, bold-italic) for the first family that exists on this machine.
_FONT_SETS = [
    ("/usr/share/fonts/truetype/dejavu/", ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans-Oblique.ttf", "DejaVuSans-BoldOblique.ttf")),
    ("C:/Windows/Fonts/", ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf")),
    ("/System/Library/Fonts/Supplemental/", ("Arial.ttf", "Arial Bold.ttf", "Arial Italic.ttf", "Arial Bold Italic.ttf")),
    ("/Library/Fonts/", ("Arial.ttf", "Arial Bold.ttf", "Arial Italic.ttf", "Arial Bold Italic.ttf")),
]


def _unicode_fonts() -> list[str] | None:
    for folder, names in _FONT_SETS:
        paths = [Path(folder) / n for n in names]
        if all(p.exists() for p in paths):
            return [str(p) for p in paths]
    return None


def _safe_html(markdown_text: str) -> str:
    """Markdown -> HTML with raw HTML and images neutralised.

    Report text derives from untrusted web content; fpdf2 would fetch <img> URLs, so neither raw
    tags nor Markdown images may reach the PDF renderer.
    """
    text = markdown_text.replace("<", "&lt;").replace("![", "[")
    return md.markdown(text, extensions=["tables", "sane_lists"])


def to_pdf(report: Report) -> bytes:
    pdf = FPDF(format="A4")
    pdf.set_margins(18, 18, 18)
    pdf.set_auto_page_break(True, margin=18)
    fonts = _unicode_fonts()
    family = "Helvetica"
    if fonts:
        family = "body"
        for style, path in zip(("", "B", "I", "BI"), fonts):
            pdf.add_font(family, style, path)
    pdf.set_title(report.title)
    pdf.add_page()
    pdf.set_font(family, size=10.5)

    refs = "".join(
        f'<p>{r.number}. <a href="{_attr(r.url)}">{_text(r.title)}</a> - {_text(_reference_line(r))}</p>'
        for r in report.references
    )
    html = f"<h1>{_text(report.title)}</h1>"
    html += "".join(f"<h2>{_text(s.heading)}</h2>{_safe_html(s.content)}" for s in report.sections)
    html += f"<h2>References</h2>{refs or '<p>No sources were cited.</p>'}"
    if not fonts:
        # ponytail: no Unicode TTF found -> core PDF font, non-Latin-1 characters become '?'.
        # Install DejaVu Sans (the Docker image does) for full Unicode output.
        html = html.encode("latin-1", "replace").decode("latin-1")
    pdf.write_html(html, font_family=family, tag_styles={"code": FontFace(family=family), "pre": FontFace(family=family)})
    return bytes(pdf.output())


def _text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr(value: str) -> str:
    return _text(value).replace('"', "&quot;")


# ---------------------------------------------------------------- DOCX

_INLINE = re.compile(r"(\*\*[^*\n]+\*\*|\*[^*\n]+\*|\[[^\]\n]+\]\(https?://[^)\s]+\))")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def _add_hyperlink(paragraph, url: str, text: str) -> None:
    rel_id = paragraph.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rel_id)
    run = OxmlElement("w:r")
    props = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "1D4ED8")
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    props.append(color)
    props.append(underline)
    run.append(props)
    text_el = OxmlElement("w:t")
    text_el.text = text
    text_el.set(qn("xml:space"), "preserve")
    run.append(text_el)
    link.append(run)
    paragraph._p.append(link)


def _add_inline(paragraph, text: str) -> None:
    for part in _INLINE.split(text):
        if not part:
            continue
        link = _LINK.fullmatch(part)
        if link:
            _add_hyperlink(paragraph, link.group(2), link.group(1))
        elif part.startswith("**") and part.endswith("**") and len(part) > 4:
            paragraph.add_run(part[2:-2]).bold = True
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            paragraph.add_run(part[1:-1]).italic = True
        else:
            paragraph.add_run(part)


def _add_markdown(doc, text: str) -> None:
    # ponytail: line-based Markdown subset (headings, lists, bold/italic, links). Tables and
    # nested lists come through as plain lines; swap in pandoc if richer DOCX is needed.
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        heading = re.match(r"(#{1,6})\s+(.*)", stripped)
        bullet = re.match(r"[-*+]\s+(.*)", stripped)
        numbered = re.match(r"\d+[.)]\s+(.*)", stripped)
        if heading:
            doc.add_heading(heading.group(2).strip("# "), level=min(len(heading.group(1)) + 1, 4))
        elif bullet:
            _add_inline(doc.add_paragraph(style="List Bullet"), bullet.group(1))
        elif numbered:
            _add_inline(doc.add_paragraph(style="List Number"), numbered.group(1))
        else:
            _add_inline(doc.add_paragraph(), stripped.lstrip("> "))


def to_docx(report: Report) -> bytes:
    doc = Document()
    doc.core_properties.title = report.title
    doc.add_heading(report.title, level=0)
    for section in report.sections:
        doc.add_heading(section.heading, level=1)
        _add_markdown(doc, section.content)
    doc.add_heading("References", level=1)
    for ref in report.references:
        paragraph = doc.add_paragraph(f"[{ref.number}] ")
        _add_hyperlink(paragraph, ref.url, ref.title)
        paragraph.add_run(f" - {_reference_line(ref)}")
    if not report.references:
        doc.add_paragraph("No sources were cited.")
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
