"""Heading groups and the local rule summary. No database writes and no apply path."""

from __future__ import annotations

import hashlib
from pathlib import Path

from defects import DEFECT_TYPE_SPEC, DefectType
from report_documents import DocumentBundle, DocumentSection, ExtractionMethod, ReadStatus, TextBlock, read_report_document
from report_nlp import LocalTextProvider
from report_pipeline import simplify_report
from report_preview import PreviewStatus, validate_candidates
from report_schema import ReviewStatus, Uncertainty

_DEMO_PDF = Path(__file__).parent / "fixtures" / "ai1_railway_maintenance_demo_report_4_pages.pdf"

_OVERVIEW = (
    "Overview: this page mentions a track-tamping requirement and a signalling issue "
    "so a reader can see the scope. It is an instruction, not a finding."
)
_WELD = """FINDING B-07 | WELD_FAILURE
DEFECT TYPE
 WELD_FAILURE (reported)
LOCATION
 KM 80.0, DN
SEVERITY
 70 / 100 (reported)
DUE DATE / OVERDUE
 01 Jan 2026 / 3 days
ACTIVE TSR
 Yes - 20 km/h
REPORTED DURATION
 15 minutes (estimate)
"""
_TAMPING = """FINDING B-08 | TAMPING_REQUIRED
DEFECT TYPE
 TAMPING_REQUIRED (reported)
LOCATION
 KM 81.0, DN
SEVERITY
 30 / 100 (reported)
DUE DATE / OVERDUE
 02 Jan 2026 / 4 days
ACTIVE TSR
 No
"""
_SIGNAL = """FINDING C-03 | SIGNAL_INTERLOCK
DEPARTMENT
 SMMS
LOCATION
 KM 20.0, UP_FAST
SEVERITY
 50 / 100 (reported)
DUE DATE / OVERDUE
 03 Jan 2026 / 5 days
ACTIVE TSR
 NOT STATED - UNKNOWN
The department note and the reported duration are unrelated to the missing speed restriction.
REPORTED DURATION
 10 minutes
"""
_NEGATIVE = """OBSERVATION N-07 - EXPLICITLY NEGATIVE
No rail fracture was observed at KM 79.0 on DN.
This statement must not create a positive RAIL_FRACTURE defect.
"""
_UNCERTAIN = """OBSERVATION A-07 - INCOMPLETE
Vibration was reported near KM 82.0 on DN. The cause has not been determined.
The report does not identify a confirmed defect type, numeric severity, due date, days overdue, or TSR status.
"""
_REGISTER = """SIMULATED FIELD REGISTER
B-07
WELD_FAILURE
KM 80.0 DN
70
Yes, 20 km/h
B-08
TAMPING_REQUIRED
KM 81.0 DN
C-03
SIGNAL_INTERLOCK
KM 20.0 UP_FAST
"""


def _section(reference: str, page: int, text: str) -> DocumentSection:
    return DocumentSection(
        reference=reference,
        page_number=page,
        native_text=text,
        ocr_text="",
        blocks=(
            TextBlock(
                block_id=f"{reference}/text:1",
                text=text,
                method=ExtractionMethod.NATIVE_TEXT,
                reference=reference,
                page_number=page,
            ),
        ),
        images=(),
        methods=(ExtractionMethod.NATIVE_TEXT,),
    )


def _bundle(sections: list[DocumentSection], filename: str) -> DocumentBundle:
    return DocumentBundle(
        filename=filename,
        media_kind="pdf",
        sha256=hashlib.sha256(filename.encode("utf-8")).hexdigest(),
        byte_size=128,
        status=ReadStatus.COMPLETE,
        sections=tuple(sections),
        warnings=(),
        methods_used=(ExtractionMethod.NATIVE_TEXT,),
    )


def _understand(bundle: DocumentBundle):
    provider = LocalTextProvider()
    understanding = simplify_report(bundle, provider)
    preview = validate_candidates(understanding, existing_defects=())
    return provider, understanding, preview


def _synthetic():
    bundle = _bundle(
        [
            _section("page:1", 1, "\n".join((_OVERVIEW, _WELD, _TAMPING, _SIGNAL))),
            _section("page:2", 2, "\n".join((_NEGATIVE, _UNCERTAIN))),
            _section("page:3", 3, _REGISTER),
        ],
        "section-notes.pdf",
    )
    return (bundle, *_understand(bundle))


def _demo():
    bundle = read_report_document(_DEMO_PDF.read_bytes(), _DEMO_PDF.name)
    return (bundle, *_understand(bundle))


def _by_asset(understanding, asset: str):
    found = [item for item in understanding.candidates if item.reported_asset_ref == asset]
    assert len(found) == 1
    return found[0]


def _preview_for(preview, candidate_id: str):
    found = [item for item in preview.records if item.source_finding_id == candidate_id]
    assert len(found) == 1
    return found[0]


def _excerpts_are_source_text(bundle: DocumentBundle, understanding) -> None:
    pages = {section.reference: section for section in bundle.sections}
    findings = (*understanding.candidates, *understanding.ruled_out)
    assert findings
    for item in findings:
        evidence = item.evidence[0]
        section = pages[evidence.reference]
        assert item.excerpt == evidence.excerpt
        assert item.excerpt in section.native_text or item.excerpt in section.ocr_text


def test_headed_finding_is_one_candidate_not_a_heading_and_a_category_line() -> None:
    _bundle_text, _provider, understanding, _preview = _synthetic()
    weld = _by_asset(understanding, "B-07")
    assert weld.normalized_defect_type.value == "WELD_FAILURE"
    assert "FINDING B-07" in weld.excerpt
    assert "WELD_FAILURE (reported)" in weld.excerpt
    assert sum("WELD_FAILURE" in item.excerpt for item in understanding.candidates) == 1
    assert len(understanding.candidates) == 4


def test_later_field_register_does_not_add_another_operational_job() -> None:
    _bundle_text, _provider, understanding, preview = _synthetic()
    weld_jobs = [
        item
        for item in preview.records
        if item.scheduling_record is not None and item.evidence.normalized_defect_type == "WELD_FAILURE"
    ]
    assert len(weld_jobs) == 1
    assert weld_jobs[0].evidence.chainage_km == 80.0
    assert all("FIELD REGISTER" not in item.excerpt for item in understanding.candidates)
    assert all(item.evidence[0].reference != "page:3" for item in understanding.candidates)


def test_nearby_findings_of_different_categories_stay_separate() -> None:
    _bundle_text, _provider, understanding, preview = _synthetic()
    weld = _by_asset(understanding, "B-07")
    tamping = _by_asset(understanding, "B-08")
    assert weld.chainage_km.value == 80.0
    assert tamping.chainage_km.value == 81.0
    assert weld.normalized_defect_type.value == "WELD_FAILURE"
    assert tamping.normalized_defect_type.value == "TAMPING_REQUIRED"
    weld_record = _preview_for(preview, weld.candidate_id)
    tamping_record = _preview_for(preview, tamping.candidate_id)
    assert weld_record.status is PreviewStatus.READY_TO_APPLY
    assert tamping_record.status is PreviewStatus.READY_TO_APPLY
    assert weld_record.duplicate_of == ()
    assert tamping_record.duplicate_of == ()
    assert weld_record.scheduling_record is not None
    assert tamping_record.scheduling_record is not None
    assert weld_record.scheduling_record.defect_id != tamping_record.scheduling_record.defect_id


def test_overview_mentioning_tamping_and_signalling_is_not_a_defect() -> None:
    bundle, _provider, understanding, _preview = _synthetic()
    assert all(_OVERVIEW not in item.excerpt for item in (*understanding.candidates, *understanding.ruled_out))
    assert not any(
        item.normalized_defect_type.value == "TAMPING_REQUIRED" and item.department == "SMMS"
        for item in understanding.candidates
    )
    _excerpts_are_source_text(bundle, understanding)


def test_negative_statement_does_not_become_a_rail_fracture_job() -> None:
    _bundle_text, _provider, understanding, preview = _synthetic()
    assert all(item.normalized_defect_type.value != "RAIL_FRACTURE" for item in understanding.candidates)
    ruled = [item for item in understanding.ruled_out if "No rail fracture was observed" in item.excerpt]
    assert len(ruled) == 1
    assert ruled[0].reason == "negated_finding"
    assert "must not create a positive RAIL_FRACTURE" in ruled[0].excerpt
    assert all(item.status is not PreviewStatus.READY_TO_APPLY or item.evidence.normalized_defect_type != "RAIL_FRACTURE" for item in preview.records)
    assert not any(item.evidence.normalized_defect_type == "RAIL_FRACTURE" for item in preview.records)


def test_uncertain_observation_stays_in_review_without_invented_fields() -> None:
    _bundle_text, _provider, understanding, preview = _synthetic()
    observation = _by_asset(understanding, "A-07")
    assert observation.normalized_defect_type.value is None
    assert observation.severity.value is None
    assert observation.uncertainty is Uncertainty.HIGH
    assert observation.review_status is ReviewStatus.NEEDS_REVIEW
    assert observation.chainage_km.value == 82.0
    record = _preview_for(preview, observation.candidate_id)
    assert record.status is PreviewStatus.NEEDS_REVIEW
    assert record.scheduling_record is None
    assert record.evidence.normalized_defect_type is None
    assert record.evidence.severity is None


def test_unknown_tsr_stays_unknown_when_other_fields_are_present() -> None:
    _bundle_text, _provider, understanding, preview = _synthetic()
    signal = _by_asset(understanding, "C-03")
    assert signal.normalized_defect_type.value == "SIGNAL_INTERLOCK"
    assert signal.track_line.value == "UP_FAST"
    assert signal.chainage_km.value == 20.0
    assert signal.severity.value == 50
    assert signal.days_overdue.value == 5.0
    assert signal.department == "SMMS"
    assert signal.reported_duration_min.value == 10.0
    assert signal.has_tsr.value is None
    assert signal.review_status is ReviewStatus.NEEDS_REVIEW
    record = _preview_for(preview, signal.candidate_id)
    assert record.missing_fields == ("has_tsr",)
    assert record.status is PreviewStatus.NEEDS_REVIEW
    assert record.scheduling_record is None
    assert record.evidence.severity == 50
    assert record.evidence.track_line == "UP_FAST"
    assert record.evidence.chainage_km == 20.0
    assert record.evidence.days_overdue == 5.0


def test_report_duration_stays_evidence_and_planner_duration_stays_configured() -> None:
    _bundle_text, _provider, understanding, preview = _synthetic()
    weld = _by_asset(understanding, "B-07")
    record = _preview_for(preview, weld.candidate_id)
    assert record.scheduling_record is not None
    assert record.evidence.reported_duration_min == 15.0
    assert record.scheduling_record.required_block_min == DEFECT_TYPE_SPEC[DefectType.WELD_FAILURE][0]
    assert record.scheduling_record.required_block_min != 15.0


def test_headingless_report_gets_one_field_line_per_finding() -> None:
    first = "Weld failure at KM 40.0 on the DN line, severity 40, 3 days overdue, no TSR."
    second = "Tamping required at KM 41.2 on the DN line, severity 22, 4 days overdue, no TSR."
    third = "Signal interlock at KM 12.0 on the UP fast line, severity 18, 5 days overdue."
    text = " ".join((first, second, third))
    bundle = _bundle([_section("page:1", 1, text)], "plain-notes.pdf")
    provider, understanding, preview = _understand(bundle)
    assert provider.used_heading_groups is False
    assert [item.excerpt for item in understanding.candidates] == [first, second, third]
    assert understanding.summary == (
        "Weld failure · KM 40 DN · Severity 40 · 3 days overdue · No TSR. "
        "Tamping required · KM 41.2 DN · Severity 22 · 4 days overdue · No TSR. "
        "Signal interlock · KM 12 UP_FAST · Severity 18 · 5 days overdue · TSR unknown · Needs review."
    )
    assert understanding.summary != text
    assert all(sentence not in understanding.summary for sentence in (first, second, third))
    assert understanding.ruled_out == ()
    _excerpts_are_source_text(bundle, understanding)
    # KM 40 is a four-line section, so "DN" is rejected by the existing topology check.
    # The summary still names each finding; only the planner record is withheld.
    assert all(item.scheduling_record is None for item in preview.records)
    assert [item.status for item in preview.records] == [
        PreviewStatus.REJECTED,
        PreviewStatus.REJECTED,
        PreviewStatus.NEEDS_REVIEW,
    ]
    assert all("not present at KM" in " ".join(item.reasons) for item in preview.records[:2])
    assert preview.records[2].missing_fields == ("has_tsr",)

    schedulable = _bundle(
        [_section("page:1", 1, "Weld failure at KM 80.0 on the DN line, severity 40, 3 days overdue, no TSR.")],
        "plain-notes-2.pdf",
    )
    _provider, understanding, preview = _understand(schedulable)
    assert understanding.summary == "Weld failure · KM 80 DN · Severity 40 · 3 days overdue · No TSR."
    scheduled = [item for item in preview.records if item.scheduling_record is not None]
    assert [(item.evidence.normalized_defect_type, item.evidence.chainage_km, item.evidence.severity) for item in scheduled] == [
        ("WELD_FAILURE", 80.0, 40),
    ]
    assert scheduled[0].scheduling_record.required_block_min == DEFECT_TYPE_SPEC[DefectType.WELD_FAILURE][0]


def test_demo_report_groups_findings_and_writes_a_plain_summary() -> None:
    bundle, provider, understanding, preview = _demo()
    assert provider.name == "local-text"
    assert provider.used_heading_groups is True
    assert len(understanding.candidates) == 4
    assert len(understanding.ruled_out) == 1

    weld = _by_asset(understanding, "D-01")
    tamping = _by_asset(understanding, "D-02")
    signal = _by_asset(understanding, "D-03")
    observation = _by_asset(understanding, "A-01")
    assert weld.normalized_defect_type.value == "WELD_FAILURE"
    assert weld.chainage_km.value == 67.5
    assert weld.track_line.value == "DN"
    assert weld.severity.value == 84
    assert weld.days_overdue.value == 22.0
    assert weld.has_tsr.value is True
    assert "FINDING D-01" in weld.excerpt
    assert "WELD_FAILURE (reported)" in weld.excerpt
    assert "do not assume it depicts" not in weld.excerpt.casefold()

    assert tamping.normalized_defect_type.value == "TAMPING_REQUIRED"
    assert tamping.chainage_km.value == 68.2
    assert tamping.department == "TMS"
    assert tamping.has_tsr.value is False
    assert signal.normalized_defect_type.value == "SIGNAL_INTERLOCK"
    assert signal.chainage_km.value == 9.4
    assert signal.track_line.value == "UP_FAST"
    assert signal.severity.value == 63
    assert signal.days_overdue.value == 6.0
    assert signal.has_tsr.value is None
    assert signal.review_status is ReviewStatus.NEEDS_REVIEW
    assert "does not, by itself, establish" not in signal.excerpt.casefold()
    assert "no tsr value should be inferred" not in signal.excerpt.casefold()

    signal_record = _preview_for(preview, signal.candidate_id)
    assert signal_record.status is PreviewStatus.NEEDS_REVIEW
    assert signal_record.scheduling_record is None
    assert signal_record.missing_fields == ("has_tsr",)
    assert signal_record.evidence.severity == 63
    assert signal_record.evidence.days_overdue == 6.0

    assert observation.normalized_defect_type.value is None
    assert observation.severity.value is None
    assert observation.chainage_km.value == 69.0
    assert observation.uncertainty is Uncertainty.HIGH
    assert observation.review_status is ReviewStatus.NEEDS_REVIEW
    observation_record = _preview_for(preview, observation.candidate_id)
    assert observation_record.scheduling_record is None

    assert all(item.normalized_defect_type.value != "RAIL_FRACTURE" for item in understanding.candidates)
    assert understanding.ruled_out[0].reason == "negated_finding"
    assert "No rail fracture was observed" in understanding.ruled_out[0].excerpt
    assert not any(item.evidence.normalized_defect_type == "RAIL_FRACTURE" for item in preview.records)

    scheduled = [item for item in preview.records if item.scheduling_record is not None]
    assert [(item.evidence.normalized_defect_type, item.evidence.chainage_km) for item in scheduled] == [
        ("WELD_FAILURE", 67.5),
        ("TAMPING_REQUIRED", 68.2),
    ]
    assert all("three reported maintenance findings" not in item.excerpt for item in understanding.candidates)
    assert all("FIELD REGISTER" not in item.excerpt for item in (*understanding.candidates, *understanding.ruled_out))
    assert not any(
        item.normalized_defect_type.value == "TAMPING_REQUIRED" and item.department == "SMMS"
        for item in understanding.candidates
    )
    _excerpts_are_source_text(bundle, understanding)

    summary = understanding.summary
    assert len(summary) < 900
    assert not summary.endswith("...")
    assert summary.endswith(".")
    assert "REPORT ID" not in summary
    assert "REPORTED VALUES" not in summary
    assert "photo" not in summary.casefold()
    assert "language model" not in summary.casefold()
    lines = summary.rstrip(".").split(". ")
    assert len(lines) == len(understanding.candidates) + len(understanding.ruled_out) == 5
    assert lines[0] == "Weld failure · KM 67.5 DN · Severity 84 · 22 days overdue · Active TSR 30 km/h"
    assert lines[1] == "Tamping required · KM 68.2 DN · Severity 58 · 12 days overdue · No TSR"
    assert lines[2] == "Signal interlock · KM 9.4 UP_FAST · Severity 63 · 6 days overdue · TSR unknown · Needs review"
    assert "No TSR" not in lines[2] and "Active TSR" not in lines[2]
    observation_line = lines[3]
    assert observation_line.startswith("Uncertain observation · Near KM 69 DN")
    assert "Defect type, severity, days overdue and TSR unknown" in observation_line
    assert "not a confirmed defect" in observation_line.casefold()
    assert observation_line.endswith("Needs review")
    assert "weld" not in observation_line.casefold()
    assert "tamping" not in observation_line.casefold()
    assert "signal" not in observation_line.casefold()
    assert lines[4] == "Negative rail fracture observation · Ruled out; not a maintenance job"
    assert weld.excerpt not in summary
    assert "three reported maintenance findings" not in summary
    assert "urgency" not in summary.casefold()
    assert "15 min" not in summary and "estimate" not in summary.casefold()
