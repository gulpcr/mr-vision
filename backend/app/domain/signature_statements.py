"""Attestation statements a signer affirms when electronically signing a report.

Statements are versioned: the signature record stores the version *and* the exact text
the signer saw (with their name filled in), so a later wording change never alters what a
past signature meant.
"""
from __future__ import annotations

CURRENT_STATEMENT_VERSION = "comprehensive-v1"

STATEMENTS: dict[str, str] = {
    "comprehensive-v1": (
        "I, {full_name}, attest that I have personally reviewed the complete imaging study, "
        "including all series, and every AI-generated finding, measurement and report section "
        "for this examination. I have verified the findings, corrected them where necessary, and "
        "accept this report as an accurate and complete statement of my professional "
        "interpretation. I understand that the AI output is decision support only and that I "
        "bear clinical responsibility for this report. I intend my electronic signature to be "
        "the legally binding equivalent of my handwritten signature."
    ),
}


def render_statement(full_name: str, version: str = CURRENT_STATEMENT_VERSION) -> str:
    return STATEMENTS[version].format(full_name=full_name.strip() or "the signing physician")
