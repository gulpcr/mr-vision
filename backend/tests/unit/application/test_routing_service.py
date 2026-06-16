from __future__ import annotations

import pytest

from app.application.routing_service import RoutingService
from app.domain.enums import BodyPart
from app.domain.models import Series, Study


class TestRuleMatches:
    """Tests for RoutingService._rule_matches static method."""

    def test_body_part_match(self):
        rule = {"body_parts": ["BRAIN", "HEAD"], "study_description_patterns": [], "series_description_patterns": []}
        assert RoutingService._rule_matches(rule, "BRAIN", "", []) is True

    def test_body_part_no_match(self):
        rule = {"body_parts": ["BRAIN"], "study_description_patterns": [], "series_description_patterns": []}
        assert RoutingService._rule_matches(rule, "KNEE", "", []) is False

    def test_body_part_empty_field_skipped(self):
        """Empty body_part field is skipped, not penalized."""
        rule = {"body_parts": ["BRAIN"], "study_description_patterns": [], "series_description_patterns": []}
        # body_part is empty -> condition skipped; no other conditions -> no match
        assert RoutingService._rule_matches(rule, "", "", []) is False

    def test_study_description_pattern_match(self):
        rule = {"body_parts": [], "study_description_patterns": ["BRAIN.*MRI"], "series_description_patterns": []}
        assert RoutingService._rule_matches(rule, "", "BRAIN MRI PROTOCOL", []) is True

    def test_study_description_pattern_no_match(self):
        rule = {"body_parts": [], "study_description_patterns": ["BRAIN.*MRI"], "series_description_patterns": []}
        assert RoutingService._rule_matches(rule, "", "KNEE SCAN", []) is False

    def test_series_description_pattern_match(self):
        rule = {"body_parts": [], "study_description_patterns": [], "series_description_patterns": ["T1.*MPRAGE"]}
        assert RoutingService._rule_matches(rule, "", "", ["T1 MPRAGE", "FLAIR"]) is True

    def test_series_description_pattern_no_match(self):
        rule = {"body_parts": [], "study_description_patterns": [], "series_description_patterns": ["T1.*MPRAGE"]}
        assert RoutingService._rule_matches(rule, "", "", ["FLAIR", "DWI"]) is False

    def test_or_logic_any_condition_sufficient(self):
        """If body_part matches, the rule matches even if study desc doesn't."""
        rule = {
            "body_parts": ["BRAIN"],
            "study_description_patterns": ["SPINE"],
            "series_description_patterns": [],
        }
        assert RoutingService._rule_matches(rule, "BRAIN", "BRAIN MRI", []) is True

    def test_no_conditions_evaluable_returns_false(self):
        """If all fields are empty, no match."""
        rule = {"body_parts": ["BRAIN"], "study_description_patterns": [], "series_description_patterns": []}
        assert RoutingService._rule_matches(rule, "", "", []) is False

    def test_empty_rule_returns_false(self):
        rule = {"body_parts": [], "study_description_patterns": [], "series_description_patterns": []}
        assert RoutingService._rule_matches(rule, "BRAIN", "MRI BRAIN", ["T1"]) is False

    def test_case_insensitive_pattern(self):
        rule = {"body_parts": [], "study_description_patterns": ["brain"], "series_description_patterns": []}
        assert RoutingService._rule_matches(rule, "", "BRAIN MRI", []) is True

    def test_body_part_case_normalization(self):
        """Rule body_parts are uppercased internally."""
        rule = {"body_parts": ["brain", "head"], "study_description_patterns": [], "series_description_patterns": []}
        assert RoutingService._rule_matches(rule, "BRAIN", "", []) is True

    def test_multiple_series_one_matches(self):
        rule = {"body_parts": [], "study_description_patterns": [], "series_description_patterns": ["FLAIR"]}
        assert RoutingService._rule_matches(rule, "", "", ["T1 MPRAGE", "FLAIR", "DWI"]) is True

    def test_enabled_field(self):
        """Disabled rules — _rule_matches itself doesn't check enabled; route_study does."""
        rule = {
            "body_parts": ["BRAIN"],
            "study_description_patterns": [],
            "series_description_patterns": [],
            "enabled": False,
        }
        # _rule_matches only checks conditions, not enabled flag
        assert RoutingService._rule_matches(rule, "BRAIN", "", []) is True


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
