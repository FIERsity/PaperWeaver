"""Minimal, print-oriented PDF rendering for a completed translated Markdown artifact."""

from __future__ import annotations

import html
import re
from pathlib import Path

from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer


def render_translation_pdf(markdown: Path) -> Path:
    """Render translated Markdown to A4 PDF using Songti for CJK and Times for Latin runs."""
    output = markdown.parent / "pdf" / "translated.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "ChineseTitle", parent=styles["Title"], fontName="STSong-Light", fontSize=18,
        leading=25, alignment=TA_CENTER, spaceAfter=16,
    )
    heading = ParagraphStyle(
        "ChineseHeading", parent=styles["Heading2"], fontName="STSong-Light", fontSize=14,
        leading=20, spaceBefore=14, spaceAfter=8,
    )
    body = ParagraphStyle(
        "ChineseBody", parent=styles["BodyText"], fontName="STSong-Light", fontSize=10.5,
        leading=18, alignment=TA_JUSTIFY, firstLineIndent=21, spaceAfter=6,
    )
    document = SimpleDocTemplate(
        str(output), pagesize=A4, leftMargin=25 * mm, rightMargin=25 * mm,
        topMargin=22 * mm, bottomMargin=22 * mm, title="PaperWeaver translated paper",
    )
    story = []
    for line in markdown.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if line.startswith("# "):
            story.append(Paragraph(_mixed(line[2:]), title))
        elif line.startswith("## "):
            story.append(Paragraph(_mixed(line[3:]), heading))
        elif line == "---":
            story.append(PageBreak())
        else:
            story.append(Paragraph(_mixed(line), body))
            story.append(Spacer(1, 1))
    document.build(story, onFirstPage=_page_number, onLaterPages=_page_number)
    return output


def _mixed(text: str) -> str:
    escaped = html.escape(text)
    return re.sub(
        r"([A-Za-z0-9][A-Za-z0-9 .,:;()/%+*=\-–]*[A-Za-z0-9])",
        r'<font name="Times-Roman">\1</font>', escaped,
    )


def _page_number(canvas, document) -> None:
    canvas.saveState()
    canvas.setFont("Times-Roman", 9)
    canvas.drawCentredString(A4[0] / 2, 12 * mm, str(document.page))
    canvas.restoreState()
