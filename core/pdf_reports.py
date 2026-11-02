"""Build downloadable, multi-page PDF assessment reports."""

from __future__ import annotations

import io
import json
from html import escape
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


INK = colors.HexColor("#192522")
GREEN = colors.HexColor("#245c47")
MUTED = colors.HexColor("#64716c")
PALE = colors.HexColor("#edf3ee")
LINE = colors.HexColor("#d9e2dc")


def _text(value: Any) -> str:
    if value is None or value == "":
        return "Not available"
    return str(value)


def _score(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{float(value) * 100:.1f}%"


def build_assessment_pdf(
    report: dict,
    score: float | None,
    io_score: float | None,
    quality_score: float | None,
    sanitizer_score: float | None,
    feedback: str | None,
    *,
    student_name: str = "",
    roll_no: str = "",
    assignment_title: str = "",
    submitted_at: str = "",
    submitted_code: str = "",
) -> bytes:
    """Return a PDF containing the visible assessment report and diagnostics."""
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=17 * mm,
        leftMargin=17 * mm,
        topMargin=17 * mm,
        bottomMargin=17 * mm,
        title="C-Grade Assessment Report",
        author="C-Grade Portal",
    )
    base = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "PortalTitle", parent=base["Title"], fontName="Helvetica-Bold",
        fontSize=20, leading=24, textColor=INK, alignment=TA_CENTER,
        spaceAfter=5 * mm,
    )
    section_style = ParagraphStyle(
        "PortalSection", parent=base["Heading2"], fontName="Helvetica-Bold",
        fontSize=12, leading=15, textColor=GREEN, spaceBefore=5 * mm,
        spaceAfter=2 * mm, keepWithNext=True,
    )
    body_style = ParagraphStyle(
        "PortalBody", parent=base["BodyText"], fontName="Helvetica",
        fontSize=9, leading=12, textColor=INK, spaceAfter=2 * mm,
        wordWrap="CJK",
    )
    small_style = ParagraphStyle(
        "PortalSmall", parent=body_style, fontSize=8, leading=10,
        textColor=MUTED,
    )
    code_style = ParagraphStyle(
        "PortalCode", parent=base["Code"], fontName="Courier",
        fontSize=7, leading=9, textColor=INK, backColor=PALE,
        borderColor=LINE, borderWidth=0.4, borderPadding=5,
        leftIndent=4, rightIndent=4, spaceBefore=1 * mm, spaceAfter=2 * mm,
        wordWrap="CJK",
    )
    story = [Paragraph("C-Grade Assessment Report", title_style)]

    identity_rows = []
    for label, value in (
        ("Assignment", assignment_title),
        ("Student", student_name),
        ("Roll number", roll_no),
        ("Submitted", submitted_at),
        ("Problem", report.get("problem", "")),
        ("Generated", report.get("generated_at", "")),
    ):
        if value:
            identity_rows.append([Paragraph(f"<b>{escape(label)}</b>", body_style),
                                 Paragraph(escape(_text(value)), body_style)])
    if identity_rows:
        identity_table = Table(identity_rows, colWidths=[35 * mm, 140 * mm], hAlign="LEFT")
        identity_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), PALE),
            ("BOX", (0, 0), (-1, -1), 0.5, LINE),
            ("INNERGRID", (0, 0), (-1, -1), 0.35, LINE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.extend([identity_table, Spacer(1, 4 * mm)])

    story.append(Paragraph("Assessment summary", section_style))
    summary_rows = [["Overall", "I/O", "Code quality", "Sanitizers"],
                    [_score(score), _score(io_score), _score(quality_score), _score(sanitizer_score)]]
    summary_table = Table(summary_rows, colWidths=[43.75 * mm] * 4, hAlign="LEFT")
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), GREEN),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 1), (-1, 1), PALE),
        ("TEXTCOLOR", (0, 1), (-1, 1), INK),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([summary_table, Paragraph("Compilation and gatekeeper", section_style)])

    first_pass = report.get("first_pass", {})
    gatekeeper_rows = [["Check", "Result"]]
    for key, label in (
        ("rules_valid", "Rules validation"),
        ("static_sanity", "Static sanity"),
        ("compiled", "Compilation"),
        ("smoke_ok", "Smoke test"),
        ("suspected_infinite_loop", "Suspected infinite loop"),
    ):
        if key in first_pass:
            gatekeeper_rows.append([label, "Passed" if first_pass[key] else "Failed"])
    if first_pass.get("error"):
        gatekeeper_rows.append(["Compiler / gatekeeper error", _text(first_pass["error"])])
    gatekeeper_table = Table(gatekeeper_rows, colWidths=[52 * mm, 123 * mm], repeatRows=1, hAlign="LEFT")
    gatekeeper_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PALE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("GRID", (0, 0), (-1, -1), 0.35, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(gatekeeper_table)

    if feedback:
        story.append(Paragraph("Feedback summary", section_style))
        for item in str(feedback).splitlines():
            if item.strip():
                story.append(Paragraph(escape(item), body_style))

    metrics = report.get("metrics", [])
    if metrics:
        story.append(Paragraph("Analyzer results and evidence", section_style))
        for metric in metrics:
            name = _text(metric.get("name", metric.get("analyzer", "Analyzer")))
            analyzer = _text(metric.get("analyzer", ""))
            grade = _score(metric.get("score"))
            weight = metric.get("weight")
            weight_text = f" · weight {float(weight) * 100:.0f}%" if weight is not None else ""
            story.append(Paragraph(
                f"<b>{escape(name)}</b> ({escape(analyzer)}) · {grade}{weight_text}",
                section_style,
            ))
            if metric.get("feedback"):
                story.append(Paragraph(escape(str(metric["feedback"])), body_style))
            if metric.get("evidence") is not None:
                evidence = json.dumps(metric["evidence"], indent=2, ensure_ascii=True, default=str)
                story.append(Preformatted(evidence, code_style, maxLineLength=96))

    if submitted_code:
        story.append(Paragraph("Submitted source code", section_style))
        story.append(Preformatted(submitted_code, code_style, maxLineLength=96))

    flags = report.get("flags", [])
    if flags:
        story.append(Paragraph("Review flags", section_style))
        for flag in flags:
            story.append(Paragraph(escape(str(flag)), body_style))

    def footer(canvas, _document):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.line(17 * mm, 12 * mm, A4[0] - 17 * mm, 12 * mm)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(17 * mm, 7 * mm, "C-Grade Portal · Assessment evidence")
        canvas.drawRightString(A4[0] - 17 * mm, 7 * mm, f"Page {_document.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
