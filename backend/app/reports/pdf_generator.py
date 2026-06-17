from __future__ import annotations

import io
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Maps the pet_ct pipeline's per-lesion anatomical_region values onto the
# report's regional SCAN FINDINGS headings.
_REGION_TO_REPORT_SECTION = {
    "Brain": "HEAD & NECK",
    "Head/Neck": "HEAD & NECK",
    "Thorax": "THORAX",
    "Upper Abdomen": "ABDOMEN / PELVIS",
    "Lower Abdomen/Pelvis": "ABDOMEN / PELVIS",
    "Pelvis/Perineum": "ABDOMEN / PELVIS",
}

# Order and default (negative) statement for each report region.
_REPORT_SECTIONS: list[tuple[str, str]] = [
    ("HEAD & NECK",
     "No abnormal FDG-avid lesion is seen in the head and neck region. "
     "Physiological FDG activity is noted in the brain."),
    ("THORAX",
     "No FDG-avid lesion is seen in the thorax. Physiological FDG uptake is "
     "noted in the myocardium and great vessels."),
    ("ABDOMEN / PELVIS",
     "No hypermetabolic lesion is seen in this region. The liver, spleen, "
     "pancreas and bowel show physiological tracer distribution."),
    ("BONES / BONE MARROW",
     "No FDG-avid / non-avid skeletal lesion is noted in this region."),
]


def _lesion_finding_sentence(lesion: dict[str, Any]) -> str:
    """Build a clinician-style finding sentence from a pet_ct lesion dict."""
    region = lesion.get("anatomical_region", "the region")
    suv_max = lesion.get("suv_max")
    volume = lesion.get("volume_ml")
    parts = [f"FDG-avid lesion in {region}"]
    if isinstance(suv_max, (int, float)):
        parts.append(f"with SUV<sub>max</sub> {suv_max:.1f}")
    if isinstance(volume, (int, float)):
        parts.append(f"(metabolic volume {volume:.1f} mL)")
    return " ".join(parts) + "."


def _group_lesions_by_report_section(lesions: list[dict]) -> dict[str, list[dict]]:
    """Bucket lesions into the four report regions by anatomical_region."""
    grouped: dict[str, list[dict]] = {}
    for lesion in lesions:
        section = _REGION_TO_REPORT_SECTION.get(
            lesion.get("anatomical_region", ""), "ABDOMEN / PELVIS"
        )
        grouped.setdefault(section, []).append(lesion)
    return grouped


def _build_conclusions(summary: dict[str, Any], lesions: list[dict]) -> list[str]:
    """Derive the CONCLUSIONS bullet list from the result summary."""
    bullets: list[str] = []
    diagnosis = summary.get("diagnosis")
    if diagnosis:
        bullets.append(str(diagnosis))

    if lesions:
        suvmax = summary.get("suvmax_body")
        suv_txt = f" (highest SUVmax {suvmax:.1f})" if isinstance(suvmax, (int, float)) else ""
        bullets.append(
            f"{len(lesions)} FDG-avid lesion(s) detected{suv_txt}, consistent with "
            "metabolically active disease."
        )
        deauville = summary.get("deauville_score")
        if deauville:
            bullets.append(f"Deauville score: {deauville}.")
        percist = summary.get("percist_score")
        if percist:
            bullets.append(f"PERCIST status: {percist}.")
        mtv = summary.get("mtv_total_ml")
        tlg = summary.get("tlg_total")
        if isinstance(mtv, (int, float)) and isinstance(tlg, (int, float)):
            bullets.append(f"Total metabolic tumour volume {mtv:.1f} mL; total lesion glycolysis {tlg:.1f}.")
    else:
        bullets.append("No FDG-avid lesion suggestive of metabolically active disease was detected.")

    return bullets


def build_petct_patient_info(study_rec: Any) -> dict[str, Any]:
    """Build the PET-CT report patient_info dict (with formatted demographics)
    from a StudyRecord ORM row. Returns {} when no study record is available.

    Shared by both report endpoints so the formal FDG PET-CT layout receives
    identical header data regardless of which route renders it.
    """
    if study_rec is None:
        return {}

    def _fmt_age(raw: Any) -> str:
        if not raw:
            return ""
        raw = str(raw).strip()
        # DICOM AS format e.g. "043Y" → "43 Yrs"
        if len(raw) == 4 and raw[:3].isdigit() and raw[3].upper() in ("Y", "M", "W", "D"):
            unit = {"Y": "Yrs", "M": "Mos", "W": "Wks", "D": "Days"}[raw[3].upper()]
            return f"{int(raw[:3])} {unit}"
        return raw

    def _fmt_sex(raw: Any) -> str:
        return {"M": "Male", "F": "Female", "O": "Other"}.get(
            (raw or "").strip().upper(), raw or ""
        )

    return {
        "patient_name": study_rec.patient_name or "",
        "patient_id": study_rec.patient_id or "",
        "study_date": study_rec.study_date.strftime("%d/%m/%Y") if study_rec.study_date else "",
        "referring_physician": study_rec.referring_physician or "",
        "institution_name": study_rec.institution_name or "",
        "patient_age": _fmt_age(getattr(study_rec, "patient_age", None)),
        "patient_sex": _fmt_sex(getattr(study_rec, "patient_sex", None)),
        "patient_weight": (
            f"{study_rec.patient_weight_kg:g}"
            if getattr(study_rec, "patient_weight_kg", None) else ""
        ),
        "patient_height": (
            f"{study_rec.patient_height_cm:g}"
            if getattr(study_rec, "patient_height_cm", None) else ""
        ),
    }


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
        """Generate a PDF report and return bytes.

        PET-CT use cases (pet_ct, pet_ct_brain) render the formal departmental
        FDG PET-CT molecular-imaging report; everything else uses the generic
        analysis layout.
        """
        try:
            if usecase_name in ("pet_ct", "pet_ct_brain"):
                return self._generate_petct_molecular_report(
                    study_uid, usecase_name, result, patient_info or {}, narrative
                )
            return self._generate_with_reportlab(
                study_uid, usecase_name, result, patient_info, narrative
            )
        except ImportError:
            logger.warning("reportlab_not_installed, using text fallback")
            return self._generate_text_fallback(
                study_uid, usecase_name, result, patient_info, narrative
            )

    def _generate_petct_molecular_report(
        self,
        study_uid: str,
        usecase_name: str,
        result: dict[str, Any],
        patient_info: dict[str, Any],
        narrative: str = "",
    ) -> bytes:
        """Render the formal departmental FDG PET-CT molecular-imaging report.

        Layout mirrors the reference clinical report: department header, bordered
        patient table, then EXAMINATION / CLINICAL HISTORY / COMPARATIVE STUDY /
        PROCEDURE / TECHNIQUE / SCAN FINDINGS / CONCLUSIONS sections and dual
        signatories, with "Page X of Y" footers.

        Derived fields (findings, conclusions, liver SUV, tracer) come from the
        AI result. Demographics / vitals / clinical history come from
        patient_info when available, else render as fillable placeholders.
        """
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.pdfgen import canvas
        from reportlab.platypus import (
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )

        from app.config import get_settings

        settings = get_settings()
        summary = result.get("summary", {}) or {}
        measurements = result.get("measurements", {}) or {}
        lesions = measurements.get("lesions", []) or []

        is_brain = usecase_name == "pet_ct_brain"
        coverage = "brain" if is_brain else "vertex to mid-thigh"
        tracer = summary.get("radiopharmaceutical") or "18F-FDG"

        def g(key: str, default: str = "_____________") -> str:
            val = patient_info.get(key)
            return str(val) if val not in (None, "") else default

        prn = g("patient_id")
        report_date = g("study_date")

        # ── Page-numbering canvas + running header ─────────────────────────────
        institution = settings.report_institution_name

        class NumberedCanvas(canvas.Canvas):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._saved = []

            def showPage(self):
                self._saved.append(dict(self.__dict__))
                self._startPage()

            def save(self):
                total = len(self._saved)
                for state in self._saved:
                    self.__dict__.update(state)
                    self.setFont("Helvetica", 8)
                    self.drawRightString(
                        A4[0] - 2 * cm, 1.2 * cm, f"Page {self._pageNumber} of {total}"
                    )
                    super().showPage()
                super().save()

        def _later_header(cnv, _doc):
            cnv.setFont("Helvetica-Oblique", 8)
            cnv.drawString(2 * cm, A4[1] - 1.2 * cm, institution)
            cnv.drawRightString(A4[0] - 2 * cm, A4[1] - 1.2 * cm, f"PRN: {prn}")

        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=A4,
            leftMargin=2 * cm, rightMargin=2 * cm,
            topMargin=1.8 * cm, bottomMargin=1.8 * cm,
            title=f"FDG PET-CT Report — {prn}",
        )

        styles = getSampleStyleSheet()
        inst_style = ParagraphStyle(
            "Inst", parent=styles["Title"], fontName="Helvetica-Bold",
            fontSize=14, spaceAfter=8, alignment=TA_CENTER,
        )
        report_title_style = ParagraphStyle(
            "RptTitle", parent=styles["Normal"], fontName="Helvetica-Bold",
            fontSize=11, alignment=TA_CENTER, spaceBefore=8, spaceAfter=8,
        )
        body = ParagraphStyle(
            "Body", parent=styles["Normal"], fontSize=10, leading=14,
            alignment=TA_JUSTIFY, spaceAfter=6,
        )
        tech_style = ParagraphStyle(
            "Tech", parent=body, leftIndent=18, spaceAfter=2, alignment=TA_JUSTIFY,
        )
        section_head = ParagraphStyle(
            "SecHead", parent=styles["Normal"], fontName="Helvetica-Bold",
            fontSize=10.5, spaceBefore=8, spaceAfter=4,
        )
        bullet_style = ParagraphStyle(
            "Bullet", parent=body, leftIndent=18, bulletIndent=6, spaceAfter=4,
        )

        story: list = []

        # ── Department header ──────────────────────────────────────────────────
        story.append(Paragraph(institution, inst_style))

        # ── Patient info table (bordered) ──────────────────────────────────────
        cell_l = ParagraphStyle("CellL", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=9)
        cell_v = ParagraphStyle("CellV", parent=styles["Normal"], fontSize=9)

        def lbl(t):  # noqa: E306
            return Paragraph(t, cell_l)

        def val(t):  # noqa: E306
            return Paragraph(t, cell_v)

        info_data = [
            [lbl("Name:"), val(g("patient_name")), lbl("PRN:"), val(prn), lbl("Date:"), val(report_date)],
            [lbl("Ref. Dr / Hosp:"), val(g("referring_physician")), lbl("Age:"), val(g("patient_age")), lbl("Sex:"), val(g("patient_sex"))],
        ]
        info_table = Table(
            info_data,
            colWidths=[2.6 * cm, 4.0 * cm, 1.4 * cm, 3.0 * cm, 1.2 * cm, 3.0 * cm],
        )
        info_table.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
            ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(info_table)

        # ── Report title ───────────────────────────────────────────────────────
        story.append(Paragraph(
            "<super>18</super>F-FDG POSITRON EMISSION-COMPUTERIZED TOMOGRAPHY (FDG PET-CT)",
            report_title_style,
        ))

        def run_in(label: str, text: str) -> Paragraph:
            return Paragraph(f"<b>{label}:</b> {text}", body)

        # ── Narrative sections ───────────────────────────────────────────────────
        story.append(run_in("EXAMINATION", f"{tracer} PET-CT scan, {coverage}."))
        story.append(run_in("CLINICAL HISTORY", g("clinical_history", "[To be completed by referring clinician]")))
        story.append(run_in("COMPARATIVE STUDY", g("comparative_study", "No prior study available for comparison.")))

        dose = patient_info.get("injected_dose")
        dose_txt = f"{dose} of " if dose else ""
        story.append(run_in(
            "PROCEDURE",
            f"Approximately 60 minutes after the intravenous administration of {dose_txt}{tracer}, "
            f"PET images were acquired from the {coverage} using 3-D acquisition. A low-dose CT was "
            "obtained for attenuation correction and anatomical localisation. Images were displayed in "
            "the axial, coronal and sagittal planes. Maximum Standardized Uptake Value (SUVmax) "
            "normalized for body weight was used.",
        ))

        # ── TECHNIQUE ────────────────────────────────────────────────────────────
        story.append(Paragraph("TECHNIQUE:", section_head))
        liver = (measurements.get("reference_organs", {}) or {}).get("liver_suv_mean")
        liver_txt = f"{liver:.1f}" if isinstance(liver, (int, float)) else "_____"
        story.append(Paragraph(f"Height: {g('patient_height', '_____')} cm", tech_style))
        story.append(Paragraph(f"Weight: {g('patient_weight', '_____')} kg", tech_style))
        story.append(Paragraph(f"Fasting blood sugar: {g('fasting_glucose', '_____')} mg/dl", tech_style))
        story.append(Paragraph(f"Site of injection: {g('injection_site', '_____')}", tech_style))
        story.append(Paragraph(
            f"Normal blood pool liver demonstrates SUV<sub>max</sub> {liver_txt}", tech_style
        ))

        # ── SCAN FINDINGS ──────────────────────────────────────────────────────
        story.append(Paragraph("SCAN FINDINGS:", section_head))
        grouped = _group_lesions_by_report_section(lesions)
        for section_name, default_text in _REPORT_SECTIONS:
            sec_lesions = grouped.get(section_name, [])
            if sec_lesions:
                sentences = " ".join(_lesion_finding_sentence(le) for le in sec_lesions)
                text = sentences
            else:
                text = default_text
            story.append(Paragraph(f"<b><i>{section_name}:</i></b> {text}", body))

        # ── CONCLUSIONS ──────────────────────────────────────────────────────────
        story.append(Paragraph("CONCLUSIONS:", section_head))
        for bullet in _build_conclusions(summary, lesions):
            story.append(Paragraph(bullet, bullet_style, bulletText="•"))

        # Optional AI narrative impression (appended, clearly labelled)
        if narrative:
            story.append(Spacer(1, 0.2 * cm))
            story.append(Paragraph("<b>AI-Generated Impression:</b>", section_head))
            story.append(Paragraph(narrative, body))

        # ── Signatures ───────────────────────────────────────────────────────────
        story.append(Spacer(1, 1.4 * cm))
        sig_style = ParagraphStyle("Sig", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=10)
        sig_table = Table(
            [[Paragraph(settings.report_signatory_primary, sig_style),
              Paragraph(settings.report_signatory_secondary, sig_style)]],
            colWidths=[7.5 * cm, 7.5 * cm],
        )
        sig_table.setStyle(TableStyle([
            ("ALIGN", (0, 0), (0, 0), "LEFT"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(sig_table)

        doc.build(story, onLaterPages=_later_header, canvasmaker=NumberedCanvas)
        buf.seek(0)
        return buf.read()

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
