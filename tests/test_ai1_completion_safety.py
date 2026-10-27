"""Completed work and not-yet-assessed wording. No database and no answer key."""

from __future__ import annotations

import hashlib

from report_documents import DocumentBundle, DocumentSection, ExtractionMethod, ReadStatus, TextBlock
from report_nlp import LocalTextProvider
from report_pipeline import simplify_report
from report_preview import PreviewStatus, validate_candidates
from report_schema import ReviewStatus


def _section(text: str) -> DocumentSection:
    return DocumentSection(
        reference="page:1",
        page_number=1,
        native_text=text,
        ocr_text="",
        blocks=(
            TextBlock(
                block_id="page:1/text:1",
                text=text,
                method=ExtractionMethod.NATIVE_TEXT,
                reference="page:1",
                page_number=1,
            ),
        ),
        images=(),
        methods=(ExtractionMethod.NATIVE_TEXT,),
    )


def _read(text: str):
    encoded = text.encode("utf-8")
    bundle = DocumentBundle(
        filename="completion-safety.pdf",
        media_kind="text",
        sha256=hashlib.sha256(encoded).hexdigest(),
        byte_size=len(encoded),
        status=ReadStatus.COMPLETE,
        sections=(_section(text),),
        warnings=(),
        methods_used=(ExtractionMethod.NATIVE_TEXT,),
    )
    understanding = simplify_report(bundle, LocalTextProvider())
    preview = validate_candidates(understanding, existing_defects=())
    return understanding, preview


def _assert_no_active_job(text: str) -> None:
    understanding, preview = _read(text)
    assert understanding.candidates == (), text
    assert all(item.excerpt in text for item in understanding.ruled_out)
    assert all(item.status is not PreviewStatus.READY_TO_APPLY for item in preview.records)
    assert all(item.scheduling_record is None for item in preview.records)


def test_not_assessed_yet_stays_uncertain_and_unschedulable() -> None:
    text = "Need for tamping not assessed yet."
    understanding, preview = _read(text)
    assert understanding.ruled_out == ()
    assert len(understanding.candidates) == 1
    candidate = understanding.candidates[0]
    assert candidate.excerpt == text
    assert candidate.excerpt in text
    assert candidate.evidence[0].excerpt == text
    assert candidate.evidence[0].reference == "page:1"
    assert candidate.normalized_defect_type.value == "TAMPING_REQUIRED"
    assert candidate.uncertainty.value == "high"
    assert candidate.review_status is ReviewStatus.NEEDS_REVIEW
    assert candidate.severity.value is None
    assert candidate.track_line.value is None
    assert candidate.chainage_km.value is None
    assert preview.records[0].status is PreviewStatus.NEEDS_REVIEW
    assert preview.records[0].scheduling_record is None

    equivalent = "Need for deep screening not assessed yet."
    equivalent_result, equivalent_preview = _read(equivalent)
    assert equivalent_result.ruled_out == ()
    assert equivalent_result.candidates[0].excerpt == equivalent
    assert equivalent_result.candidates[0].normalized_defect_type.value == "DEEP_SCREENING"
    assert equivalent_result.candidates[0].uncertainty.value == "high"
    assert equivalent_result.candidates[0].review_status is ReviewStatus.NEEDS_REVIEW
    assert equivalent_preview.records[0].scheduling_record is None


def test_resolved_work_does_not_become_an_active_job() -> None:
    samples = (
        "OHE sag regulated earlier and rechecked within limits, none outstanding.",
        "Defective weld was cut out, a new thermit weld made and tested satisfactory.",
        "Weld failure of last month stands rectified; no fresh failure noted.",
        "The interlocking fault of last Thursday was traced to a faulty relay, "
        "which has been changed and the route retested; nothing outstanding.",
        "The weld failure stands rectified.",
        "OHE sag regulated earlier and rechecked, none outstanding.",
    )
    for text in samples:
        _assert_no_active_job(text)


def test_a_single_resolution_word_does_not_close_a_defect() -> None:
    changed = "The interlocking fault has been changed."
    changed_result, _changed_preview = _read(changed)
    assert len(changed_result.candidates) == 1
    assert changed_result.candidates[0].excerpt == changed
    assert changed_result.candidates[0].normalized_defect_type.value == "SIGNAL_INTERLOCK"
    assert changed_result.ruled_out == ()

    tested = "The defective weld was tested satisfactory."
    tested_result, _tested_preview = _read(tested)
    assert len(tested_result.candidates) == 1
    assert tested_result.candidates[0].excerpt == tested
    assert tested_result.candidates[0].normalized_defect_type.value == "WELD_FAILURE"

    outstanding = "Tamping is still outstanding for this block."
    outstanding_result, _outstanding_preview = _read(outstanding)
    assert len(outstanding_result.candidates) == 1
    assert outstanding_result.candidates[0].excerpt == outstanding
    assert outstanding_result.candidates[0].normalized_defect_type.value == "TAMPING_REQUIRED"
    assert outstanding_result.candidates[0].uncertainty.value == "low"


def test_a_current_defect_is_still_recognized() -> None:
    text = "Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR."
    understanding, preview = _read(text)
    assert understanding.ruled_out == ()
    candidate = understanding.candidates[0]
    assert candidate.excerpt == text
    assert candidate.normalized_defect_type.value == "WELD_FAILURE"
    assert candidate.track_line.value == "DN"
    assert candidate.chainage_km.value == 80.0
    assert candidate.severity.value == 84
    assert candidate.days_overdue.value == 22.0
    assert candidate.has_tsr.value is False
    assert candidate.review_status is ReviewStatus.READY
    assert preview.records[0].status is PreviewStatus.READY_TO_APPLY
    assert preview.records[0].scheduling_record is not None


def test_resolved_work_does_not_hide_a_separate_current_defect() -> None:
    repaired = "Earlier weld failure was rewelded; today a separate rail fracture is noted 15 m away."
    repaired_result, repaired_preview = _read(repaired)
    assert repaired_result.ruled_out == ()
    assert len(repaired_result.candidates) == 1
    assert repaired_result.candidates[0].normalized_defect_type.value == "RAIL_FRACTURE"
    assert repaired_result.candidates[0].excerpt == "today a separate rail fracture is noted 15 m away."
    assert repaired_result.candidates[0].excerpt in repaired
    assert "rewelded" not in repaired_result.candidates[0].excerpt
    assert repaired_preview.records[0].scheduling_record is None

    text = (
        "Weld failure of last month stands rectified; "
        "a rail fracture is noted at KM 80.0 on the DN line, severity 90, 8 days overdue, no TSR."
    )
    understanding, preview = _read(text)
    assert len(understanding.candidates) == 1
    candidate = understanding.candidates[0]
    assert candidate.normalized_defect_type.value == "RAIL_FRACTURE"
    assert candidate.excerpt == "a rail fracture is noted at KM 80.0 on the DN line, severity 90, 8 days overdue, no TSR."
    assert candidate.excerpt in text
    assert "stands rectified" not in candidate.excerpt
    assert candidate.track_line.value == "DN"
    assert candidate.chainage_km.value == 80.0
    assert candidate.severity.value == 90
    assert preview.records[0].status is PreviewStatus.READY_TO_APPLY

    uncertain = (
        "Need for tamping not assessed yet; "
        "rail fracture at KM 80.0 on the DN line, severity 90, 8 days overdue, no TSR."
    )
    mixed, mixed_preview = _read(uncertain)
    assert [item.excerpt for item in mixed.candidates] == [
        "Need for tamping not assessed yet",
        "rail fracture at KM 80.0 on the DN line, severity 90, 8 days overdue, no TSR.",
    ]
    assert mixed.candidates[0].uncertainty.value == "high"
    assert mixed.candidates[0].review_status is ReviewStatus.NEEDS_REVIEW
    assert mixed.candidates[1].normalized_defect_type.value == "RAIL_FRACTURE"
    assert mixed.candidates[1].review_status is ReviewStatus.READY
    ready = [item for item in mixed_preview.records if item.status is PreviewStatus.READY_TO_APPLY]
    assert len(ready) == 1
    assert ready[0].evidence.normalized_defect_type == "RAIL_FRACTURE"


def test_explicit_negation_does_not_become_a_positive_job() -> None:
    text = "No rail fracture was observed at KM 80.0 on the DN line."
    understanding, preview = _read(text)
    assert understanding.candidates == ()
    assert len(understanding.ruled_out) == 1
    assert understanding.ruled_out[0].excerpt == text
    assert understanding.ruled_out[0].reason == "negated_finding"
    assert preview.records[0].status is PreviewStatus.RULED_OUT
    assert preview.records[0].scheduling_record is None

    failed = "The weld has not failed at KM 80.0 on the DN line."
    failed_result, failed_preview = _read(failed)
    assert failed_result.candidates == ()
    assert failed_result.ruled_out[0].excerpt == failed
    assert failed_result.ruled_out[0].reason == "negated_finding"
    assert failed_preview.records[0].scheduling_record is None


def test_uncertain_observation_is_not_a_scheduling_record() -> None:
    text = "Possible weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR."
    understanding, preview = _read(text)
    candidate = understanding.candidates[0]
    assert candidate.excerpt == text
    assert candidate.normalized_defect_type.value == "WELD_FAILURE"
    assert candidate.uncertainty.value == "high"
    assert candidate.review_status is ReviewStatus.NEEDS_REVIEW
    assert preview.records[0].status is PreviewStatus.NEEDS_REVIEW
    assert preview.records[0].scheduling_record is None


def test_explicit_running_lines_remain_recognized() -> None:
    dn, _dn_preview = _read("Weld failure on the DN line.")
    assert dn.candidates[0].track_line.value == "DN"
    assert dn.candidates[0].excerpt == "Weld failure on the DN line."

    up_fast, _up_preview = _read("Signal interlock at KM 9.4 on the UP fast line.")
    assert up_fast.candidates[0].track_line.value == "UP_FAST"
    assert up_fast.candidates[0].chainage_km.value == 9.4
    assert up_fast.candidates[0].excerpt == "Signal interlock at KM 9.4 on the UP fast line."

    mid, _mid_preview = _read("OHE sag on the MID line at KM 110.0.")
    assert mid.candidates[0].track_line.value == "MID"
    assert mid.candidates[0].chainage_km.value == 110.0
    assert mid.candidates[0].excerpt == "OHE sag on the MID line at KM 110.0."
