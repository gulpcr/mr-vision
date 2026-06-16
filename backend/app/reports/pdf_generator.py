from __future__ import annotations

import io
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class PDFReportGenerator:
    """Generate PDF reports from AI results using reportlab."""

    def generate(
        self,
        study_uid: str,
        usecase_name: str,
        result: dict[str, Any],
        patient_info: dict[str, Any] | None = None,
        narrative: str = "",
    ) -> bytes:
        """Generate a PDF report and return bytes."""
        try:
            return self._generate_with_reportlab(
                study_uid, usecase_name, result, patient_info, narrative
            )
        except ImportError:
            logger.warning("reportlab_not_installed, using text fallback")
            return self._generate_text_fallback(
                study_uid, usecase_name, result, patient_info, narrative
            )

    def _generate_with_reportlab(
        self,
        study_uid: str,
        usecase_name: str,
        result: dict[str, Any],
        patient_info: dict[str, Any] | None,
        narrative: str = "",
    ) -> bytes:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.colors import HexColor
        from reportlab.platypus import (
            SimpleDocTemplate,
            Paragraph,
            Spacer,
            Table,
            TableStyle,
        )

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            topMargin=20 * mm,
            bottomMargin=20 * mm,
            leftMargin=20 * mm,
            rightMargin=20 * mm,
        )

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "CustomTitle",
            parent=styles["Title"],
            fontSize=18,
            textColor=HexColor("#1e3a5f"),
            spaceAfter=12,
        )
        heading_style = ParagraphStyle(
            "CustomHeading",
            parent=styles["Heading2"],
            fontSize=13,
            textColor=HexColor("#2563eb"),
            spaceBefore=16,
            spaceAfter=8,
        )
        body_style = styles["Normal"]
        narrative_style = ParagraphStyle(
            "Narrative",
            parent=body_style,
            fontSize=11,
            leading=16,
            leftIndent=10,
            rightIndent=10,
            spaceBefore=6,
            spaceAfter=6,
            backColor=HexColor("#f0f7ff"),
            borderPad=8,
        )
        narrative_label_style = ParagraphStyle(
            "NarrativeLabel",
            parent=heading_style,
            fontSize=12,
            textColor=HexColor("#1e40af"),
        )
        cds_body_style = ParagraphStyle(
            "CDSBody",
            parent=body_style,
            fontSize=10,
            leading=15,
            leftIndent=10,
            spaceBefore=3,
            spaceAfter=3,
        )
        cds_disclaimer_style = ParagraphStyle(
            "CDSDisclaimer",
            parent=body_style,
            fontSize=8,
            textColor=HexColor("#6b7280"),
            leftIndent=10,
            spaceBefore=4,
        )
        lng_body_style = ParagraphStyle(
            "LngBody",
            parent=body_style,
            fontSize=10,
            leading=15,
            leftIndent=10,
            spaceBefore=3,
            spaceAfter=3,
        )
        lng_disclaimer_style = ParagraphStyle(
            "LngDisclaimer",
            parent=body_style,
            fontSize=8,
            textColor=HexColor("#6b7280"),
            leftIndent=10,
            spaceBefore=4,
        )

        elements = []

        # Title
        elements.append(Paragraph("MRI AI Platform - Analysis Report", title_style))
        elements.append(Spacer(1, 6))

        # Patient info
        if patient_info:
            info_data = [
                ["Patient Name:", patient_info.get("patient_name", "N/A")],
                ["Patient ID:", patient_info.get("patient_id", "N/A")],
                ["Study Date:", patient_info.get("study_date", "N/A")],
                ["Study UID:", study_uid[:40] + "..." if len(study_uid) > 40 else study_uid],
            ]
            info_table = Table(info_data, colWidths=[120, 350])
            info_table.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
            ]))
            elements.append(info_table)
            elements.append(Spacer(1, 12))

        # Use Case
        uc_label = usecase_name.replace("_", " ").title()
        elements.append(Paragraph(f"Analysis: {uc_label}", heading_style))

        # AI-Generated Narrative Impression
        if narrative:
            elements.append(Paragraph("AI-Generated Impression", narrative_label_style))
            elements.append(Paragraph(narrative, narrative_style))
            elements.append(Spacer(1, 8))

        # Clinical Decision Support (Phase 3)
        summary = result.get("summary", {})
        clinical_context = summary.get("clinical_context") if isinstance(summary, dict) else None
        if clinical_context and isinstance(clinical_context, dict):
            from app.application.cds_service import RISK_COLOURS, URGENCY_COLOURS

            risk = clinical_context.get("risk_level", "moderate")
            urgency = clinical_context.get("urgency", "routine")
            risk_hex = RISK_COLOURS.get(risk, "#6b7280")
            urgency_hex = URGENCY_COLOURS.get(urgency, "#6b7280")

            cds_heading_style = ParagraphStyle(
                "CDSHeading",
                parent=heading_style,
                fontSize=13,
                textColor=HexColor("#1e3a5f"),
            )
            elements.append(Paragraph("Clinical Decision Support", cds_heading_style))

            # Risk + urgency badges (inline coloured text)
            elements.append(
                Paragraph(
                    f'<b>Risk Level:</b> <font color="{risk_hex}"><b>{risk.upper()}</b></font>'
                    f'&nbsp;&nbsp;&nbsp;<b>Urgency:</b> <font color="{urgency_hex}">'
                    f'<b>{urgency.replace("-", "‑").title()}</b></font>',
                    cds_body_style,
                )
            )

            criteria = clinical_context.get("relevant_criteria", "")
            if criteria:
                elements.append(
                    Paragraph(f"<b>Criteria:</b> {criteria}", cds_body_style)
                )

            interpretation = clinical_context.get("interpretation", "")
            if interpretation:
                elements.append(Spacer(1, 4))
                elements.append(Paragraph("<b>Interpretation</b>", cds_body_style))
                elements.append(Paragraph(interpretation, cds_body_style))

            recommendations = clinical_context.get("recommendations", [])
            if recommendations:
                elements.append(Spacer(1, 4))
                elements.append(Paragraph("<b>Recommendations</b>", cds_body_style))
                for rec in recommendations:
                    elements.append(Paragraph(f"• {rec}", cds_body_style))

            disclaimer = clinical_context.get("disclaimer", "")
            if disclaimer:
                elements.append(Paragraph(f"<i>{disclaimer}</i>", cds_disclaimer_style))

            elements.append(Spacer(1, 10))

        # Longitudinal Trend Analysis (Phase 4)
        longitudinal_analysis = summary.get("longitudinal_analysis") if isinstance(summary, dict) else None
        if longitudinal_analysis and isinstance(longitudinal_analysis, dict):
            from app.application.longitudinal_service import TREND_COLOURS

            trend = longitudinal_analysis.get("trend", "insufficient_data")
            response_cat = longitudinal_analysis.get("response_category", "not_applicable")
            trend_hex = TREND_COLOURS.get(trend, "#6b7280")
            studies_compared = longitudinal_analysis.get("studies_compared", 1)
            timespan_days = longitudinal_analysis.get("timespan_days")

            lng_heading_style = ParagraphStyle(
                "LngHeading",
                parent=heading_style,
                fontSize=13,
                textColor=HexColor("#1e3a5f"),
            )
            elements.append(Paragraph("Longitudinal Trend Analysis", lng_heading_style))

            # Trend + response badges
            timespan_txt = f" over {timespan_days} days" if timespan_days else ""
            elements.append(
                Paragraph(
                    f'<b>Trend:</b> <font color="{trend_hex}"><b>{trend.replace("_", " ").upper()}</b></font>'
                    f'&nbsp;&nbsp;&nbsp;<b>Response:</b> {response_cat}'
                    f'&nbsp;&nbsp;&nbsp;<b>Studies compared:</b> {studies_compared}{timespan_txt}',
                    lng_body_style,
                )
            )

            # Key changes table
            key_changes = longitudinal_analysis.get("key_changes", [])
            if key_changes:
                elements.append(Spacer(1, 6))
                chg_data = [["Metric", "Baseline", "Current", "Change %", "Direction"]]
                for chg in key_changes:
                    baseline = chg.get("baseline_value")
                    current_val = chg.get("current_value")
                    change_pct = chg.get("change_pct")
                    chg_data.append([
                        chg.get("metric", "").replace("_", " ").title(),
                        f"{baseline:.2f}" if isinstance(baseline, (int, float)) else str(baseline or "N/A"),
                        f"{current_val:.2f}" if isinstance(current_val, (int, float)) else str(current_val or "N/A"),
                        f"{change_pct:+.1f}%" if isinstance(change_pct, (int, float)) else "N/A",
                        chg.get("direction", ""),
                    ])
                chg_table = Table(chg_data, colWidths=[120, 70, 70, 70, 140])
                chg_table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), HexColor("#1e3a5f")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), HexColor("#ffffff")),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#cccccc")),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [HexColor("#f8fafc"), HexColor("#ffffff")]),
                ]))
                elements.append(chg_table)

            clinical_sig = longitudinal_analysis.get("clinical_significance", "")
            if clinical_sig:
                elements.append(Spacer(1, 4))
                elements.append(Paragraph("<b>Clinical Significance</b>", lng_body_style))
                elements.append(Paragraph(clinical_sig, lng_body_style))

            follow_up = longitudinal_analysis.get("follow_up_recommendation", "")
            if follow_up:
                elements.append(Spacer(1, 4))
                elements.append(Paragraph(f"<b>Follow-up:</b> {follow_up}", lng_body_style))

            lng_disclaimer = longitudinal_analysis.get("disclaimer", "")
            if lng_disclaimer:
                elements.append(Paragraph(f"<i>{lng_disclaimer}</i>", lng_disclaimer_style))

            elements.append(Spacer(1, 10))

        # Summary (exclude nested LLM dicts — rendered in their own sections above)
        if summary:
            plain_summary = {
                k: v for k, v in summary.items()
                if k not in ("clinical_context", "longitudinal_analysis")
            }
            if plain_summary:
                elements.append(Paragraph("Summary", heading_style))
                for key, value in plain_summary.items():
                    label = key.replace("_", " ").title()
                    elements.append(Paragraph(f"<b>{label}:</b> {value}", body_style))
                    elements.append(Spacer(1, 4))

        # Measurements
        measurements = result.get("measurements", {})
        if measurements:
            elements.append(Paragraph("Measurements", heading_style))
            meas_data = [["Measurement", "Value"]]
            for key, value in measurements.items():
                label = key.replace("_", " ").title()
                if isinstance(value, float):
                    meas_data.append([label, f"{value:.4f}"])
                else:
                    meas_data.append([label, str(value)])

            meas_table = Table(meas_data, colWidths=[250, 220])
            meas_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), HexColor("#2563eb")),
                ("TEXTCOLOR", (0, 0), (-1, 0), HexColor("#ffffff")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#cccccc")),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [HexColor("#f8fafc"), HexColor("#ffffff")]),
            ]))
            elements.append(meas_table)

        # QA Flags
        qa_flags = result.get("qa_flags", [])
        if qa_flags:
            elements.append(Paragraph("Quality Assurance Flags", heading_style))
            for flag in qa_flags:
                flag_str = flag.replace("_", " ").title() if isinstance(flag, str) else str(flag)
                elements.append(Paragraph(f"• {flag_str}", body_style))

        # Model info
        elements.append(Paragraph("Model Information", heading_style))
        elements.append(Paragraph(
            f"<b>Version:</b> {result.get('model_version', 'N/A')}", body_style
        ))
        elements.append(Paragraph(
            f"<b>Checksum:</b> {result.get('model_checksum', 'N/A')[:32]}...", body_style
        ))

        # Footer note
        elements.append(Spacer(1, 24))
        elements.append(Paragraph(
            "<i>This report was generated automatically by the MRI AI Platform. "
            "It is intended for research use and should be reviewed by a qualified radiologist.</i>",
            ParagraphStyle("Footer", parent=body_style, fontSize=8, textColor=HexColor("#666666")),
        ))

        doc.build(elements)
        return buffer.getvalue()

    def _generate_text_fallback(
        self,
        study_uid: str,
        usecase_name: str,
        result: dict[str, Any],
        patient_info: dict[str, Any] | None,
        narrative: str = "",
    ) -> bytes:
        """Plain text fallback when reportlab is not available."""
        lines = ["=" * 60]
        lines.append("MRI AI Platform - Analysis Report")
        lines.append("=" * 60)
        lines.append("")

        if patient_info:
            lines.append(f"Patient: {patient_info.get('patient_name', 'N/A')}")
            lines.append(f"ID: {patient_info.get('patient_id', 'N/A')}")
            lines.append(f"Study: {study_uid}")
            lines.append("")

        lines.append(f"Analysis: {usecase_name.replace('_', ' ').title()}")
        lines.append("-" * 40)

        if narrative:
            lines.append("\nAI-Generated Impression:")
            lines.append(narrative)
            lines.append("")

        summary = result.get("summary", {})
        clinical_context = summary.get("clinical_context") if isinstance(summary, dict) else None
        if clinical_context and isinstance(clinical_context, dict):
            lines.append("\nClinical Decision Support:")
            lines.append(f"  Risk Level : {clinical_context.get('risk_level', '').upper()}")
            lines.append(f"  Urgency    : {clinical_context.get('urgency', '').title()}")
            if clinical_context.get("relevant_criteria"):
                lines.append(f"  Criteria   : {clinical_context['relevant_criteria']}")
            if clinical_context.get("interpretation"):
                lines.append(f"  Interpretation: {clinical_context['interpretation']}")
            for rec in clinical_context.get("recommendations", []):
                lines.append(f"  - {rec}")
            if clinical_context.get("disclaimer"):
                lines.append(f"  [{clinical_context['disclaimer']}]")
            lines.append("")

        longitudinal_analysis = summary.get("longitudinal_analysis") if isinstance(summary, dict) else None
        if longitudinal_analysis and isinstance(longitudinal_analysis, dict):
            lines.append("\nLongitudinal Trend Analysis:")
            lines.append(f"  Trend            : {longitudinal_analysis.get('trend', '').replace('_', ' ').upper()}")
            lines.append(f"  Response Category: {longitudinal_analysis.get('response_category', 'N/A')}")
            lines.append(f"  Studies Compared : {longitudinal_analysis.get('studies_compared', 'N/A')}")
            if longitudinal_analysis.get("timespan_days"):
                lines.append(f"  Timespan (days)  : {longitudinal_analysis['timespan_days']}")
            if longitudinal_analysis.get("clinical_significance"):
                lines.append(f"  Significance     : {longitudinal_analysis['clinical_significance']}")
            if longitudinal_analysis.get("follow_up_recommendation"):
                lines.append(f"  Follow-up        : {longitudinal_analysis['follow_up_recommendation']}")
            for chg in longitudinal_analysis.get("key_changes", []):
                pct = chg.get("change_pct")
                pct_str = f"{pct:+.1f}%" if isinstance(pct, (int, float)) else "N/A"
                lines.append(f"    {chg.get('metric', '')}: {chg.get('baseline_value')} → {chg.get('current_value')} ({pct_str})")
            if longitudinal_analysis.get("disclaimer"):
                lines.append(f"  [{longitudinal_analysis['disclaimer']}]")
            lines.append("")

        if summary:
            plain_summary = {
                k: v for k, v in summary.items()
                if k not in ("clinical_context", "longitudinal_analysis")
            }
            if plain_summary:
                lines.append("\nSummary:")
                for k, v in plain_summary.items():
                    lines.append(f"  {k}: {v}")

        measurements = result.get("measurements", {})
        if measurements:
            lines.append("\nMeasurements:")
            for k, v in measurements.items():
                lines.append(f"  {k}: {v}")

        qa_flags = result.get("qa_flags", [])
        if qa_flags:
            lines.append(f"\nQA Flags: {', '.join(str(f) for f in qa_flags)}")

        lines.append(f"\nModel: {result.get('model_version', 'N/A')}")
        lines.append("\n" + "=" * 60)

        return "\n".join(lines).encode("utf-8")
