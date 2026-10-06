"""AI-04: AI draft vs signed report metrics (numbers only)."""
from __future__ import annotations

from app.domain.report_edit_metrics import levenshtein, review_metrics, tokens


def test_levenshtein_on_words():
    assert levenshtein(tokens("no acute abnormality"), tokens("no acute abnormality")) == 0
    assert levenshtein(tokens("small mass left breast"), tokens("small cyst left breast")) == 1
    assert levenshtein([], tokens("a b c")) == 3


def test_read_only_ai_report_signed_as_is_is_unchanged():
    m = review_metrics({"ai_report": {"findings": "Liver normal.", "impression": "No acute findings."}}, None)
    assert m["edit_distance"] == 0 and m["similarity"] == 1.0 and m["outcome"] == "unchanged"


def test_edited_structured_report_counts_slot_discrepancies():
    ai = {"opinion": "Suspicious mass in the right breast.", "birads_right": "4", "birads_left": "1",
          "density_right": "C", "density_left": "C"}
    signed = {"opinion": "Suspicious mass in the right breast, biopsy advised.", "birads_right": "5",
              "birads_left": "1", "density_right": "C", "density_left": "C"}
    m = review_metrics(ai, signed)
    assert m["slots_compared"] == 4 and m["slots_mismatched"] == 1
    assert 0 < m["similarity"] < 1 and m["outcome"] == "major_edit"


def test_minor_text_edit():
    ai = {"opinion": " ".join(["word"] * 40)}
    signed = {"opinion": " ".join(["word"] * 39 + ["term"])}
    assert review_metrics(ai, signed)["outcome"] == "minor_edit"
