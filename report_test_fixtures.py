"""
report_test_fixtures.py
====================================================================
One optional scripted mock for report DEMO-MR-26027-041.

It is used only when REPORT_AI_PROVIDER=mock and
REPORT_AI_MOCK_FIXTURE=demo_mr_26027_041, and only after the document
reader's text and images match that report. The filename is not used.
A mismatch keeps the ordinary empty mock. This module does not call a
live model and does not create TrackDefect records.
====================================================================
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from report_documents import DocumentBundle, DocumentSection, ReadStatus
from report_provider import (
    FIXTURE_NOT_MATCHED_MESSAGE,
    MockReportProvider,
    ProviderRequest,
    ProviderResponse,
    ReportUnderstandingProvider,
    SCRIPTED_MOCK_LABEL,
)

DEMO_FIXTURE_ID = "demo_mr_26027_041"
_REPORT_ID = "DEMO-MR-26027-041"
_INTERPRETATION = (
    "The simulated multi-department report contains three reported maintenance findings: "
    "a rail-joint defect reported as WELD_FAILURE at KM 67.5 DN, a TAMPING_REQUIRED finding at KM 68.2 DN, "
    "and an intermittent SIGNAL_INTERLOCK indication at KM 9.4 UP_FAST. "
    "The signal-interlocking record has unknown active TSR status and requires review. "
    "A rail-fracture observation was explicitly negative, and a vibration observation near KM 69.0 DN remains unclassified. "
    "Three supplied photographs provide contextual evidence, but their capture locations and dates have not been verified."
)
_DISCLAIMER = "SIMULATED TEST DATA - NOT AN OFFICIAL RAILWAY RECORD"
_EXTRACTION_NOTE = (
    "Page text and source images were extracted by the document reader from the uploaded file. "
    "The maintenance paragraph above is a scripted mock interpretation, not a live AI result."
)
_IMAGE_NOTES = (
    "The source location and capture date are unverified; do not assume it depicts KM 67.5 DN.",
    "This is a visual description only, not an engineering diagnosis.",
    "The image does not, by itself, establish a signal/interlocking failure or confirm that the work occurred at KM 9.4 UP_FAST.",
)
_SIGNATURE = (
    _REPORT_ID,
    "Page 1 of 4",
    "Page 2 of 4",
    "Page 3 of 4",
    "Page 4 of 4",
    "WELD_FAILURE",
    "TAMPING_REQUIRED",
    "SIGNAL_INTERLOCK",
    "KM 67.5",
    "KM 68.2",
    "KM 9.4",
    "UP_FAST",
    "No rail fracture was observed at KM 67.1 on DN",
    "Vibration was reported near KM 69.0 on DN",
    _DISCLAIMER,
    "do not assume it depicts KM 67.5 DN",
    "not an engineering diagnosis",
    "signal/interlocking failure",
    "The report does not state whether an active TSR is in force",
    "The report does not identify a confirmed defect type, numeric severity, due date, days overdue, or TSR status",
)
_ANCHORS: tuple[tuple[str, int, str, str], ...] = (
    ("D-01", 1, "FINDING D-01", "30 km/h."),
    ("D-02", 2, "FINDING D-02", "the photo must not independently establish those fields."),
    ("D-03", 3, "FINDING D-03", "active TSR is in force."),
    ("N-01", 4, "OBSERVATION N-01", "in this observation."),
    ("A-01", 4, "OBSERVATION A-01", "or TSR status."),
)


@dataclass(frozen=True, slots=True)
class _DemoMatch:
    summary: str
    findings: tuple[dict[str, object], ...]


class _ScriptedFixtureProvider:
    """Canned interpretation for one matched document. Excerpts stay in the reader text."""

    name = "mock"
    supports_images = False
    fixture_matched = True
    fixture_notice = None

    def __init__(self, fixture_id: str, summary: str, findings: tuple[dict[str, object], ...]) -> None:
        self.scripted_fixture = fixture_id
        self.scripted_summary = summary
        self._findings = findings
        self.requests: list[ProviderRequest] = []

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        rows = [row for row in self._findings if str(row["excerpt"]) in request.user_prompt]
        return ProviderResponse(raw_text=json.dumps({"summary": "", "findings": rows}))


class _UnmatchedFixtureProvider:
    """Ordinary empty mock, with an explicit notice that the fixture did not match."""

    name = "mock"
    supports_images = False
    fixture_matched = False
    scripted_fixture = None
    scripted_summary = None
    fixture_notice = FIXTURE_NOT_MATCHED_MESSAGE

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return ProviderResponse(raw_text='{"summary": "", "findings": []}')


def maybe_scripted_fixture(
    model: ReportUnderstandingProvider,
    bundle: DocumentBundle,
    environ: Mapping[str, str] | None = None,
) -> ReportUnderstandingProvider:
    """Use the demo fixture only for an empty mock and a matching document."""
    if not isinstance(model, MockReportProvider) or model._script:
        return model
    source = os.environ if environ is None else environ
    provider_name = source.get("REPORT_AI_PROVIDER", "mock").strip() or "mock"
    if provider_name.casefold() != "mock":
        return model
    fixture_name = source.get("REPORT_AI_MOCK_FIXTURE", "").strip()
    if fixture_name != DEMO_FIXTURE_ID:
        if not fixture_name:
            return model
        return _UnmatchedFixtureProvider()
    match = match_demo_report(bundle)
    if match is None:
        return _UnmatchedFixtureProvider()
    return _ScriptedFixtureProvider(DEMO_FIXTURE_ID, match.summary, match.findings)


def match_demo_report(bundle: DocumentBundle) -> _DemoMatch | None:
    """Return canned findings when the reader output is this report. Filename is ignored."""
    if not isinstance(bundle, DocumentBundle) or bundle.status is not ReadStatus.COMPLETE:
        return None
    if len(bundle.sections) != 4:
        return None
    pages: dict[int, DocumentSection] = {}
    for section in bundle.sections:
        if section.page_number not in {1, 2, 3, 4} or section.page_number in pages:
            return None
        pages[section.page_number] = section
    folded = _fold("\n".join(section.native_text for section in bundle.sections))
    if any(_fold(phrase) not in folded for phrase in _SIGNATURE):
        return None
    images = _images_by_page(bundle)
    if not images.get(1) or not images.get(2) or not images.get(3) or images.get(4):
        return None
    excerpts: dict[str, tuple[str, str]] = {}
    for label, page_number, start, end in _ANCHORS:
        section = pages[page_number]
        excerpt = _slice(section.native_text, start, end)
        if excerpt is None:
            return None
        excerpts[label] = (section.reference, excerpt)
    image_refs = tuple(reference for page in (1, 2, 3) for reference in images[page])
    return _DemoMatch(summary=_summary(image_refs, folded), findings=_findings(excerpts))


def _summary(image_refs: tuple[str, ...], folded: str) -> str:
    notes = [note for note in _IMAGE_NOTES if note in folded]
    lines = [
        SCRIPTED_MOCK_LABEL,
        _INTERPRETATION,
        _DISCLAIMER,
        _EXTRACTION_NOTE,
    ]
    if notes:
        lines.append("Extracted source-image notes: " + " ".join(notes))
    if image_refs:
        lines.append("Document reader image references: " + ", ".join(image_refs) + ".")
    return "\n".join(lines)


def _findings(excerpts: dict[str, tuple[str, str]]) -> tuple[dict[str, object], ...]:
    d01_ref, d01 = excerpts["D-01"]
    d02_ref, d02 = excerpts["D-02"]
    d03_ref, d03 = excerpts["D-03"]
    n01_ref, n01 = excerpts["N-01"]
    a01_ref, a01 = excerpts["A-01"]
    return (
        _row(
            d01,
            d01_ref,
            department="TMS",
            reported_defect_type="WELD_FAILURE",
            normalized_defect_type="WELD_FAILURE",
            reported_severity_text="84/100",
            severity=84,
            reported_track_line="DN",
            track_line="DN",
            chainage_km=67.5,
            days_overdue=22,
            has_tsr=True,
            tsr_speed_kmh=30,
            reported_duration_min=60,
            reported_asset_ref="D-01",
        ),
        _row(
            d02,
            d02_ref,
            department="TMS",
            reported_defect_type="TAMPING_REQUIRED",
            normalized_defect_type="TAMPING_REQUIRED",
            reported_severity_text="58/100",
            severity=58,
            reported_track_line="DN",
            track_line="DN",
            chainage_km=68.2,
            days_overdue=12,
            has_tsr=False,
            reported_duration_min=120,
            reported_asset_ref="D-02",
        ),
        _row(
            d03,
            d03_ref,
            department="SMMS",
            reported_defect_type="SIGNAL_INTERLOCK",
            normalized_defect_type="SIGNAL_INTERLOCK",
            reported_severity_text="63/100",
            severity=63,
            reported_track_line="UP_FAST",
            track_line="UP_FAST",
            chainage_km=9.4,
            days_overdue=6,
            reported_asset_ref="D-03",
        ),
        _row(n01, n01_ref, kind="ruled_out", ruled_out_reason="negated_finding", reported_asset_ref="N-01"),
        _row(
            a01,
            a01_ref,
            reported_track_line="DN",
            track_line="DN",
            chainage_km=69.0,
            reported_asset_ref="A-01",
        ),
    )


def _row(excerpt: str, reference: str, **values: object) -> dict[str, object]:
    row: dict[str, object] = {
        "kind": "defect",
        "statement": excerpt,
        "excerpt": excerpt,
        "reference": reference,
        "department": None,
        "reported_defect_type": None,
        "normalized_defect_type": None,
        "reported_severity_text": None,
        "severity": None,
        "reported_track_line": None,
        "track_line": None,
        "chainage_km": None,
        "due_date": None,
        "days_overdue": None,
        "has_tsr": None,
        "tsr_speed_kmh": None,
        "reported_duration_min": None,
        "reported_asset_ref": None,
        "extraction_method": "native_text",
        "uncertainty": "low",
        "ruled_out_reason": None,
    }
    row.update(values)
    return row


def _images_by_page(bundle: DocumentBundle) -> dict[int, tuple[str, ...]]:
    found: dict[int, list[str]] = {}
    for section in bundle.sections:
        if section.page_number is None:
            continue
        for image in section.images:
            if image.reference and image.data:
                found.setdefault(section.page_number, []).append(image.reference)
    return {page: tuple(references) for page, references in found.items()}


def _slice(text: str, start: str, end: str) -> str | None:
    if text.count(start) != 1 or text.count(end) != 1:
        return None
    begin = text.find(start)
    finish = text.find(end, begin)
    if finish < 0:
        return None
    return text[begin : finish + len(end)]


def _fold(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


__all__ = [
    "DEMO_FIXTURE_ID",
    "match_demo_report",
    "maybe_scripted_fixture",
]
