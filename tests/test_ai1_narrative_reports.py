"""Narrative wording, false-positive guards, and local summaries. No live database."""

from __future__ import annotations

import hashlib
import json

import pytest

from defects import DEFECT_TYPE_SPEC, DefectType
from report_documents import DocumentBundle, DocumentSection, ExtractionMethod, ReadStatus, TextBlock
from report_nlp import LocalTextProvider
from report_pipeline import simplify_report
from report_preview import PreviewStatus, validate_candidates
from report_provider import MockReportProvider, ProviderResponse
from report_schema import ReviewStatus, Uncertainty
from report_validate import check_findings

_PHRASES = (
    ("The rail is fractured at KM 80.0 on the DN line, severity 90, 2 days overdue, TSR imposed.", "RAIL_FRACTURE"),
    ("Rail breakage was recorded at KM 82.0 on the DN line, severity 88, 3 days overdue, no TSR.", "RAIL_FRACTURE"),
    ("The rail is broken at KM 84.0 on the DN line, severity 86, 3 days overdue, no TSR.", "RAIL_FRACTURE"),
    ("A defective weld was recorded at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR.", "WELD_FAILURE"),
    ("The weld has failed at KM 85.0 on the DN line, severity 71, 4 days overdue, no TSR.", "WELD_FAILURE"),
    ("A fractured weld was recorded at KM 86.0 on the UP line, severity 72, 4 days overdue, no TSR.", "WELD_FAILURE"),
    ("A weld fracture was recorded at KM 87.0 on the UP line, severity 73, 4 days overdue, no TSR.", "WELD_FAILURE"),
    ("Catenary sag was recorded at KM 150.0 on the UP line, severity 60, 5 days overdue, no TSR.", "OHE_SAG"),
    ("Sagging catenary was recorded at KM 151.0 on the UP line, severity 61, 5 days overdue, no TSR.", "OHE_SAG"),
    ("A sagging contact wire was recorded at KM 152.0 on the UP line, severity 62, 5 days overdue, no TSR.", "OHE_SAG"),
    ("Overhead line sag was recorded at KM 153.0 on the UP line, severity 63, 5 days overdue, no TSR.", "OHE_SAG"),
    ("Contact-wire sag was recorded at KM 154.0 on the UP line, severity 64, 5 days overdue, no TSR.", "OHE_SAG"),
    ("An interlocking defect was reported at KM 20.0 on the UP fast line, severity 50, 6 days overdue, no TSR.", "SIGNAL_INTERLOCK"),
    ("The interlocking has failed at KM 21.0 on the UP fast line, severity 51, 6 days overdue, no TSR.", "SIGNAL_INTERLOCK"),
    ("A point-machine failure was reported at KM 22.0 on the UP fast line, severity 52, 6 days overdue, no TSR.", "SIGNAL_INTERLOCK"),
    ("A track-circuit failure was reported at KM 23.0 on the UP fast line, severity 53, 6 days overdue, no TSR.", "SIGNAL_INTERLOCK"),
    ("A deep screen is required at KM 156.0 on the DN line, severity 55, 8 days overdue, no TSR.", "DEEP_SCREENING"),
    ("The track is out of tamp at KM 81.0 on the DN line, severity 30, 4 days overdue, no TSR.", "TAMPING_REQUIRED"),
    ("An out-of-tamp section was recorded at KM 89.0 on the DN line, severity 31, 4 days overdue, no TSR.", "TAMPING_REQUIRED"),
)

_NOT_CONFIRMED = (
    "No rail breakage was observed at KM 79.0 on the DN line.",
    "No, the rail is fractured at KM 79.0 on the DN line.",
    "The track is not out of tamp at KM 81.0 on the DN line.",
    "If a defective weld is found, impose a restriction.",
    "In case of catenary sag, call the controller.",
    "Check for a sagging contact wire during the patrol.",
    "The photograph shows rail breakage at KM 80.0 on the DN line, severity 90, 2 days overdue, no TSR.",
    "Tamping will be carried out at KM 81.0 on the DN line next week.",
    "The weld has not failed at KM 80.0 on the DN line.",
    "Overview: this note lists a weld failure, tamping and a signal interlock, and it assigns no chainage.",
)


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


def _bundle(sections: list[DocumentSection], filename: str = "narrative-notes.pdf") -> DocumentBundle:
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


def _read(text: str):
    return simplify_report(_bundle([_section("page:1", 1, text)]), LocalTextProvider())


def _finding(excerpt: str, reference: str, **overrides: object) -> dict[str, object]:
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
    row.update(overrides)
    return row


@pytest.mark.parametrize(("text", "defect_type"), _PHRASES)
def test_supported_wording_maps_to_one_category(text: str, defect_type: str) -> None:
    result = _read(text)
    assert result.ruled_out == ()
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.normalized_defect_type.value == defect_type
    assert candidate.excerpt == text
    assert candidate.excerpt in text


def test_two_narrative_sentences_stay_separate() -> None:
    first = "A defective weld was recorded at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR."
    second = "The rail is broken at KM 86.0 on the UP line, severity 91, 6 days overdue, TSR imposed."
    result = _read(f"{first} {second}")
    assert [item.excerpt for item in result.candidates] == [first, second]
    assert [item.normalized_defect_type.value for item in result.candidates] == ["WELD_FAILURE", "RAIL_FRACTURE"]
    assert [item.chainage_km.value for item in result.candidates] == [80.0, 86.0]


def test_repeated_finding_id_does_not_create_a_second_job() -> None:
    finding = """FINDING B-17 | WELD_FAILURE
DEFECT TYPE
 WELD_FAILURE (reported)
LOCATION
 KM 80.0, DN
SEVERITY
 70 / 100 (reported)
DUE DATE / OVERDUE
 02 Jan 2026 / 4 days
ACTIVE TSR
 Yes - 20 km/h
REPORTED DURATION
 15 minutes (estimate)
"""
    register = """SIMULATED FIELD REGISTER
B-17
WELD_FAILURE
KM 80.0 DN
70
Yes, 20 km/h
"""
    understanding = simplify_report(
        _bundle(
            [_section("page:1", 1, finding), _section("page:2", 2, register)],
            "headed-notes.pdf",
        ),
        LocalTextProvider(),
    )
    preview = validate_candidates(understanding, existing_defects=())
    welds = [item for item in understanding.candidates if item.normalized_defect_type.value == "WELD_FAILURE"]
    assert len(welds) == 1
    assert welds[0].reported_asset_ref == "B-17"
    assert "FINDING B-17" in welds[0].excerpt
    assert "WELD_FAILURE (reported)" in welds[0].excerpt
    jobs = [item for item in preview.records if item.scheduling_record is not None]
    assert len(jobs) == 1
    assert jobs[0].evidence.reported_duration_min == 15.0
    assert jobs[0].scheduling_record is not None
    assert jobs[0].scheduling_record.required_block_min == DEFECT_TYPE_SPEC[DefectType.WELD_FAILURE][0]
    assert jobs[0].scheduling_record.required_block_min != 15.0


def test_nearby_categories_are_not_merged() -> None:
    weld = "A defective weld was recorded at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR."
    tamp = "The track is out of tamp at KM 81.0 on the DN line, severity 30, 4 days overdue, no TSR."
    result = _read(f"{weld} {tamp}")
    preview = validate_candidates(result, existing_defects=())
    assert [item.normalized_defect_type.value for item in result.candidates] == ["WELD_FAILURE", "TAMPING_REQUIRED"]
    assert [item.chainage_km.value for item in result.candidates] == [80.0, 81.0]
    ready = [item for item in preview.records if item.status is PreviewStatus.READY_TO_APPLY]
    assert len(ready) == 2
    assert ready[0].duplicate_of == ()
    assert ready[1].duplicate_of == ()
    assert ready[0].scheduling_record is not None and ready[1].scheduling_record is not None
    assert ready[0].scheduling_record.defect_id != ready[1].scheduling_record.defect_id


@pytest.mark.parametrize("text", _NOT_CONFIRMED)
def test_overview_instruction_caption_and_negation_are_not_confirmed_jobs(text: str) -> None:
    result = _read(text)
    preview = validate_candidates(result, existing_defects=())
    assert all(item.normalized_defect_type.value is None or item.review_status is not ReviewStatus.READY for item in result.candidates)
    assert all(item.status is not PreviewStatus.READY_TO_APPLY for item in preview.records)
    assert all(item.scheduling_record is None for item in preview.records)
    assert not any(item.normalized_defect_type.value == "RAIL_FRACTURE" and item.review_status is ReviewStatus.READY for item in result.candidates)


def test_negative_fracture_wording_is_ruled_out() -> None:
    text = "No rail breakage was observed at KM 79.0 on the DN line."
    result = _read(text)
    assert result.candidates == ()
    assert len(result.ruled_out) == 1
    assert result.ruled_out[0].excerpt == text
    assert result.ruled_out[0].reason == "negated_finding"


def test_uncertain_vibration_does_not_gain_a_type_or_severity() -> None:
    text = (
        "Vibration was reported near KM 82.0 on the DN line, but the cause has not been determined "
        "and no confirmed defect type or severity was stated."
    )
    result = _read(text)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.excerpt == text
    assert candidate.normalized_defect_type.value is None
    assert candidate.severity.value is None
    assert candidate.chainage_km.value == 82.0
    assert candidate.uncertainty is Uncertainty.HIGH
    assert candidate.review_status is ReviewStatus.NEEDS_REVIEW
    record = validate_candidates(result, existing_defects=()).records[0]
    assert record.status is PreviewStatus.NEEDS_REVIEW
    assert record.scheduling_record is None
    assert record.evidence.severity is None
    assert record.evidence.normalized_defect_type is None


def test_unknown_tsr_is_not_changed_to_no() -> None:
    text = "An interlocking defect was reported at KM 20.0 on the UP fast line, severity 50, 6 days overdue."
    result = _read(text)
    candidate = result.candidates[0]
    assert candidate.has_tsr.value is None
    assert candidate.normalized_defect_type.value == "SIGNAL_INTERLOCK"
    assert candidate.severity.value == 50
    assert candidate.days_overdue.value == 6.0
    assert candidate.review_status is ReviewStatus.NEEDS_REVIEW
    record = validate_candidates(result, existing_defects=()).records[0]
    assert record.missing_fields == ("has_tsr",)
    assert record.evidence.has_tsr is None
    assert record.scheduling_record is None


def test_missing_severity_stays_unknown_and_unsupported_numbers_are_ignored() -> None:
    text = "Rail breakage was recorded at KM 80.0 on the DN line, 3 days overdue, no TSR."
    result = _read(text)
    assert result.candidates[0].severity.value is None
    assert "severity" in result.candidates[0].missing_fields
    assert result.candidates[0].review_status is ReviewStatus.NEEDS_REVIEW

    section = _section("page:1", 1, text)
    accepted, rejected = check_findings(
        {
            "summary": "",
            "findings": [_finding(text, "page:1", chainage_km=12.5, severity=99)],
        },
        (section,),
    )
    assert rejected == ()
    assert len(accepted) == 1
    assert accepted[0].chainage_km.value == 80.0
    assert accepted[0].severity.value is None or accepted[0].severity.value != 99
    assert any("Ignored" in warning for warning in accepted[0].validation_warnings)


def test_ambiguous_description_does_not_pick_one_category() -> None:
    text = (
        "A weld fracture or rail breakage was reported at KM 80.0 on the DN line, "
        "severity 70, 4 days overdue, no TSR."
    )
    result = _read(text)
    assert len(result.candidates) == 1
    assert result.candidates[0].normalized_defect_type.value is None
    assert result.candidates[0].excerpt == text
    assert result.candidates[0].review_status is ReviewStatus.NEEDS_REVIEW
    record = validate_candidates(result, existing_defects=()).records[0]
    assert record.scheduling_record is None


def test_every_excerpt_is_an_exact_source_slice() -> None:
    first = "A defective weld was recorded at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR."
    second = "No rail breakage was observed at KM 79.0 on the DN line."
    text = f"{first} {second} The patrol then returned."
    bundle = _bundle([_section("page:1", 1, text)])
    result = simplify_report(bundle, LocalTextProvider())
    findings = (*result.candidates, *result.ruled_out)
    assert findings
    for item in findings:
        assert item.excerpt == item.evidence[0].excerpt
        assert item.excerpt in bundle.sections[0].native_text
    assert "returned" not in result.summary


def test_headingless_and_headed_findings_get_the_same_field_based_summary() -> None:
    sentence = "A defective weld was recorded at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR."
    narrative = f"{sentence} The patrol finished at dusk."
    prose = _read(narrative)
    assert prose.summary == "Weld failure · KM 80 DN · Severity 70 · 4 days overdue · No TSR."
    assert sentence not in prose.summary
    assert "dusk" not in prose.summary
    assert prose.candidates[0].excerpt == sentence
    assert prose.candidates[0].evidence[0].excerpt == sentence
    assert prose.candidates[0].evidence[0].reference == "page:1"
    assert prose.candidates[0].evidence[0].page_number == 1

    block = "A defective weld was recorded at KM 80.0 on the DN line\nseverity 70, 4 days overdue, no TSR"
    wrapped = _read(block)
    assert wrapped.candidates[0].excerpt == block
    assert "\n" in wrapped.candidates[0].statement
    assert wrapped.summary == prose.summary
    assert "\n" not in wrapped.summary
    assert "language model" not in wrapped.summary.casefold()

    headed = """FINDING C-04 | SIGNAL_INTERLOCK
LOCATION
 KM 20.0, UP_FAST
SEVERITY
 50 / 100 (reported)
DUE DATE / OVERDUE
 03 Jan 2026 / 5 days
ACTIVE TSR
 NOT STATED - UNKNOWN
"""
    grouped = simplify_report(_bundle([_section("page:1", 1, headed)]), LocalTextProvider())
    assert grouped.provider_name == "local-text"
    assert grouped.summary == (
        "Signal interlock · KM 20 UP_FAST · Severity 50 · 5 days overdue · TSR unknown · Needs review."
    )
    assert grouped.candidates[0].has_tsr.value is None
    assert grouped.candidates[0].review_status is ReviewStatus.NEEDS_REVIEW
    assert "No TSR" not in grouped.summary
    assert "FINDING C-04" not in grouped.summary
    assert grouped.candidates[0].excerpt.startswith("FINDING C-04 | SIGNAL_INTERLOCK")


def test_summary_marks_unknown_fields_instead_of_filling_them() -> None:
    no_severity = _read("OHE sag at KM 150.0 on the UP line, 9 days overdue, no TSR.")
    assert no_severity.summary == "OHE sag · KM 150 UP · 9 days overdue · No TSR · Severity unknown · Needs review."
    assert no_severity.candidates[0].severity.value is None

    no_line = _read("Weld failure at KM 80.0, severity 70, 4 days overdue, no TSR.")
    assert no_line.summary == "Weld failure · KM 80 · Severity 70 · 4 days overdue · No TSR · Line unknown · Needs review."
    assert no_line.candidates[0].track_line.value is None

    no_location = _read("Weld failure on the section, severity 70, 4 days overdue, no TSR.")
    assert no_location.candidates[0].chainage_km.value is None
    assert "Chainage and line unknown" in no_location.summary
    assert "KM" not in no_location.summary
    assert "Needs review" in no_location.summary

    unknown_tsr = _read("Signal interlock at KM 9.4 on the UP fast line, severity 63, 6 days overdue.")
    assert unknown_tsr.candidates[0].has_tsr.value is None
    assert "TSR unknown" in unknown_tsr.summary
    assert "No TSR" not in unknown_tsr.summary
    assert "Active TSR" not in unknown_tsr.summary
    assert unknown_tsr.summary.endswith("Needs review.")

    due_only = _read("Deep screening at KM 120.0 on the DN line, severity 44, due 24 Sep 2026, no TSR.")
    assert due_only.candidates[0].days_overdue.value is None
    assert due_only.candidates[0].due_date.value == "24 Sep 2026"
    assert due_only.summary == (
        "Deep screening · KM 120 DN · Severity 44 · Due 24 Sep 2026 · No TSR · Days overdue unknown · Needs review."
    )

    active = _read("Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, TSR imposed at 30 km/h.")
    assert active.summary == "Weld failure · KM 80 DN · Severity 84 · 22 days overdue · Active TSR 30 km/h."
    assert active.candidates[0].review_status is ReviewStatus.READY
    assert "Needs review" not in active.summary
    assert "urgency" not in active.summary.casefold()


def test_summary_keeps_uncertain_and_negative_findings_out_of_the_job_list() -> None:
    text = (
        "Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR. "
        "Possible weld failure at KM 67.5 on the DN line, severity 84, 22 days overdue, no TSR. "
        "A defect near KM 69.0 on the DN line cannot be ruled out. "
        "No rail fracture was observed at KM 12.0 on the UP fast line. "
        "Nil defect was recorded on the UP line."
    )
    result = _read(text)
    lines = [line + "." for line in result.summary.rstrip(".").split(". ")]
    assert len(lines) == len(result.candidates) + len(result.ruled_out) == 5
    assert lines[0] == "Weld failure · KM 80 DN · Severity 84 · 22 days overdue · No TSR."
    assert lines[1] == (
        "Possible weld failure · KM 67.5 DN · Severity 84 · 22 days overdue · No TSR"
        " · Not a confirmed defect · Needs review."
    )
    assert lines[2] == (
        "Uncertain observation · Near KM 69 DN · Defect type, severity, days overdue and TSR unknown"
        " · Not a confirmed defect · Needs review."
    )
    assert lines[3] == "Negative rail fracture observation · Ruled out; not a maintenance job."
    assert lines[4] == "Nil defect report · Ruled out; not a maintenance job."
    assert [item.uncertainty for item in result.candidates] == [Uncertainty.LOW, Uncertainty.HIGH, Uncertainty.HIGH]
    assert [item.reason for item in result.ruled_out] == ["negated_finding", "nil_report"]
    for item in (*result.candidates, *result.ruled_out):
        assert item.excerpt in text
        assert item.evidence[0].excerpt == item.excerpt
        assert item.evidence[0].reference == "page:1"
    preview = validate_candidates(result, existing_defects=())
    scheduled = [item for item in preview.records if item.scheduling_record is not None]
    assert len(scheduled) == 1
    assert scheduled[0].evidence.chainage_km == 80.0
    assert scheduled[0].evidence.severity == 84
    assert scheduled[0].scheduling_record.required_block_min == DEFECT_TYPE_SPEC[DefectType.WELD_FAILURE][0]
    assert sum(1 for item in preview.records if item.status is PreviewStatus.RULED_OUT) == 2
    assert sum(1 for item in preview.records if item.status is PreviewStatus.NEEDS_REVIEW) == 2


def test_long_local_summary_stops_on_a_sentence_boundary() -> None:
    sentences = [
        f"Weld failure at KM {80 + index}.0 on the DN line, severity 40, 3 days overdue, no TSR."
        for index in range(40)
    ]
    result = _read(" ".join(sentences))
    assert len(result.candidates) == 40
    assert 0 < len(result.summary) <= 1200
    assert result.summary.endswith("No TSR.")
    assert "..." not in result.summary
    lines = result.summary.rstrip(".").split(". ")
    assert 1 < len(lines) < 40
    for index, line in enumerate(lines):
        assert line == f"Weld failure · KM {80 + index} DN · Severity 40 · 3 days overdue · No TSR"


def test_mock_provider_and_scripted_summary_stay_unchanged() -> None:
    text = "A defective weld was recorded at KM 80.0 on the DN line, severity 70, 4 days overdue, no TSR."
    empty = simplify_report(_bundle([_section("page:1", 1, text)]), MockReportProvider())
    assert empty.provider_name == "mock"
    assert empty.candidates == ()
    assert empty.summary == ""

    class _Scripted:
        name = "mock"
        supports_images = False
        fixture_matched = True
        scripted_fixture = "sample_script"
        scripted_summary = "Scripted interpretation for the test provider."

        def complete(self, request: object) -> ProviderResponse:
            del request
            return ProviderResponse(
                raw_text=json.dumps({"summary": "provider wording", "findings": [_finding(text, "page:1")]})
            )

    scripted = simplify_report(_bundle([_section("page:1", 1, text)]), _Scripted())
    assert scripted.summary == "Scripted interpretation for the test provider."
    assert scripted.provider_name == "mock:sample_script"
    assert scripted.candidates[0].excerpt == text
    assert "Reported weld failure" not in scripted.summary
