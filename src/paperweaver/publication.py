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
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer

SONGTI_PATH = Path("/System/Library/Fonts/Supplemental/Songti.ttc")
SONGTI_NAME = "SongtiSC"
SONGTI_REGULAR_INDEX = 6


def render_translation_pdf(markdown: Path) -> Path:
    """Render translated Markdown to A4 PDF using Songti for CJK and Times for Latin runs."""
    output = markdown.parent / "pdf" / "translated.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    chinese_font = _register_chinese_font()
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "ChineseTitle", parent=styles["Title"], fontName=chinese_font, fontSize=18,
        leading=25, alignment=TA_CENTER, spaceAfter=16,
    )
    heading = ParagraphStyle(
        "ChineseHeading", parent=styles["Heading2"], fontName=chinese_font, fontSize=14,
        leading=20, spaceBefore=14, spaceAfter=8,
    )
    body = ParagraphStyle(
        "ChineseBody", parent=styles["BodyText"], fontName=chinese_font, fontSize=10.5,
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
        elif line.startswith("![") and "](assets/" in line:
            asset = markdown.parent / "assets" / line.split("](assets/", 1)[1].rstrip(")")
            image = Image(str(asset))
            image._restrictSize(155 * mm, 180 * mm)
            story.extend([image, Spacer(1, 6)])
        else:
            story.append(Paragraph(_mixed(line), body))
            story.append(Spacer(1, 1))
    document.build(story, onFirstPage=_page_number, onLaterPages=_page_number)
    return output


def _register_chinese_font() -> str:
    """Use the macOS Songti collection when present; retain a portable CJK fallback."""
    if SONGTI_PATH.exists():
        if SONGTI_NAME not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(SONGTI_NAME, str(SONGTI_PATH), subfontIndex=SONGTI_REGULAR_INDEX))
        return SONGTI_NAME
    if "STSong-Light" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"


def _mixed(text: str) -> str:
    escaped = html.escape(text).replace("&amp;", "@@@")
    mixed = re.sub(
        r"([A-Za-z0-9][A-Za-z0-9 .,:;()/%+*=\-–]*[A-Za-z0-9])",
        r'<font name="Times-Roman">\1</font>', escaped,
    )
    return mixed.replace("@@@", "&amp;")


def _page_number(canvas, document) -> None:
    canvas.saveState()
    canvas.setFont("Times-Roman", 9)
    canvas.drawCentredString(A4[0] / 2, 12 * mm, str(document.page))
    canvas.restoreState()
