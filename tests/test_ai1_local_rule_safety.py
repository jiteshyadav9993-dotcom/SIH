"""Local phrase, polarity, and track-line guards. No database and no answer key."""

from __future__ import annotations

import hashlib

from report_documents import DocumentBundle, DocumentSection, ExtractionMethod, ReadStatus, TextBlock
from report_nlp import LocalTextProvider
from report_pipeline import simplify_report
from report_preview import PreviewStatus, validate_candidates
from report_schema import ReviewStatus


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
        filename="local-rule-safety.pdf",
        media_kind="text",
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        byte_size=len(text.encode("utf-8")),
        status=ReadStatus.COMPLETE,
        sections=(_section("page:1", 1, text),),
        warnings=(),
        methods_used=(ExtractionMethod.NATIVE_TEXT,),
    )


def _read(text: str):
    understanding = simplify_report(_bundle(text), LocalTextProvider())
    preview = validate_candidates(understanding, existing_defects=())
    return understanding, preview


def _assert_no_active_job(text: str) -> None:
    understanding, preview = _read(text)
    assert understanding.candidates == (), text
    assert all(item.status is not PreviewStatus.READY_TO_APPLY for item in preview.records)
    assert all(item.scheduling_record is None for item in preview.records)


def test_supported_wording_variations_map_to_existing_categories() -> None:
    weld = "AT weld failed at joint, gap visible, SSE told"
    understanding, preview = _read(weld)
    assert understanding.ruled_out == ()
    assert len(understanding.candidates) == 1
    candidate = understanding.candidates[0]
    assert candidate.normalized_defect_type.value == "WELD_FAILURE"
    assert candidate.excerpt == weld
    assert candidate.excerpt in weld
    assert candidate.evidence[0].reference == "page:1"
    assert candidate.evidence[0].excerpt == weld
    assert candidate.track_line.value is None
    assert candidate.chainage_km.value is None
    assert candidate.severity.value is None
    assert candidate.days_overdue.value is None
    assert candidate.due_date.value is None
    assert candidate.has_tsr.value is None
    assert candidate.review_status is ReviewStatus.NEEDS_REVIEW
    assert preview.records[0].status is PreviewStatus.NEEDS_REVIEW
    assert preview.records[0].scheduling_record is None

    signal = (
        "At the cabin, interlocking failed to prove point detection with the route set, "
        "so the signal could not clear."
    )
    signal_result, _signal_preview = _read(signal)
    assert len(signal_result.candidates) == 1
    assert signal_result.candidates[0].normalized_defect_type.value == "SIGNAL_INTERLOCK"
    assert signal_result.candidates[0].excerpt == signal
    assert signal_result.candidates[0].track_line.value is None
    assert signal_result.candidates[0].chainage_km.value is None

    sag = "ohe sagging badly mid span, slip raised"
    sag_result, _sag_preview = _read(sag)
    assert len(sag_result.candidates) == 1
    assert sag_result.candidates[0].normalized_defect_type.value == "OHE_SAG"
    assert sag_result.candidates[0].excerpt == sag
    assert sag_result.candidates[0].track_line.value is None

    stated = "The AT weld failed at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR."
    stated_result, stated_preview = _read(stated)
    assert stated_result.candidates[0].normalized_defect_type.value == "WELD_FAILURE"
    assert stated_result.candidates[0].excerpt == stated
    assert stated_result.candidates[0].chainage_km.value == 80.0
    assert stated_result.candidates[0].track_line.value == "DN"
    assert stated_result.candidates[0].severity.value == 70
    assert stated_result.candidates[0].days_overdue.value == 4.0
    assert stated_result.candidates[0].has_tsr.value is False
    assert stated_preview.records[0].status is PreviewStatus.READY_TO_APPLY


def test_fractured_rail_is_not_a_positive_finding() -> None:
    text = "Fractured rail at KM 40.0 on the UP fast line, severity 90, 8 days overdue, no TSR."
    understanding, preview = _read(text)
    assert understanding.candidates == ()
    assert understanding.ruled_out == ()
    assert preview.records == ()

    supported = "The rail is fractured at KM 80.0 on the DN line, severity 90, 2 days overdue, no TSR."
    kept, _preview = _read(supported)
    assert kept.candidates[0].normalized_defect_type.value == "RAIL_FRACTURE"
    assert kept.candidates[0].excerpt == supported


def test_instructions_are_not_active_defects() -> None:
    samples = (
        "Patrol staff to watch for rail fracture signs during the cold spell.",
        "Gang to report any OHE sag noticed near the work site.",
        "Supervisor to check whether tamping is needed once the monsoon ends.",
        "S&T staff must test the interlocking whenever a point machine is replaced.",
        "Check for a sagging contact wire during the patrol.",
        "If weld failure is seen, stop work and inform the SSE at once.",
    )
    for text in samples:
        understanding, preview = _read(text)
        assert understanding.candidates == (), text
        assert understanding.ruled_out == (), text
        assert preview.records == (), text


def test_planned_maintenance_is_not_an_active_defect() -> None:
    samples = (
        "Tamping will be carried out at KM 81.0 on the DN line next week.",
        "Planned tamping at KM 81.0 on the DN line.",
        "Tamping and deep screening both appear in the yearly programme, "
        "but no field observation is recorded for this block.",
        "Tamping cycle for this section is normally set at eighteen months.",
        "Tamping machine reached the depot this evening and was refuelled.",
    )
    for text in samples:
        _assert_no_active_job(text)
        understanding, _preview = _read(text)
        assert understanding.ruled_out == (), text


def test_completed_work_does_not_become_a_new_defect() -> None:
    _assert_no_active_job("Tamping was carried out in this block last week.")
    _assert_no_active_job(
        "The earlier weld failure has been rectified and rechecked, nothing outstanding at the joint."
    )
    _assert_no_active_job("Deep screening finished in the block last month, nothing pending.")
    _assert_no_active_job("Interlocking fault was rectified and retested, no defect outstanding.")

    done = "Tamping done on the 2nd and the section tested fine by trolley; nothing outstanding for tamping."
    understanding, preview = _read(done)
    assert understanding.candidates == ()
    assert understanding.ruled_out
    assert all(item.reason == "negated_finding" for item in understanding.ruled_out)
    assert all("nothing outstanding for tamping" in item.excerpt for item in understanding.ruled_out)
    assert all(item.scheduling_record is None for item in preview.records)

    unwarranted = (
        "Packing found firm and level throughout the stretch, so tamping is not warranted this cycle."
    )
    negative, _preview = _read(unwarranted)
    assert negative.candidates == ()
    assert negative.ruled_out[0].reason == "negated_finding"
    assert negative.ruled_out[0].excerpt == unwarranted


def test_not_yet_checked_statements_are_not_confirmed_jobs() -> None:
    fracture = "Rail fracture check on the siding has not been carried out yet, condition unknown."
    understanding, preview = _read(fracture)
    assert understanding.ruled_out == ()
    assert len(understanding.candidates) == 1
    candidate = understanding.candidates[0]
    assert candidate.normalized_defect_type.value == "RAIL_FRACTURE"
    assert candidate.excerpt == fracture
    assert candidate.uncertainty.value == "high"
    assert candidate.review_status is ReviewStatus.NEEDS_REVIEW
    assert candidate.severity.value is None
    assert candidate.chainage_km.value is None
    assert candidate.track_line.value is None
    assert preview.records[0].status is PreviewStatus.NEEDS_REVIEW
    assert preview.records[0].scheduling_record is None

    tamping = "Need for tamping on the loop has not been assessed, recording car data awaited."
    tamping_result, tamping_preview = _read(tamping)
    assert tamping_result.candidates[0].uncertainty.value == "high"
    assert tamping_result.candidates[0].normalized_defect_type.value == "TAMPING_REQUIRED"
    assert tamping_result.candidates[0].track_line.value is None
    assert tamping_result.candidates[0].review_status is ReviewStatus.NEEDS_REVIEW
    assert tamping_preview.records[0].scheduling_record is None


def test_explicit_negation_and_nil_do_not_use_a_broad_no() -> None:
    absent = "No rail fracture was observed at KM 80.0 on the DN line."
    ruled, _preview = _read(absent)
    assert ruled.candidates == ()
    assert ruled.ruled_out[0].reason == "negated_finding"
    assert ruled.ruled_out[0].excerpt == absent

    nil_defect = "Nil defect was recorded on the UP line."
    nil_result, _nil_preview = _read(nil_defect)
    assert nil_result.candidates == ()
    assert nil_result.ruled_out[0].reason == "nil_report"
    assert nil_result.ruled_out[0].excerpt == nil_defect

    ohe_nil = "OHE sag nil, contact wire height within limits per tower wagon check."
    ohe, _ohe_preview = _read(ohe_nil)
    assert ohe.candidates == ()
    assert ohe.ruled_out[0].reason == "nil_report"
    assert ohe.ruled_out[0].excerpt == ohe_nil

    neither = "Neither rail fracture nor weld failure noticed during the night patrol."
    neither_result, neither_preview = _read(neither)
    assert neither_result.candidates == ()
    assert len(neither_result.ruled_out) == 1
    assert neither_result.ruled_out[0].reason == "negated_finding"
    assert neither_result.ruled_out[0].excerpt == neither
    assert neither_preview.records[0].status is PreviewStatus.RULED_OUT
    assert neither_preview.records[0].scheduling_record is None

    bare = "Nil."
    bare_result, bare_preview = _read(bare)
    assert bare_result.candidates == ()
    assert bare_result.ruled_out == ()
    assert bare_preview.records == ()

    comma_no = "No, the rail is fractured at KM 80.0 on the DN line."
    comma_result, _comma_preview = _read(comma_no)
    assert comma_result.candidates == ()
    assert comma_result.ruled_out[0].reason == "negated_finding"


def test_a_negative_clause_does_not_hide_a_separate_positive_finding() -> None:
    text = (
        "No weld failure was observed on the UP line, but a rail fracture remains "
        "at KM 80.0 on the DN line, severity 90, 8 days overdue, no TSR."
    )
    understanding, preview = _read(text)
    assert len(understanding.ruled_out) == 1
    assert understanding.ruled_out[0].excerpt == "No weld failure was observed on the UP line"
    assert understanding.ruled_out[0].reason == "negated_finding"
    assert len(understanding.candidates) == 1
    candidate = understanding.candidates[0]
    assert candidate.normalized_defect_type.value == "RAIL_FRACTURE"
    assert candidate.excerpt in text
    assert "weld failure" not in candidate.excerpt.casefold()
    assert candidate.chainage_km.value == 80.0
    assert candidate.track_line.value == "DN"
    assert candidate.severity.value == 90
    assert candidate.days_overdue.value == 8.0
    assert candidate.has_tsr.value is False
    assert candidate.evidence[0].reference == "page:1"
    ready = [item for item in preview.records if item.status is PreviewStatus.READY_TO_APPLY]
    assert len(ready) == 1
    assert ready[0].scheduling_record is not None

    repaired = "Earlier weld failure was rewelded; today a separate rail fracture is noted 15 m away."
    repaired_result, repaired_preview = _read(repaired)
    assert repaired_result.ruled_out == ()
    assert len(repaired_result.candidates) == 1
    assert repaired_result.candidates[0].normalized_defect_type.value == "RAIL_FRACTURE"
    assert repaired_result.candidates[0].excerpt == "today a separate rail fracture is noted 15 m away."
    assert repaired_result.candidates[0].chainage_km.value is None
    assert repaired_result.candidates[0].severity.value is None
    assert repaired_preview.records[0].scheduling_record is None

    finished = (
        "Tamping was finished in the up loop on Tuesday, "
        "but the main line beside it still requires tamping."
    )
    finished_result, _finished_preview = _read(finished)
    assert finished_result.ruled_out == ()
    assert len(finished_result.candidates) == 1
    assert finished_result.candidates[0].normalized_defect_type.value == "TAMPING_REQUIRED"
    assert finished_result.candidates[0].excerpt == "the main line beside it still requires tamping."
    assert finished_result.candidates[0].track_line.value is None
    assert "up loop" not in finished_result.candidates[0].excerpt


def test_no_decision_yet_does_not_rule_out_the_defect() -> None:
    text = "No decision yet on whether deep screening is needed, as ballast sampling has not been done."
    understanding, preview = _read(text)
    assert understanding.ruled_out == ()
    assert len(understanding.candidates) == 1
    candidate = understanding.candidates[0]
    assert candidate.normalized_defect_type.value == "DEEP_SCREENING"
    assert candidate.excerpt == text
    assert candidate.uncertainty.value == "high"
    assert candidate.review_status is ReviewStatus.NEEDS_REVIEW
    assert candidate.track_line.value is None
    assert candidate.chainage_km.value is None
    assert candidate.severity.value is None
    assert preview.records[0].status is PreviewStatus.NEEDS_REVIEW
    assert preview.records[0].scheduling_record is None


def test_ordinary_words_are_not_track_lines() -> None:
    mid = "ohe sag at mid span, no date on slip"
    mid_result, _mid_preview = _read(mid)
    assert mid_result.candidates[0].normalized_defect_type.value == "OHE_SAG"
    assert mid_result.candidates[0].track_line.value is None
    assert mid_result.candidates[0].excerpt == mid

    up_to = (
        "Ballast is fouled right up to the sleeper bottoms with mud pumping at the rail seats, "
        "and deep screening is required."
    )
    up_to_result, _up_to_preview = _read(up_to)
    assert up_to_result.candidates[0].normalized_defect_type.value == "DEEP_SCREENING"
    assert up_to_result.candidates[0].track_line.value is None
    assert up_to_result.candidates[0].excerpt == up_to

    loop = "Tamping is required in the up loop."
    loop_result, _loop_preview = _read(loop)
    assert loop_result.candidates[0].normalized_defect_type.value == "TAMPING_REQUIRED"
    assert loop_result.candidates[0].track_line.value is None
    assert loop_result.candidates[0].excerpt == loop


def test_explicit_running_lines_are_still_recognized() -> None:
    dn = "Weld failure on the DN line."
    dn_result, _dn_preview = _read(dn)
    assert dn_result.candidates[0].track_line.value == "DN"
    assert dn_result.candidates[0].excerpt == dn

    shorthand = "Weld failure identified at KM 80.0 DN, severity 70, 4 days overdue, no TSR."
    shorthand_result, _shorthand_preview = _read(shorthand)
    assert shorthand_result.candidates[0].track_line.value == "DN"
    assert shorthand_result.candidates[0].chainage_km.value == 80.0

    up_fast = "Signal interlock at KM 9.4 on the UP fast line, severity 63, 6 days overdue, no TSR."
    up_fast_result, _up_fast_preview = _read(up_fast)
    assert up_fast_result.candidates[0].track_line.value == "UP_FAST"
    assert up_fast_result.candidates[0].chainage_km.value == 9.4

    down_fast = "Signal interlock at KM 9.4 on the down fast line."
    down_fast_result, _down_fast_preview = _read(down_fast)
    assert down_fast_result.candidates[0].track_line.value == "DN_FAST"

    mid = "OHE sag on the MID line at KM 110.0, severity 40, 2 days overdue, no TSR."
    mid_result, _mid_preview = _read(mid)
    assert mid_result.candidates[0].track_line.value == "MID"
    assert mid_result.candidates[0].chainage_km.value == 110.0


def test_unknown_line_stays_unknown_and_unstated_fields_stay_empty() -> None:
    text = "Weld failure beside the depot, severity 40, 2 days overdue, no TSR."
    understanding, preview = _read(text)
    candidate = understanding.candidates[0]
    assert candidate.excerpt == text
    assert candidate.excerpt in text
    assert candidate.evidence[0].reference == "page:1"
    assert candidate.evidence[0].excerpt == text
    assert candidate.normalized_defect_type.value == "WELD_FAILURE"
    assert candidate.track_line.value is None
    assert candidate.chainage_km.value is None
    assert candidate.due_date.value is None
    assert candidate.severity.value == 40
    assert candidate.days_overdue.value == 2.0
    assert candidate.has_tsr.value is False
    assert candidate.review_status is ReviewStatus.NEEDS_REVIEW
    assert preview.records[0].scheduling_record is None
    assert "track_line" in preview.records[0].missing_fields
    assert "chainage_km" in preview.records[0].missing_fields
