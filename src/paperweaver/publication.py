"""Minimal, print-oriented PDF rendering for a completed translated Markdown artifact."""

from __future__ import annotations

import html
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

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
    formula = ParagraphStyle(
        "Formula", parent=body, alignment=TA_CENTER, firstLineIndent=0, fontSize=10,
        leading=16, spaceAfter=6,
    )
    table_cell = ParagraphStyle(
        "TableCell", parent=body, fontSize=8.5, leading=11, firstLineIndent=0,
        alignment=TA_CENTER, spaceAfter=0,
    )
    document = SimpleDocTemplate(
        str(output), pagesize=A4, leftMargin=25 * mm, rightMargin=25 * mm,
        topMargin=22 * mm, bottomMargin=22 * mm, title="PaperWeaver translated paper",
    )
    story = []
    lines = markdown.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("|") and line.rstrip().endswith("|"):
            table_lines = []
            while index < len(lines) and lines[index].startswith("|"):
                table_lines.append(lines[index])
                index += 1
            rows, header_rows = _pipe_rows(table_lines)
            if rows:
                columns = max(len(row) for row in rows)
                data = [
                    [Paragraph(_mixed(cell), table_cell) for cell in row]
                    for row in rows
                ]
                table = Table(
                    data,
                    colWidths=[155 * mm / columns] * columns,
                    repeatRows=header_rows,
                    hAlign="CENTER",
                )
                style = [
                    ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#888888")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
                if header_rows:
                    style.extend(
                        [
                            ("BACKGROUND", (0, 0), (-1, header_rows - 1), colors.HexColor("#EEEEEE")),
                            ("LINEBELOW", (0, header_rows - 1), (-1, header_rows - 1), 0.8, colors.black),
                        ]
                    )
                table.setStyle(TableStyle(style))
                story.extend([table, Spacer(1, 8)])
            continue
        if line.strip() == "$$":
            index += 1
            formula_lines = []
            while index < len(lines) and lines[index].strip() != "$$":
                if lines[index].strip():
                    formula_lines.append(lines[index].strip())
                index += 1
            if index >= len(lines):
                raise ValueError("Unclosed display equation in translated Markdown")
            story.append(
                Paragraph(_mixed(_latex_to_display(" ".join(formula_lines))), formula)
            )
            index += 1
            continue
        if not line.strip():
            index += 1
            continue
        if line.startswith("# "):
            story.append(Paragraph(_mixed(line[2:]), title))
        elif line.startswith("## "):
            story.append(Paragraph(_mixed(line[3:]), heading))
        elif line == "---":
            story.append(PageBreak())
        elif line.startswith("$$ ") and line.endswith(" $$"):
            story.append(Paragraph(_mixed(line[3:-3]), formula))
        elif line.startswith("![") and "](assets/" in line:
            asset = markdown.parent / "assets" / line.split("](assets/", 1)[1].rstrip(")")
            image = Image(str(asset))
            image._restrictSize(155 * mm, 180 * mm)
            caption = line[2:].split("]", 1)[0]
            story.extend([image, Spacer(1, 3), Paragraph(_mixed(caption), body), Spacer(1, 6)])
        else:
            story.append(Paragraph(_mixed(line), body))
            story.append(Spacer(1, 1))
        index += 1
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
    escaped = html.escape(text).replace("&amp;", "@@@").replace("&#x27;", "%%%")
    mixed = re.sub(
        r"([A-Za-z0-9][A-Za-z0-9 .,:;()/%+*=\-–]*[A-Za-z0-9])",
        r'<font name="Times-Roman">\1</font>', escaped,
    )
    return mixed.replace("@@@", "&amp;").replace("%%%", "&#x27;")


def _page_number(canvas, document) -> None:
    canvas.saveState()
    canvas.setFont("Times-Roman", 9)
    canvas.drawCentredString(A4[0] / 2, 12 * mm, str(document.page))
    canvas.restoreState()


def _pipe_rows(lines: list[str]) -> tuple[list[list[str]], int]:
    rows: list[list[str]] = []
    header_rows = 0
    for line in lines:
        cells = [
            item.strip().replace(r"\|", "|")
            for item in re.split(r"(?<!\\)\|", line.strip().strip("|"))
        ]
        if cells and all(re.fullmatch(r":?-{3,}:?", item) for item in cells):
            header_rows = len(rows)
            continue
        rows.append(cells)
    return rows, header_rows


def _latex_to_display(value: str) -> str:
    replacements = {
        r"\alpha": "α",
        r"\beta": "β",
        r"\gamma": "γ",
        r"\delta": "δ",
        r"\epsilon": "ε",
        r"\tau": "τ",
        r"\cdot": "⋅",
        r"\times": "×",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    value = re.sub(r"\\tag\{([^{}]+)\}", r"(\1)", value)
    value = re.sub(r"_\{([^{}]+)\}", r"_\1", value)
    value = re.sub(r"\^\{([^{}]+)\}", r"^\1", value)
    return value
