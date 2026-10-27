"""
report_schema.py
====================================================================
Typed result of one report-understanding run.

Values the report does not state stay null. This module does not score
urgency and does not build scheduler records.
====================================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


class UnderstandingStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class ReviewStatus(str, Enum):
    NEEDS_REVIEW = "NEEDS_REVIEW"
    READY = "READY"


class FindingMethod(str, Enum):
    NATIVE_TEXT = "native_text"
    OCR = "ocr"
    VISUAL = "visual"


class ValueOrigin(str, Enum):
    REPORTED = "reported"
    NORMALIZED = "normalized"
    UNKNOWN = "unknown"


class Uncertainty(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class SourcedValue:
    """One field, with the reason the value is present or still unknown."""

    value: str | int | float | bool | None
    origin: ValueOrigin


def unknown_value() -> SourcedValue:
    return SourcedValue(value=None, origin=ValueOrigin.UNKNOWN)


def reported_value(value: str | int | float | bool) -> SourcedValue:
    return SourcedValue(value=value, origin=ValueOrigin.REPORTED)


def normalized_value(value: str | int | float | bool) -> SourcedValue:
    return SourcedValue(value=value, origin=ValueOrigin.NORMALIZED)


@dataclass(frozen=True, slots=True)
class Evidence:
    """Exact source quote, or a visual note tied to a real image reference."""

    evidence_id: str
    filename: str
    reference: str
    page_number: int | None
    excerpt: str
    extraction_method: FindingMethod


@dataclass(frozen=True, slots=True)
class DefectCandidate:
    """One distinct finding. Null fields were not stated in the evidence."""

    candidate_id: str
    statement: str
    excerpt: str
    evidence: tuple[Evidence, ...]
    department: str | None
    reported_defect_type: str | None
    normalized_defect_type: SourcedValue
    reported_severity_text: str | None
    severity: SourcedValue
    reported_track_line: str | None
    track_line: SourcedValue
    chainage_km: SourcedValue
    due_date: SourcedValue
    days_overdue: SourcedValue
    has_tsr: SourcedValue
    tsr_speed_kmh: float | None
    reported_duration_min: SourcedValue
    reported_asset_ref: str | None
    extraction_method: FindingMethod
    missing_fields: tuple[str, ...]
    uncertainty: Uncertainty
    validation_warnings: tuple[str, ...]
    review_status: ReviewStatus


@dataclass(frozen=True, slots=True)
class RuledOutFinding:
    """A negated, nil, or lifted observation. It is not a defect candidate."""

    finding_id: str
    statement: str
    excerpt: str
    evidence: tuple[Evidence, ...]
    reason: str
    extraction_method: FindingMethod


@dataclass(frozen=True, slots=True)
class ReportWarning:
    code: str
    message: str
    reference: str | None = None


@dataclass(frozen=True, slots=True)
class ReportUnderstanding:
    """Summary plus separate candidates and ruled-out findings for one file."""

    report_id: str
    filename: str
    summary: str
    candidates: tuple[DefectCandidate, ...]
    ruled_out: tuple[RuledOutFinding, ...]
    warnings: tuple[ReportWarning, ...]
    status: UnderstandingStatus
    provider_name: str
    chunks_processed: int
    error: str | None
    nlp_status: str


PROVIDER_RESPONSE_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "findings"],
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "statement", "excerpt", "reference", "extraction_method", "uncertainty"],
                "properties": {
                    "kind": {"enum": ["defect", "ruled_out"]},
                    "statement": {"type": "string"},
                    "excerpt": {"type": "string"},
                    "reference": {"type": "string"},
                    "department": {"type": ["string", "null"]},
                    "reported_defect_type": {"type": ["string", "null"]},
                    "normalized_defect_type": {"type": ["string", "null"]},
                    "reported_severity_text": {"type": ["string", "null"]},
                    "severity": {"type": ["integer", "null"]},
                    "reported_track_line": {"type": ["string", "null"]},
                    "track_line": {"type": ["string", "null"]},
                    "chainage_km": {"type": ["number", "null"]},
                    "due_date": {"type": ["string", "null"]},
                    "days_overdue": {"type": ["number", "null"]},
                    "has_tsr": {"type": ["boolean", "null"]},
                    "tsr_speed_kmh": {"type": ["number", "null"]},
                    "reported_duration_min": {"type": ["number", "null"]},
                    "reported_asset_ref": {"type": ["string", "null"]},
                    "extraction_method": {"enum": ["native_text", "ocr", "visual"]},
                    "uncertainty": {"enum": ["low", "moderate", "high"]},
                    "ruled_out_reason": {"type": ["string", "null"]},
                },
            },
        },
    },
}


__all__ = [
    "PROVIDER_RESPONSE_SCHEMA",
    "DefectCandidate",
    "Evidence",
    "FindingMethod",
    "ReportUnderstanding",
    "ReportWarning",
    "ReviewStatus",
    "RuledOutFinding",
    "SourcedValue",
    "Uncertainty",
    "UnderstandingStatus",
    "ValueOrigin",
    "normalized_value",
    "reported_value",
    "unknown_value",
]
