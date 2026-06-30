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
    ct_hu = lesion.get("ct_mean_hu")
    sentence = f"FDG-avid lesion in {region}"
    if isinstance(suv_max, (int, float)):
        sentence += f" with SUV<sub>max</sub> {suv_max:.1f}"
    paren = []
    if isinstance(volume, (int, float)):
        paren.append(f"metabolic volume {volume:.1f} mL")
    if isinstance(ct_hu, (int, float)):
        paren.append(f"CT density {ct_hu:.0f} HU")
    if paren:
        sentence += f" ({', '.join(paren)})"
    return sentence + "."


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
        tlr = summary.get("tumor_to_liver_ratio")
        if isinstance(tlr, (int, float)):
            bullets.append(f"Tumor-to-liver ratio (SUVmax/liver SUVmean): {tlr:.2f}.")
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


# ── MRI narrative report (brain/spine/chest/abdomen) — auto-derived findings ──
_MRI_EXAMINATION_BY_USECASE = {
    "spine_mri": "MRI OF THE SPINE",
    "chest_mri": "MRI OF THE CHEST",
    "abdomen_mri": "MRI OF THE ABDOMEN",
}


_LOCATION_PHRASE = {
    "left": "in the left cerebral hemisphere",
    "right": "in the right cerebral hemisphere",
    "midline": "near the midline",
}


def _mri_size_phrase(summary: dict[str, Any]) -> str:
    """e.g. '2.1 × 1.9 × 1.5 cm (AP × TS × CC)' from derived lesion_dimensions_cm."""
    dims = summary.get("lesion_dimensions_cm") or {}
    ap, ts, cc = dims.get("ap"), dims.get("transverse"), dims.get("craniocaudal")
    if all(isinstance(v, (int, float)) for v in (ap, ts, cc)):
        return f"{ap:.1f} × {ts:.1f} × {cc:.1f} cm (AP × TS × CC)"
    ordered = [v for v in dims.values() if isinstance(v, (int, float))]
    if len(ordered) == 3:
        return " × ".join(f"{v:.1f}" for v in ordered) + " cm"
    return ""


def _mri_signal_phrase(summary: dict[str, Any]) -> str:
    """e.g. 'T2 and FLAIR hyperintense, T1 hypointense relative to brain parenchyma.'"""
    signal = summary.get("signal_profile") or {}
    if not signal:
        return ""
    by_desc: dict[str, list[str]] = {}
    for modality, desc in signal.items():
        by_desc.setdefault(desc, []).append(modality)
    clauses = [
        f"{' and '.join(mods)} {desc}"
        for desc in ("hyperintense", "hypointense", "isointense")
        if (mods := by_desc.get(desc))
    ]
    if not clauses:
        return ""
    return "The lesion appears " + ", ".join(clauses) + " relative to surrounding brain parenchyma."


def _build_mri_findings(summary: dict[str, Any], measurements: dict[str, Any]) -> list[str]:
    """Auto-derive FINDINGS paragraphs from the MRI AI segmentation result.

    Mirrors the PET-CT report's lesion-sentence derivation, enriched with lesion
    geometry (size/location/count) and relative signal characterisation computed
    from the segmentation mask. Every quantity comes from the model's output; the
    radiologist reviews these against the images.
    """
    lines: list[str] = []
    volumes = (measurements or {}).get("volumes_ml", {}) or {}
    detected = summary.get("tumor_detected") is True or summary.get("lesion_detected") is True

    if detected:
        total = summary.get("total_lesion_volume_ml")
        count = summary.get("lesion_count")
        location = summary.get("lesion_location")
        size_phrase = _mri_size_phrase(summary)

        if isinstance(count, int) and count > 1:
            lead = f"AI segmentation identifies {count} segmented lesions; the largest"
        else:
            lead = "AI segmentation identifies a lesion"
        if location in _LOCATION_PHRASE:
            lead += f" {_LOCATION_PHRASE[location]}"
        if size_phrase:
            lead += f", measuring approximately {size_phrase}"
        if isinstance(total, (int, float)):
            lead += f", with a total segmented volume of {total:.1f} mL"
        lines.append(lead.rstrip(",") + ".")

        signal_phrase = _mri_signal_phrase(summary)
        if signal_phrase:
            lines.append(signal_phrase)

        comps = [(k, v) for k, v in volumes.items() if isinstance(v, (int, float)) and v > 0]
        if comps:
            parts = [f"{str(k).replace('_', ' ').title()} {v:.1f} mL" for k, v in comps]
            lines.append("Segmented component volumes — " + "; ".join(parts) + ".")
    else:
        lines.append(
            "No abnormal segmented lesion was detected on the analysed sequences by the AI model."
        )

    findings = summary.get("abnormal_findings")
    if isinstance(findings, list):
        for f in findings:
            if not isinstance(f, dict):
                continue
            title = str(
                f.get("organ") or f.get("finding") or f.get("side") or "Finding"
            ).replace("_", " ").title()
            sev = f.get("severity") or f.get("status") or ""
            note = f.get("note") or ""
            line = title
            if sev:
                line += f" — {str(sev).replace('_', ' ').title()}"
            if note:
                line += f": {note}"
            lines.append(line)

    notes = summary.get("processing_notes")
    if notes:
        lines.append(str(notes))
    return lines


def _build_mri_impression(summary: dict[str, Any]) -> str:
    """Auto-derive the IMPRESSION line from the MRI AI result."""
    detected = summary.get("tumor_detected") is True or summary.get("lesion_detected") is True
    if detected:
        total = summary.get("total_lesion_volume_ml")
        location = summary.get("lesion_location")
        size_phrase = _mri_size_phrase(summary)
        size = f" measuring ~{size_phrase}" if size_phrase else (
            f" (~{total:.1f} mL)" if isinstance(total, (int, float)) else ""
        )
        loc = f" {_LOCATION_PHRASE[location]}" if location in _LOCATION_PHRASE else ""
        return (
            f"Segmented brain lesion{size}{loc} identified on AI analysis — "
            "recommend clinical and radiological correlation."
        )
    return "No segmentable focal lesion identified on AI analysis."


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


def _reading_status_line(patient_info: dict[str, Any]) -> tuple[str, bool]:
    """Human-readable reading-workflow status for the report. Returns (text, is_signed)."""
    rs = patient_info.get("reading_status") or "unread"
    by = patient_info.get("assigned_to_username")
    signed_at = patient_info.get("signed_at")
    if rs == "signed":
        return (
            f"SIGNED OFF{(' — ' + by) if by else ''}{(' · ' + signed_at) if signed_at else ''}",
            True,
        )
    label = {
        "unread": "Unclaimed",
        "in_progress": f"Reading{(' — ' + by) if by else ''}",
        "reported": f"Reported{(' — ' + by) if by else ''}",
    }.get(rs, rs)
    return (f"PRELIMINARY · {label}", False)


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
            if usecase_name == "mammography":
                return self._generate_mammography_report(
                    study_uid, result, patient_info or {}
                )
            if usecase_name in ("brain_mri", "spine_mri", "chest_mri", "abdomen_mri"):
                return self._generate_mri_narrative_report(
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

    def _generate_mammography_report(
        self,
        study_uid: str,
        result: dict[str, Any],
        patient_info: dict[str, Any],
    ) -> bytes:
        """Render the formal bilateral mammography report (AECH-KIRAN layout).

        Body fields (procedure, clinical features, per-breast findings, opinion,
        BI-RADS, signatories) come from the saved radiologist report in
        ``result["summary"]``; demographics from ``patient_info``; hospital header,
        footer roster and address from settings.
        """
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.pdfgen import canvas
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        from app.config import get_settings

        settings = get_settings()
        r = result.get("summary", {}) or {}

        def g(key: str, default: str = "_____") -> str:
            val = patient_info.get(key)
            return str(val) if val not in (None, "") else default

        def rv(key: str, default: str = "_____") -> str:
            val = r.get(key)
            return str(val) if val not in (None, "") else default

        prn = g("patient_id")
        roster = settings.report_footer_roster or []
        address = settings.report_footer_address

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
                    self.setFont("Helvetica", 7)
                    # Footer doctor roster (two columns) + address.
                    if roster:
                        half = (len(roster) + 1) // 2
                        y = 2.4 * cm
                        for i, entry in enumerate(roster[:half]):
                            self.drawString(2 * cm, y - i * 0.32 * cm, entry.replace("|", "—"))
                        for i, entry in enumerate(roster[half:]):
                            self.drawString(11 * cm, y - i * 0.32 * cm, entry.replace("|", "—"))
                    self.setFont("Helvetica-Oblique", 7)
                    self.drawCentredString(A4[0] / 2, 1.0 * cm, address)
                    self.setFont("Helvetica", 8)
                    self.drawRightString(
                        A4[0] - 2 * cm, 0.6 * cm, f"Page {self._pageNumber} of {total}"
                    )
                    super().showPage()
                super().save()

        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=A4,
            leftMargin=2 * cm, rightMargin=2 * cm,
            topMargin=1.6 * cm, bottomMargin=4.2 * cm,
            title=f"Bilateral Mammography — {prn}",
        )

        styles = getSampleStyleSheet()
        h_name = ParagraphStyle("hname", parent=styles["Normal"], fontSize=16,
                                fontName="Helvetica-Bold", alignment=TA_CENTER)
        h_sub = ParagraphStyle("hsub", parent=styles["Normal"], fontSize=8.5,
                               alignment=TA_CENTER, textColor=colors.HexColor("#444444"))
        title_style = ParagraphStyle("title", parent=styles["Normal"], fontSize=12,
                                     fontName="Helvetica-Bold", alignment=TA_CENTER, spaceBefore=8, spaceAfter=6)
        head = ParagraphStyle("head", parent=styles["Normal"], fontSize=10,
                              fontName="Helvetica-Bold", spaceBefore=8, spaceAfter=2)
        body = ParagraphStyle("body", parent=styles["Normal"], fontSize=9.5,
                              alignment=TA_JUSTIFY, leading=13)
        cell = ParagraphStyle("cell", parent=styles["Normal"], fontSize=9)

        story: list[Any] = [
            Paragraph(settings.report_hospital_name, h_name),
            Paragraph(settings.report_hospital_subtitle, h_sub),
            Spacer(1, 6),
        ]

        # Patient table (PRN / File No / Status / Name / Age-Gender / Contact / Entry Date)
        age_gender = f"{g('patient_age', '—')} / {g('patient_sex', '—')}"
        info = [
            [Paragraph(f"<b>PRN:</b> {prn}", cell),
             Paragraph(f"<b>File No.:</b> {rv('file_no', 'NIL')}", cell),
             Paragraph(f"<b>Status:</b> {rv('status', '—')}", cell)],
            [Paragraph(f"<b>Name:</b> {g('patient_name', '—')}", cell),
             Paragraph(f"<b>Age/Gender:</b> {age_gender}", cell),
             Paragraph(f"<b>Contact:</b> {rv('contact', '—')}", cell)],
            [Paragraph(f"<b>Entry Date:</b> {g('study_date', '—')}", cell), "", ""],
        ]
        tbl = Table(info, colWidths=[6 * cm, 5.5 * cm, 5.5 * cm])
        tbl.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#333333")),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("SPAN", (0, 2), (2, 2)),
        ]))
        story.append(tbl)

        laterality = (r.get("laterality") or "bilateral").lower()
        show_right = laterality in ("bilateral", "right")
        show_left = laterality in ("bilateral", "left")

        title_map = {"right": "RIGHT MAMMOGRAPHY", "left": "LEFT MAMMOGRAPHY"}
        story.append(Paragraph(title_map.get(laterality, "BILATERAL MAMMOGRAPHY"), title_style))

        scope = {"right": "of the right breast ", "left": "of the left breast "}.get(
            laterality, "of both breasts "
        )
        procedure_default = f"Digital mammography {scope}performed in routine CC and MLO views."
        story.append(Paragraph("Procedure:", head))
        story.append(Paragraph(rv("procedure", procedure_default), body))
        story.append(Paragraph("Clinical Features:", head))
        story.append(Paragraph(rv("clinical_features", "—"), body))

        story.append(Paragraph("Findings:", head))
        if show_right:
            story.append(Paragraph("RIGHT BREAST:", ParagraphStyle(
                "rb", parent=head, fontSize=9.5, spaceBefore=4)))
            story.append(Paragraph(rv("right_breast_findings", "—"), body))
        if show_left:
            story.append(Paragraph("LEFT BREAST:", ParagraphStyle(
                "lb", parent=head, fontSize=9.5, spaceBefore=4)))
            story.append(Paragraph(rv("left_breast_findings", "—"), body))

        story.append(Paragraph("Opinion:", head))
        story.append(Paragraph(rv("opinion", "—"), body))
        birads_lines = []
        if show_right:
            birads_lines.append(f"BI-RADS category {rv('birads_right', '—')} for right breast.")
        if show_left:
            birads_lines.append(f"BI-RADS category {rv('birads_left', '—')} for left breast.")
        if birads_lines:
            story.append(Paragraph(" ".join(birads_lines), body))

        # Signatories
        story.append(Spacer(1, 30))
        sig = ParagraphStyle("sig", parent=styles["Normal"], fontSize=9.5,
                             fontName="Helvetica-Bold", alignment=TA_CENTER)
        sig_sub = ParagraphStyle("sigsub", parent=styles["Normal"], fontSize=8,
                                 alignment=TA_CENTER)
        sig_tbl = Table([
            [Paragraph(rv("reviewing_doctor", "_______________"), sig),
             Paragraph(rv("reporting_doctor", "_______________"), sig)],
            [Paragraph("Reviewing Doctor", sig_sub), Paragraph("Reporting Doctor", sig_sub)],
        ], colWidths=[8.5 * cm, 8.5 * cm])
        story.append(sig_tbl)

        doc.build(story, canvasmaker=NumberedCanvas)
        buf.seek(0)
        return buf.read()

    def _generate_mri_narrative_report(
        self,
        study_uid: str,
        usecase_name: str,
        result: dict[str, Any],
        patient_info: dict[str, Any],
        narrative: str = "",
    ) -> bytes:
        """Render the formal MRI narrative report (departmental brain MRI layout).

        Layout mirrors the reference clinical report: a two-column demographics
        block, then EXAMINATION / TECHNIQUE / CLINICAL INDICATION / FINDINGS /
        IMPRESSION sections and a single signatory block, with a computer-generated
        footer note.

        Like the PET-CT molecular report, FINDINGS / IMPRESSION are AUTO-DERIVED
        from the AI result (segmentation volumes / lesion flags). Demographics and
        the clinical indication come from ``patient_info`` (merged with patient
        onboarding by the caller); section defaults and signatory from settings.
        """
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        from app.config import get_settings

        settings = get_settings()
        summary = result.get("summary", {}) or {}
        measurements = result.get("measurements", {}) or {}

        def g(key: str, default: str = "") -> str:
            val = patient_info.get(key)
            return str(val) if val not in (None, "") else default

        examination = _MRI_EXAMINATION_BY_USECASE.get(
            usecase_name, settings.mri_report_examination_default
        )
        technique = settings.mri_report_technique_default
        clinical_indication = g("clinical_history") or g("indication")
        findings_lines = _build_mri_findings(summary, measurements)
        impression = _build_mri_impression(summary)
        doctor = settings.mri_report_signatory_name
        doctor_title = settings.mri_report_signatory_title
        doctor_quals = settings.mri_report_signatory_qualifications

        prn = g("patient_id", "—")

        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=A4,
            leftMargin=2.2 * cm, rightMargin=2.2 * cm,
            topMargin=1.8 * cm, bottomMargin=2.0 * cm,
            title=f"MRI Report — {prn}",
        )

        styles = getSampleStyleSheet()
        cell = ParagraphStyle("cell", parent=styles["Normal"], fontSize=10, leading=14)
        sec_head = ParagraphStyle(
            "sechead", parent=styles["Normal"], fontSize=10.5,
            fontName="Helvetica-Bold", spaceBefore=12, spaceAfter=4,
        )
        body = ParagraphStyle(
            "body", parent=styles["Normal"], fontSize=10, leading=15,
            alignment=TA_JUSTIFY, spaceAfter=8,
        )
        sig = ParagraphStyle(
            "sig", parent=styles["Normal"], fontSize=10,
            fontName="Helvetica-BoldOblique", leading=14,
        )
        footer_note = ParagraphStyle(
            "footernote", parent=styles["Normal"], fontSize=8.5,
            fontName="Helvetica-BoldOblique", alignment=TA_CENTER,
            textColor=colors.HexColor("#444444"),
        )

        def section(label: str, paras: list[str]) -> list[Any]:
            """Underlined bold heading + one paragraph per non-empty line."""
            els: list[Any] = [Paragraph(f"<u>{label}:</u>", sec_head)]
            clean = [p.strip() for p in paras if p and p.strip()]
            if not clean:
                clean = ["—"]
            for p in clean:
                els.append(Paragraph(p, body))
            return els

        story: list[Any] = []

        # ── Demographics block (two columns, label : value) ────────────────────
        info_data = [
            [Paragraph(f"<b>PATIENT</b> : {g('patient_name', '—')}", cell),
             Paragraph(f"<b>MR</b> : {prn}", cell)],
            [Paragraph(f"<b>DATE</b> : {g('study_date', '—')}", cell),
             Paragraph(f"<b>AGE</b> : {g('patient_age', '—')}", cell)],
            [Paragraph(f"<b>GENDER</b> : {g('patient_sex', '—')}", cell),
             Paragraph(f"<b>REF</b> : {g('referring_physician', '')}", cell)],
        ]
        info_table = Table(info_data, colWidths=[9.6 * cm, 7.0 * cm])
        info_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(info_table)

        # ── Reading-workflow status (unclaimed / reading by / signed off) ───────
        _status_text, _is_signed = _reading_status_line(patient_info)
        story.append(Paragraph(
            _status_text,
            ParagraphStyle(
                "ReportStatus", alignment=TA_CENTER, fontName="Helvetica-Bold", fontSize=9,
                spaceBefore=6, spaceAfter=2,
                textColor=colors.HexColor("#067647") if _is_signed else colors.HexColor("#b54708"),
            ),
        ))
        story.append(Spacer(1, 0.3 * cm))

        # ── EXAMINATION (run-in, bold + underlined) ────────────────────────────
        story.append(Paragraph(f"<u>EXAMINATION:&nbsp; {examination}:</u>", sec_head))

        # ── Narrative sections ─────────────────────────────────────────────────
        story.append(Paragraph(f"<b><u>TECHNIQUE:</u></b> {technique}", body))
        story.extend(section("CLINICAL INDICATION", [clinical_indication] if clinical_indication else []))
        story.extend(section("FINDINGS", findings_lines))
        story.extend(section("IMPRESSION", [impression]))

        # Optional AI narrative impression (appended, clearly labelled).
        if narrative:
            story.append(Paragraph("<u>AI-GENERATED IMPRESSION:</u>", sec_head))
            story.append(Paragraph(narrative, body))

        # ── Signatory ───────────────────────────────────────────────────────────
        story.append(Spacer(1, 1.6 * cm))
        story.append(Paragraph("_______________________________", cell))
        story.append(Paragraph(doctor, sig))
        if doctor_title:
            story.append(Paragraph(doctor_title, sig))
        if doctor_quals:
            story.append(Paragraph(doctor_quals, sig))

        story.append(Spacer(1, 1.0 * cm))
        story.append(Paragraph(
            "Note: This is a computer generated document and does not require any signature.",
            footer_note,
        ))

        doc.build(story)
        buf.seek(0)
        return buf.read()

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

        # ── Reading-workflow status (unclaimed / reading by / signed off) ────────
        _status_text, _is_signed = _reading_status_line(patient_info)
        story.append(Paragraph(
            _status_text,
            ParagraphStyle(
                "ReportStatus", alignment=TA_CENTER, fontName="Helvetica-Bold", fontSize=9,
                spaceBefore=5, spaceAfter=2,
                textColor=colors.HexColor("#067647") if _is_signed else colors.HexColor("#b54708"),
            ),
        ))

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
        liver_txt = f"{liver:.2f}" if isinstance(liver, (int, float)) else "_____"
        story.append(Paragraph(f"Height: {g('patient_height', '_____')} cm", tech_style))
        story.append(Paragraph(f"Weight: {g('patient_weight', '_____')} kg", tech_style))
        # BMI is derived from height/weight; fall back to computing it here when the
        # intake merge did not supply one but height & weight are present.
        bmi_txt = patient_info.get("bmi")
        if not bmi_txt:
            try:
                h, w = float(patient_info.get("patient_height")), float(patient_info.get("patient_weight"))
                if h > 0 and w > 0:
                    bmi_txt = f"{round(w / ((h / 100.0) ** 2), 1):g}"
            except (TypeError, ValueError):
                bmi_txt = None
        story.append(Paragraph(f"BMI: {bmi_txt or '_____'} kg/m<super>2</super>", tech_style))
        story.append(Paragraph(f"Fasting blood sugar: {g('fasting_glucose', '_____')} mg/dl", tech_style))
        story.append(Paragraph(f"Serum creatinine: {g('creatinine', '_____')} mg/dl", tech_style))
        story.append(Paragraph(f"Site of injection: {g('injection_site', '_____')}", tech_style))
        story.append(Paragraph(
            f"Normal blood pool liver demonstrates SUV<sub>mean</sub> {liver_txt}", tech_style
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
            # Clinical intake (onboarding) — shown when an order is linked.
            for _label, _key in (
                ("Referring Physician:", "referring_physician"),
                ("Indication:", "indication"),
                ("Clinical History:", "clinical_history"),
                ("Priority:", "priority"),
            ):
                _v = patient_info.get(_key)
                if _v:
                    info_data.append([_label, str(_v)])
            info_data.append(["Report Status:", _reading_status_line(patient_info)[0]])
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

        # AI-Detected Abnormal Findings (pathology screening) — mirrors the web
        # ReportView section so the PDF and on-screen report stay consistent.
        _af_summary = result.get("summary", {}) or {}
        _af_meas = result.get("measurements", {}) or {}
        _findings = _af_summary.get("abnormal_findings") or []
        _segments = _af_meas.get("segments") or []
        _lesion_detected = _af_summary.get("lesion_detected") is True
        _lesion_count = _af_summary.get("lesion_count") or 0
        if (isinstance(_findings, list) and _findings) or (
            isinstance(_segments, list) and _segments
        ) or _lesion_detected:
            af_heading = ParagraphStyle(
                "AFHeading", parent=heading_style, fontSize=13,
                textColor=HexColor("#b45309"),
            )
            elements.append(Paragraph("AI-Detected Abnormal Findings", af_heading))
            if _lesion_detected:
                _lc = f"{_lesion_count} lesion(s) detected" if _lesion_count else "Lesion detected"
                elements.append(
                    Paragraph(f'<font color="#b91c1c"><b>{_lc}</b></font>', cds_body_style)
                )
            for _f in (_findings if isinstance(_findings, list) else []):
                if not isinstance(_f, dict):
                    continue
                _title = str(
                    _f.get("organ") or _f.get("finding") or _f.get("side") or "Finding"
                ).replace("_", " ")
                _sev = _f.get("severity") or _f.get("status") or ""
                _note = _f.get("note") or ""
                _line = f"<b>{_title.title()}</b>"
                if _sev:
                    _line += f" — {str(_sev).title()}"
                if _note:
                    _line += f": {_note}"
                elements.append(Paragraph(f"• {_line}", cds_body_style))
            if isinstance(_segments, list) and _segments:
                _cad = _af_summary.get("cad_rads")
                _hdr = "Per-Vessel Stenosis" + (
                    f" — CAD-RADS {_cad}" if _cad is not None else ""
                )
                elements.append(Spacer(1, 4))
                elements.append(Paragraph(f"<b>{_hdr}</b>", cds_body_style))
                _rows = [["Vessel", "Stenosis", "Grade", "Min lumen (mm)", "Reference (mm)"]]
                for _s in _segments:
                    if not isinstance(_s, dict):
                        continue
                    _pct = _s.get("stenosis_pct")
                    _rows.append([
                        str(_s.get("name") or _s.get("vessel") or "Vessel"),
                        f"{_pct:.0f}%" if isinstance(_pct, (int, float)) else "-",
                        str(_s.get("grade") or "-"),
                        str(_s.get("min_lumen_diameter_mm", "-")),
                        str(_s.get("reference_diameter_mm", "-")),
                    ])
                _seg_table = Table(_rows, colWidths=[120, 70, 80, 90, 90])
                _seg_table.setStyle(TableStyle([
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("BACKGROUND", (0, 0), (-1, 0), HexColor("#f3f4f6")),
                    ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#d1d5db")),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                ]))
                elements.append(_seg_table)
            elements.append(Paragraph(
                "<i>AI screening output — not a diagnosis. Review against the images "
                "and clinical context.</i>",
                cds_disclaimer_style,
            ))
            elements.append(Spacer(1, 10))

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
