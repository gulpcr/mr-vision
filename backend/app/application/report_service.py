"""Auto-Draft Structured Report Service.

Generates a PDF report from AI result measurements using reportlab.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# Brand colours
_DARK_BLUE = colors.HexColor("#0a6bff")
_TEAL = colors.HexColor("#00d4aa")
_GRAY = colors.HexColor("#6b7280")
_LIGHT_GRAY = colors.HexColor("#f3f4f6")
_DARK = colors.HexColor("#111827")
_WARN = colors.HexColor("#ff6b35")


def _fmt_meas_key(key: str) -> str:
    return key.replace(".", " › ").replace("_", " ").title()


def _fmt_val(val: Any) -> str:
    if isinstance(val, float):
        return f"{val:.2f}"
    return str(val)


def _flatten(d: dict, prefix: str = "") -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    for k, v in d.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            rows.extend(_flatten(v, full))
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            rows.append((full, v))
    return rows


def generate_report_pdf(
    study_uid: str,
    patient_name: str | None,
    patient_id: str | None,
    study_date: datetime | None,
    study_description: str | None,
    institution: str | None,
    usecase_name: str,
    model_version: str,
    measurements: dict[str, Any],
    summary: dict[str, Any],
    qa_flags: list[str],
    result_created_at: datetime | None = None,
) -> bytes:
    """Generate a PDF auto-draft report and return raw bytes."""
    buf = io.BytesIO()

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    styles = getSampleStyleSheet()
    story: list = []

    # ── Header ────────────────────────────────────────────────────────────────
    hdr_style = ParagraphStyle(
        "Header",
        parent=styles["Heading1"],
        textColor=_DARK_BLUE,
        fontSize=20,
        spaceAfter=4,
    )
    story.append(Paragraph("MRI AI Platform — Auto-Draft Report", hdr_style))
    story.append(HRFlowable(width="100%", thickness=2, color=_TEAL, spaceAfter=10))

    # ── Patient / Study info table ────────────────────────────────────────────
    info_label = ParagraphStyle(
        "InfoLabel", parent=styles["Normal"],
        textColor=_GRAY, fontSize=9, fontName="Helvetica-Bold"
    )
    info_val = ParagraphStyle(
        "InfoVal", parent=styles["Normal"],
        textColor=_DARK, fontSize=10
    )
    study_date_str = study_date.strftime("%B %d, %Y") if study_date else "—"
    result_date_str = (
        result_created_at.strftime("%B %d, %Y %H:%M UTC") if result_created_at else "—"
    )
    info_data = [
        [
            Paragraph("PATIENT", info_label),
            Paragraph(patient_name or "—", info_val),
            Paragraph("PATIENT ID", info_label),
            Paragraph(patient_id or "—", info_val),
        ],
        [
            Paragraph("STUDY DATE", info_label),
            Paragraph(study_date_str, info_val),
            Paragraph("INSTITUTION", info_label),
            Paragraph(institution or "—", info_val),
        ],
        [
            Paragraph("STUDY", info_label),
            Paragraph(study_description or usecase_name.replace("_", " ").title(), info_val),
            Paragraph("AI GENERATED", info_label),
            Paragraph(result_date_str, info_val),
        ],
    ]
    info_table = Table(info_data, colWidths=[3 * cm, 6.5 * cm, 3 * cm, 6.5 * cm])
    info_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _LIGHT_GRAY),
        ("ROUNDEDCORNERS", [4, 4, 4, 4]),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 0.5 * cm))

    # ── AI Model info ─────────────────────────────────────────────────────────
    model_style = ParagraphStyle(
        "ModelInfo", parent=styles["Normal"],
        textColor=_GRAY, fontSize=9,
        backColor=colors.HexColor("#eff6ff"),
        borderPadding=(4, 8, 4, 8),
        borderColor=_DARK_BLUE,
        borderWidth=1,
    )
    story.append(Paragraph(
        f"<b>AI Model:</b> {usecase_name.replace('_', ' ').upper()}  |  "
        f"<b>Version:</b> {model_version}  |  "
        f"<b>Study UID:</b> {study_uid[:40]}{'...' if len(study_uid) > 40 else ''}",
        model_style,
    ))
    story.append(Spacer(1, 0.4 * cm))

    # ── QA Flags ──────────────────────────────────────────────────────────────
    if qa_flags:
        qa_style = ParagraphStyle(
            "QA", parent=styles["Normal"], textColor=_WARN, fontSize=9, fontName="Helvetica-Bold"
        )
        story.append(Paragraph("QA FLAGS", qa_style))
        for flag in qa_flags:
            story.append(Paragraph(
                f"⚠  {flag.replace('_', ' ').title()}",
                ParagraphStyle("QAItem", parent=styles["Normal"], textColor=_WARN, fontSize=9, leftIndent=12),
            ))
        story.append(Spacer(1, 0.3 * cm))

    # ── Measurements ──────────────────────────────────────────────────────────
    sec_style = ParagraphStyle(
        "Section", parent=styles["Heading2"],
        textColor=_DARK_BLUE, fontSize=13, spaceBefore=8, spaceAfter=4
    )
    story.append(Paragraph("FINDINGS", sec_style))
    story.append(HRFlowable(width="100%", thickness=1, color=_DARK_BLUE, spaceAfter=6))

    meas_rows = _flatten(measurements)
    if meas_rows:
        tbl_data = [
            [
                Paragraph("<b>Measurement</b>", ParagraphStyle("TH", parent=styles["Normal"], textColor=_DARK_BLUE, fontSize=9, fontName="Helvetica-Bold")),
                Paragraph("<b>Value</b>", ParagraphStyle("TH2", parent=styles["Normal"], textColor=_DARK_BLUE, fontSize=9, fontName="Helvetica-Bold", alignment=TA_RIGHT)),
            ]
        ]
        for key, val in meas_rows:
            tbl_data.append([
                Paragraph(_fmt_meas_key(key), ParagraphStyle("Cell", parent=styles["Normal"], fontSize=10, textColor=_DARK)),
                Paragraph(_fmt_val(val), ParagraphStyle("CellR", parent=styles["Normal"], fontSize=10, textColor=_TEAL, alignment=TA_RIGHT, fontName="Helvetica-Bold")),
            ])
        meas_table = Table(tbl_data, colWidths=[13 * cm, 6 * cm])
        meas_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eff6ff")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _LIGHT_GRAY]),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(meas_table)
    else:
        story.append(Paragraph("No measurements available.", ParagraphStyle("Body", parent=styles["Normal"], fontSize=10, textColor=_GRAY)))

    story.append(Spacer(1, 0.6 * cm))

    # ── Summary ───────────────────────────────────────────────────────────────
    if summary:
        story.append(Paragraph("SUMMARY", sec_style))
        story.append(HRFlowable(width="100%", thickness=1, color=_DARK_BLUE, spaceAfter=6))
        sum_rows = _flatten(summary) if isinstance(summary, dict) else []
        for key, val in sum_rows:
            story.append(Paragraph(
                f"<b>{_fmt_meas_key(key)}:</b> {_fmt_val(val)}",
                ParagraphStyle("SumItem", parent=styles["Normal"], fontSize=10, textColor=_DARK),
            ))
        story.append(Spacer(1, 0.4 * cm))

    # ── Impression ────────────────────────────────────────────────────────────
    story.append(Paragraph("IMPRESSION", sec_style))
    story.append(HRFlowable(width="100%", thickness=1, color=_DARK_BLUE, spaceAfter=6))
    story.append(Paragraph(
        "[Radiologist adds clinical interpretation here]",
        ParagraphStyle("Impression", parent=styles["Normal"], fontSize=11, textColor=_GRAY, italics=True),
    ))
    story.append(Spacer(1, 0.5 * cm))

    # ── Disclaimer ────────────────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#e5e7eb"), spaceAfter=6))
    story.append(Paragraph(
        "This is an AI-generated draft report. All measurements and findings must be reviewed "
        "and approved by a licensed radiologist before clinical use. This document does not "
        "constitute a final radiology report.",
        ParagraphStyle("Disclaimer", parent=styles["Normal"], fontSize=8, textColor=_GRAY),
    ))

    doc.build(story)
    buf.seek(0)
    return buf.read()
