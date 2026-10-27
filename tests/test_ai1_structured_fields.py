"""Structured fields and negative findings. Synthetic text plus the fictional demo."""

from __future__ import annotations

import hashlib
from pathlib import Path

from defects import DEFECT_TYPE_SPEC, DefectType
from report_documents import (
    DocumentBundle,
    DocumentSection,
    ExtractionMethod,
    ReadStatus,
    TextBlock,
    read_report_document,
)
from report_nlp import LocalTextProvider
from report_pipeline import simplify_report
from report_preview import PreviewStatus, validate_candidates
from report_schema import ReviewStatus, Uncertainty, ValueOrigin

_DEMO_PDF = Path(__file__).parent / "fixtures" / "ai1_railway_maintenance_demo_report_4_pages.pdf"


def _section(reference: str, page: int | None, text: str) -> DocumentSection:
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


def _bundle(text: str) -> DocumentBundle:
    return DocumentBundle(
        filename="structured-notes.pdf",
        media_kind="pdf",
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        byte_size=len(text.encode("utf-8")),
        status=ReadStatus.COMPLETE,
        sections=(_section("page:1", 1, text),),
        warnings=(),
        methods_used=(ExtractionMethod.NATIVE_TEXT,),
    )


def _read(text: str):
    return simplify_report(_bundle(text), LocalTextProvider())


def test_due_date_on_the_same_line_is_kept_as_written() -> None:
    text = (
        "Weld failure at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR, "
        "due date: 4 October 2026."
    )
    candidate = _read(text).candidates[0]
    assert candidate.due_date.value == "4 October 2026"
    assert candidate.due_date.origin is ValueOrigin.REPORTED
    assert candidate.due_date.value in candidate.excerpt
    assert candidate.excerpt == text


def test_due_date_on_the_following_line_is_kept() -> None:
    text = "Weld failure at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR\nDue date\n4 October 2026"
    candidate = _read(text).candidates[0]
    assert candidate.excerpt == text
    assert candidate.due_date.value == "4 October 2026"
    assert candidate.days_overdue.value == 4.0
    assert "4 October 2026" in candidate.excerpt


def test_labelled_due_date_and_overdue_on_following_lines_stay_separate() -> None:
    text = """FINDING B-21 | WELD_FAILURE
LOCATION
 KM 80.0, DN
SEVERITY
 84
DUE DATE / OVERDUE
 24 Sep 2026 / 22 days
ACTIVE TSR
 Yes - 30 km/h
REPORTED DURATION
 15 minutes
"""
    result = _read(text)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.excerpt == text.strip() or candidate.excerpt == text
    assert candidate.excerpt in text
    assert candidate.due_date.value == "24 Sep 2026"
    assert candidate.days_overdue.value == 22.0
    assert candidate.severity.value == 84
    assert candidate.has_tsr.value is True
    assert candidate.reported_duration_min.value == 15.0
    preview = validate_candidates(result, existing_defects=()).records[0]
    assert preview.scheduling_record is not None
    assert preview.scheduling_record.days_overdue == 22.0
    assert preview.evidence.reported_due_date == "24 Sep 2026"


def test_ambiguous_invalid_and_non_due_dates_stay_unknown() -> None:
    ambiguous = _read(
        "Weld failure at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR, due 05-06-2026."
    ).candidates[0]
    invalid = _read(
        "Weld failure at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR, due 32 Sep 2026."
    ).candidates[0]
    bad_iso = _read(
        "Weld failure at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR, due 2026-13-01."
    ).candidates[0]
    reference = _read(
        "Weld failure at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR, "
        "reference date: 16 October 2026."
    ).candidates[0]
    incidental = _read(
        "Weld failure at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR, "
        "due to ballast settlement on 4 October 2026."
    ).candidates[0]
    conflicting = _read(
        "Weld failure at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR, "
        "due 4 October 2026 and due 5 October 2026."
    ).candidates[0]
    for candidate in (ambiguous, invalid, bad_iso, reference, incidental, conflicting):
        assert candidate.due_date.value is None
        assert candidate.due_date.origin is ValueOrigin.UNKNOWN
        assert candidate.days_overdue.value == 4.0


def test_explicit_overdue_days_are_not_recalculated() -> None:
    text = (
        "Weld failure at KM 80.0 on the DN line, severity 70, 22 days overdue, "
        "due 24 Sep 2026, no TSR."
    )
    result = _read(text)
    candidate = result.candidates[0]
    assert candidate.due_date.value == "24 Sep 2026"
    assert candidate.days_overdue.value == 22.0
    record = validate_candidates(result, existing_defects=()).records[0]
    assert record.scheduling_record is not None
    assert record.scheduling_record.days_overdue == 22.0
    assert record.evidence.days_overdue_origin is ValueOrigin.REPORTED


def test_negative_weld_wording_is_ruled_out() -> None:
    for text in (
        "The weld has not failed.",
        "No weld failure was detected.",
        "The weld has never failed.",
    ):
        result = _read(text)
        preview = validate_candidates(result, existing_defects=())
        assert result.candidates == ()
        assert len(result.ruled_out) == 1
        assert result.ruled_out[0].excerpt == text
        assert result.ruled_out[0].reason == "negated_finding"
        assert preview.records[0].status is PreviewStatus.RULED_OUT
        assert preview.records[0].scheduling_record is None


def test_negative_rail_fracture_wording_is_ruled_out() -> None:
    text = "Rail fracture was not observed."
    result = _read(text)
    assert result.candidates == ()
    assert len(result.ruled_out) == 1
    assert result.ruled_out[0].excerpt == text
    assert result.ruled_out[0].reason == "negated_finding"
    record = validate_candidates(result, existing_defects=()).records[0]
    assert record.status is PreviewStatus.RULED_OUT
    assert record.scheduling_record is None
    assert record.evidence.normalized_defect_type is None


def test_positive_weld_wording_stays_a_finding() -> None:
    text = "The weld has failed at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR."
    result = _read(text)
    assert result.ruled_out == ()
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.excerpt == text
    assert candidate.normalized_defect_type.value == "WELD_FAILURE"
    record = validate_candidates(result, existing_defects=()).records[0]
    assert record.status is PreviewStatus.READY_TO_APPLY
    assert record.scheduling_record is not None
    assert record.scheduling_record.defect_type is DefectType.WELD_FAILURE


def test_uncertain_observation_stays_uncertain() -> None:
    text = "Possible weld failure at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR."
    result = _read(text)
    assert result.ruled_out == ()
    candidate = result.candidates[0]
    assert candidate.excerpt == text
    assert candidate.uncertainty is Uncertainty.HIGH
    assert candidate.review_status is ReviewStatus.NEEDS_REVIEW
    record = validate_candidates(result, existing_defects=()).records[0]
    assert record.status is PreviewStatus.NEEDS_REVIEW
    assert record.scheduling_record is None


def test_unknown_tsr_is_not_converted_to_no() -> None:
    text = "An interlocking defect was reported at KM 20.0 on the UP fast line, severity 50, 6 days overdue."
    beside = (
        f"{text} No TSR value should be inferred from this sentence."
    )
    labelled = """FINDING C-11 | SIGNAL_INTERLOCK
An interlocking defect was reported at this location.
LOCATION
 KM 20.0, UP_FAST
SEVERITY
 50
DUE DATE
 10 Oct 2026
OVERDUE
 6 days
ACTIVE TSR
 NOT STATED
"""
    for source in (text, beside, labelled):
        result = _read(source)
        signal = result.candidates[0]
        assert signal.normalized_defect_type.value == "SIGNAL_INTERLOCK"
        assert signal.has_tsr.value is None
        assert signal.has_tsr.origin is ValueOrigin.UNKNOWN
        record = validate_candidates(result, existing_defects=()).records[0]
        assert record.evidence.has_tsr is None
        assert record.status is PreviewStatus.NEEDS_REVIEW
        assert record.scheduling_record is None


def test_negative_clause_does_not_cancel_a_separate_positive_finding() -> None:
    negative = "Rail fracture was not observed"
    positive = "a defective weld was recorded at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR."
    text = f"{negative}, but {positive}"
    result = _read(text)
    assert [item.excerpt for item in result.ruled_out] == [negative]
    assert result.ruled_out[0].reason == "negated_finding"
    assert [item.excerpt for item in result.candidates] == [positive]
    assert result.candidates[0].normalized_defect_type.value == "WELD_FAILURE"
    preview = validate_candidates(result, existing_defects=())
    ruled = [item for item in preview.records if item.status is PreviewStatus.RULED_OUT]
    ready = [item for item in preview.records if item.status is PreviewStatus.READY_TO_APPLY]
    assert len(ruled) == 1 and len(ready) == 1
    assert ruled[0].scheduling_record is None
    assert ready[0].scheduling_record is not None
    assert ready[0].evidence.normalized_defect_type == "WELD_FAILURE"
    assert all(item.evidence.normalized_defect_type != "RAIL_FRACTURE" for item in preview.records)


def test_excerpts_are_exact_source_text() -> None:
    text = "The weld has failed at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR."
    result = _read(text)
    assert result.candidates[0].excerpt == text
    assert result.candidates[0].excerpt in text
    assert result.candidates[0].evidence[0].excerpt == text


def test_severity_and_planner_duration_keep_their_existing_rules() -> None:
    explicit = (
        "TMS weld failure grade 2, severity 84, at KM 80.0 on the DN line, "
        "4 days overdue, no TSR, estimated duration 15 minutes."
    )
    result = _read(explicit)
    candidate = result.candidates[0]
    assert candidate.severity.value == 84
    assert candidate.reported_duration_min.value == 15.0
    assert candidate.days_overdue.value == 4.0
    record = validate_candidates(result, existing_defects=()).records[0]
    assert record.evidence.severity == 84
    assert record.evidence.reported_duration_min == 15.0
    assert record.scheduling_record is not None
    assert record.scheduling_record.required_block_min == DEFECT_TYPE_SPEC[DefectType.WELD_FAILURE][0]
    assert record.scheduling_record.required_block_min != 15

    grade_only = "TMS weld failure grade 9 at KM 80.0 on the DN line, 4 days overdue, no TSR."
    graded = _read(grade_only)
    assert graded.candidates[0].severity.value is None
    graded_record = validate_candidates(graded, existing_defects=()).records[0]
    assert graded_record.evidence.severity == 90
    assert graded_record.evidence.severity_origin is ValueOrigin.NORMALIZED
    assert graded_record.scheduling_record is not None
    assert graded_record.scheduling_record.required_block_min == DEFECT_TYPE_SPEC[DefectType.WELD_FAILURE][0]

    minutes_only = _read("The inspection lasted 15 minutes.")
    assert minutes_only.candidates == ()
    assert minutes_only.ruled_out == ()


def test_unrelated_negation_and_hypothetical_wording_are_not_findings() -> None:
    fractured = _read("Fractured rail at KM 40.0 on the UP fast line, severity 90, 8 days overdue, no TSR.")
    assert fractured.candidates == ()
    assert fractured.ruled_out == ()
    bolt = _read("The bolt has not failed.")
    assert bolt.candidates == ()
    assert bolt.ruled_out == ()
    hypothetical = _read("If the weld has not failed, traffic may continue.")
    assert hypothetical.candidates == ()
    assert hypothetical.ruled_out == ()


def test_demo_report_keeps_stated_due_dates_overdue_days_and_unknown_tsr() -> None:
    bundle = read_report_document(_DEMO_PDF.read_bytes(), _DEMO_PDF.name)
    understanding = simplify_report(bundle, LocalTextProvider())
    preview = validate_candidates(understanding, existing_defects=())
    pages = {section.reference: section.native_text for section in bundle.sections}
    weld = next(item for item in understanding.candidates if item.reported_asset_ref == "D-01")
    signal = next(item for item in understanding.candidates if item.reported_asset_ref == "D-03")
    assert weld.due_date.value == "24 Sep 2026"
    assert weld.due_date.value in weld.excerpt
    assert weld.days_overdue.value == 22.0
    assert weld.has_tsr.value is True
    assert weld.severity.value == 84
    assert signal.has_tsr.value is None
    assert signal.due_date.value == "10 October 2026"
    assert signal.due_date.value in signal.excerpt
    assert signal.days_overdue.value == 6.0
    for item in (*understanding.candidates, *understanding.ruled_out):
        assert item.excerpt in pages[item.evidence[0].reference]
        assert item.excerpt == item.evidence[0].excerpt
    signal_record = next(item for item in preview.records if item.source_finding_id == signal.candidate_id)
    assert signal_record.evidence.has_tsr is None
    assert signal_record.scheduling_record is None
    weld_record = next(item for item in preview.records if item.source_finding_id == weld.candidate_id)
    assert weld_record.scheduling_record is not None
    assert weld_record.scheduling_record.days_overdue == 22.0
    assert weld_record.scheduling_record.required_block_min == DEFECT_TYPE_SPEC[DefectType.WELD_FAILURE][0]
