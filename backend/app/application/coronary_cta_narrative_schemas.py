from __future__ import annotations

"""Pydantic input contract for the coronary_cta Gemini narrative service.

``CoronaryCtaFindings`` validates ONE boundary: the deterministic Agatston/stenosis numbers
pulled from ``postprocess()``'s ``summary``/``measurements`` dicts, typed and range-checked
before they're ever placed in the prompt as "Automated Pipeline Insights". Built only by
``build_findings_payload()`` in ``coronary_cta_narrative_service.py`` — never constructed from
LLM output. Nowhere else in this codebase's application/usecases layers uses Pydantic for
pipeline data (see ``pet_ct_narrative_service.py``, which passes plain dicts); it's used here
because these are hard numeric/categorical values — 0-100% stenosis, CAD-RADS 0-5, a fixed
stenosis-grade vocabulary — worth typing on the way in.

There is deliberately no output-side schema. The narrative service asks Gemini for autonomous
clinical prose (Markdown), not a structured object to validate field-by-field — the model has
explicit authority to weigh these "hints" against the attached images and reach its own
conclusions, including ones that diverge from a given number. See the module docstring in
``coronary_cta_narrative_service.py`` for the reasoning and the (lighter-touch) safety checks
that replace strict per-field validation on the way out.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

StenosisGrade = Literal["none", "minimal", "mild", "moderate", "severe", "occluded"]
VesselTerritory = Literal["LM", "LAD", "LCx", "RCA", "unassigned"]
PlaqueType = Literal["calcified", "non_calcified", "mixed", "indeterminate"]


class StudyMetadata(BaseModel):
    modality: Literal["Coronary CTA"] = "Coronary CTA"
    inference_method: Literal["calcium_only", "totalseg_coronary_stenosis", "dl_stenosis"]
    image_dimensions: list[int] = Field(default_factory=list)
    voxel_spacing_mm: list[float] = Field(default_factory=list)


class QuantitativeScores(BaseModel):
    agatston_calcium_score: float = Field(ge=0)
    calcium_category: str
    calcium_volume_mm3: float = Field(ge=0)
    calcium_lesion_count: int = Field(ge=0)
    calcium_score_valid: bool


class VesselFinding(BaseModel):
    """One geometrically-graded coronary lumen segment.

    ``vessel_territory`` defaults to "unassigned" — the pipeline has no learned or
    registered coronary-tree labeller (see ``coronary_cta/pipeline.py:_grade_lumen_mask``,
    ``SCCT_SEGMENTS`` is unused dead code). It MAY instead be one of "LM"/"LAD"/"LCx"/"RCA"
    when the optional, default-DISABLED ``vessel_territory`` geometric heuristic
    (``_assign_vessel_territory``, groove-proximity to TotalSegmentator heart chambers) ran
    and cleared its confidence threshold — ``vessel_confidence`` (the heuristic's vote
    fraction) is always attached alongside it so the narrative can hedge appropriately.
    This is still not a validated anatomical assignment; the prompt must always attribute it
    to the heuristic, never state it as certain. ``plaque_type`` is similarly an optional
    HU-threshold heuristic (``_classify_plaque``), not a learned classifier — ``None`` when
    disabled or no wall-shell HU data was found near the stenotic point.
    """

    vessel_label: str
    vessel_territory: VesselTerritory = "unassigned"
    vessel_confidence: float | None = Field(default=None, ge=0, le=1)
    stenosis_percentage: float = Field(ge=0, le=100)
    stenosis_grade: StenosisGrade
    reference_diameter_mm: float = Field(gt=0)
    minimal_lumen_diameter_mm: float = Field(ge=0)
    centerline_length_mm: float = Field(gt=0)
    plaque_type: PlaqueType | None = None

    @field_validator("minimal_lumen_diameter_mm")
    @classmethod
    def _mld_not_above_reference(cls, v: float, info) -> float:
        ref = info.data.get("reference_diameter_mm")
        if ref is not None and v > ref:
            raise ValueError("minimal_lumen_diameter_mm cannot exceed reference_diameter_mm")
        return v

    @field_validator("vessel_confidence")
    @classmethod
    def _confidence_requires_assigned_territory(cls, v: float | None, info) -> float | None:
        territory = info.data.get("vessel_territory")
        if v is not None and territory == "unassigned":
            raise ValueError("vessel_confidence set without an assigned vessel_territory")
        return v


class CadRadsAssessment(BaseModel):
    stenosis_analysis_available: bool
    max_stenosis_percentage: float | None = Field(default=None, ge=0, le=100)
    cad_rads_category: int | None = Field(default=None, ge=0, le=5)

    @field_validator("cad_rads_category")
    @classmethod
    def _cad_rads_requires_analysis(cls, v: int | None, info) -> int | None:
        if v is not None and not info.data.get("stenosis_analysis_available"):
            raise ValueError("cad_rads_category set without stenosis_analysis_available")
        return v


class IncidentalFinding(BaseModel):
    """Reserved for future use. The pipeline's segmentation tasks (heart ROI,
    coronary_arteries) never assess extracardiac structures, so this list is always empty
    today — kept as a typed field so the contract shape doesn't change when that capability
    is added."""

    organ_system: str
    finding_description: str
    associated_image_path: str | None = None


class CoronaryCtaFindings(BaseModel):
    """The full "Automated Pipeline Insights" payload shown to Gemini — preliminary,
    typed/range-checked hints the model is expected to weigh against the attached images,
    not a floor it's forbidden to question."""

    study_metadata: StudyMetadata
    quantitative_scores: QuantitativeScores
    cad_rads_assessment: CadRadsAssessment
    vessel_findings: list[VesselFinding] = Field(default_factory=list)
    incidental_findings: list[IncidentalFinding] = Field(default_factory=list)
    qa_flags: list[str] = Field(default_factory=list)
    processing_notes: str = ""
