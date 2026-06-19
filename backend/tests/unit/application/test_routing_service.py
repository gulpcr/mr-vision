from __future__ import annotations

from app.application.routing_service import RoutingService


def _m(rule, body_part="", study_desc="", series_descs=None, modality="", require_region=True):
    """Convenience wrapper returning just the matched boolean.

    `modality` may be a single string or a set/list of modalities present.
    """
    if isinstance(modality, str):
        mods = {modality} if modality else set()
    else:
        mods = set(modality)
    return RoutingService._rule_match_detail(
        rule, body_part, study_desc, series_descs or [], mods, require_region
    )[0]


class TestRuleMatchDetail:
    """Tests for RoutingService._rule_match_detail (region-necessary semantics)."""

    def test_body_part_match(self):
        rule = {"body_parts": ["BRAIN", "HEAD"], "study_description_patterns": [], "series_description_patterns": []}
        assert _m(rule, body_part="BRAIN") is True

    def test_body_part_no_match(self):
        rule = {"body_parts": ["BRAIN"], "study_description_patterns": [], "series_description_patterns": []}
        assert _m(rule, body_part="KNEE") is False

    def test_body_part_empty_field_skipped(self):
        """Empty body_part field cannot satisfy a region condition -> no match."""
        rule = {"body_parts": ["BRAIN"], "study_description_patterns": [], "series_description_patterns": []}
        assert _m(rule, body_part="") is False

    def test_study_description_pattern_match(self):
        rule = {"body_parts": [], "study_description_patterns": ["BRAIN.*MRI"], "series_description_patterns": []}
        assert _m(rule, study_desc="BRAIN MRI PROTOCOL") is True

    def test_study_description_pattern_no_match(self):
        rule = {"body_parts": [], "study_description_patterns": ["BRAIN.*MRI"], "series_description_patterns": []}
        assert _m(rule, study_desc="KNEE SCAN") is False

    def test_series_description_pattern_match(self):
        rule = {"body_parts": [], "study_description_patterns": [], "series_description_patterns": ["T1.*MPRAGE"]}
        assert _m(rule, series_descs=["T1 MPRAGE", "FLAIR"]) is True

    def test_series_description_pattern_no_match(self):
        rule = {"body_parts": [], "study_description_patterns": [], "series_description_patterns": ["T1.*MPRAGE"]}
        assert _m(rule, series_descs=["FLAIR", "DWI"]) is False

    def test_any_region_condition_sufficient(self):
        """A body_part hit matches even if the study-description pattern doesn't."""
        rule = {"body_parts": ["BRAIN"], "study_description_patterns": ["SPINE"], "series_description_patterns": []}
        assert _m(rule, body_part="BRAIN", study_desc="BRAIN MRI") is True

    def test_no_conditions_evaluable_returns_false(self):
        rule = {"body_parts": ["BRAIN"], "study_description_patterns": [], "series_description_patterns": []}
        assert _m(rule, body_part="", study_desc="", series_descs=[]) is False

    def test_empty_rule_returns_false(self):
        rule = {"body_parts": [], "study_description_patterns": [], "series_description_patterns": []}
        assert _m(rule, body_part="BRAIN", study_desc="MRI BRAIN", series_descs=["T1"]) is False

    def test_case_insensitive_pattern(self):
        rule = {"body_parts": [], "study_description_patterns": ["brain"], "series_description_patterns": []}
        assert _m(rule, study_desc="BRAIN MRI") is True

    def test_body_part_case_normalization(self):
        rule = {"body_parts": ["brain", "head"], "study_description_patterns": [], "series_description_patterns": []}
        assert _m(rule, body_part="BRAIN") is True

    def test_multiple_series_one_matches(self):
        rule = {"body_parts": [], "study_description_patterns": [], "series_description_patterns": ["FLAIR"]}
        assert _m(rule, series_descs=["T1 MPRAGE", "FLAIR", "DWI"]) is True

    # ── New region-necessary / modality-gate semantics ──────────────────────────

    def test_modality_gate_rejects_mismatch(self):
        """A PT rule never matches an MR study, even if a pattern coincidentally hits."""
        rule = {"modality": "PT", "body_parts": [], "study_description_patterns": [],
                "series_description_patterns": ["PET"]}
        assert _m(rule, series_descs=["PET AC"], modality="MR") is False

    def test_modality_set_pet_ct_study(self):
        """A PT rule applies to a PET/CT study whose study-level modality is CT,
        because the modality SET ({CT, PT}) carries the PT series."""
        rule = {"modality": "PT", "body_parts": [], "study_description_patterns": [],
                "series_description_patterns": ["pet"]}
        assert _m(rule, series_descs=["PET WB"], modality={"CT", "PT"}) is True

    def test_modality_alone_insufficient_when_region_declared(self):
        """The over-match fix: matching modality without region evidence is NOT enough."""
        rule = {"modality": "MR", "body_parts": ["ABDOMEN"],
                "study_description_patterns": ["abdomen"], "series_description_patterns": ["haste"]}
        assert _m(rule, modality="MR", body_part="", study_desc="", series_descs=["C+ 3D T1WI"]) is False

    def test_modality_plus_region_matches(self):
        rule = {"modality": "MR", "body_parts": [], "study_description_patterns": [],
                "series_description_patterns": ["t1"]}
        assert _m(rule, modality="MR", series_descs=["C+ 3D T1WI"]) is True

    def test_modality_only_rule_matches_on_gate(self):
        """A rule with a modality but no region conditions matches on the gate alone."""
        rule = {"modality": "MR", "body_parts": [], "study_description_patterns": [],
                "series_description_patterns": []}
        assert _m(rule, modality="MR") is True

    def test_modality_gate_skipped_when_study_has_no_modality(self):
        rule = {"modality": "PT", "body_parts": ["BRAIN"], "study_description_patterns": [],
                "series_description_patterns": []}
        assert _m(rule, body_part="BRAIN", modality="") is True

    def test_legacy_or_mode_modality_sufficient(self):
        """With require_region=False, a bare modality match is enough (legacy)."""
        rule = {"modality": "MR", "body_parts": ["ABDOMEN"],
                "study_description_patterns": [], "series_description_patterns": ["haste"]}
        assert _m(rule, modality="MR", body_part="", series_descs=["C+ 3D T1WI"], require_region=False) is True


class TestGetPriority:
    def test_empty_rules(self):
        assert RoutingService._get_priority([]) == 0

    def test_single_rule(self):
        assert RoutingService._get_priority([{"priority": 10}]) == 10

    def test_multiple_rules_returns_max(self):
        rules = [{"priority": 5}, {"priority": 20}, {"priority": 10}]
        assert RoutingService._get_priority(rules) == 20

    def test_missing_priority_defaults_zero(self):
        assert RoutingService._get_priority([{}]) == 0
