"""Preview validation for report candidates. The backlog is not changed."""

from __future__ import annotations

from datetime import date

import pytest

import backlog_state
import data_ingestion
import defects
import topology
from data_ingestion import (
    SMMS_FAULT_TYPES,
    SMMS_PRIORITY_SEVERITY,
    TDMS_CATEGORY_MAP,
    TDMS_SAG_TO_SEVERITY_DIVISOR,
    TMS_DEFECT_CODES,
    TMS_LINE_CODES,
)
from defects import DEFECT_TYPE_SPEC, DefectType, _make_defect
from report_preview import PreviewStatus, same_problem_fields, validate_candidates
from report_schema import (
    DefectCandidate,
    Evidence,
    FindingMethod,
    ReportUnderstanding,
    ReviewStatus,
    RuledOutFinding,
    Uncertainty,
    UnderstandingStatus,
    ValueOrigin,
    normalized_value,
    reported_value,
    unknown_value,
)

_REPORT_ID = "report-abc123def456"
_FILENAME = "inspection.pdf"


def _evidence(excerpt: str, reference: str = "page:1", page: int | None = 1) -> Evidence:
    return Evidence(
        evidence_id="E1",
        filename=_FILENAME,
        reference=reference,
        page_number=page,
        excerpt=excerpt,
        extraction_method=FindingMethod.NATIVE_TEXT,
    )


def _sourced(value: object, origin: ValueOrigin = ValueOrigin.REPORTED):
    if value is None:
        return unknown_value()
    return reported_value(value) if origin is ValueOrigin.REPORTED else normalized_value(value)


def _candidate(
    excerpt: str,
    *,
    candidate_id: str = "F1",
    department: str | None = None,
    reported_type: str | None = None,
    defect_type: str | None = "WELD_FAILURE",
    type_origin: ValueOrigin = ValueOrigin.NORMALIZED,
    severity_text: str | None = "severity 84",
    severity: object | None = 84,
    reported_line: str | None = "DN",
    line: str | None = "DN",
    km: float | None = 67.5,
    due: str | None = None,
    days: float | None = 22.0,
    has_tsr: bool | None = False,
    duration: float | None = None,
    method: FindingMethod = FindingMethod.NATIVE_TEXT,
    uncertainty: Uncertainty = Uncertainty.LOW,
    reference: str = "page:1",
    page: int | None = 1,
) -> DefectCandidate:
    return DefectCandidate(
        candidate_id=candidate_id,
        statement=excerpt,
        excerpt=excerpt,
        evidence=(_evidence(excerpt, reference, page),),
        department=department,
        reported_defect_type=reported_type,
        normalized_defect_type=_sourced(defect_type, type_origin),
        reported_severity_text=severity_text,
        severity=_sourced(severity),
        reported_track_line=reported_line,
        track_line=_sourced(line, ValueOrigin.REPORTED if line == reported_line else ValueOrigin.NORMALIZED),
        chainage_km=_sourced(km),
        due_date=_sourced(due),
        days_overdue=_sourced(days),
        has_tsr=_sourced(has_tsr),
        tsr_speed_kmh=None,
        reported_duration_min=_sourced(duration),
        reported_asset_ref=None,
        extraction_method=method,
        missing_fields=(),
        uncertainty=uncertainty,
        validation_warnings=(),
        review_status=ReviewStatus.READY,
    )


def _understanding(
    candidates: tuple[DefectCandidate, ...] = (),
    ruled: tuple[RuledOutFinding, ...] = (),
    *,
    status: UnderstandingStatus = UnderstandingStatus.COMPLETE,
    error: str | None = None,
    report_id: str = _REPORT_ID,
) -> ReportUnderstanding:
    return ReportUnderstanding(
        report_id=report_id,
        filename=_FILENAME,
        summary="",
        candidates=candidates,
        ruled_out=ruled,
        warnings=(),
        status=status,
        provider_name="mock",
        chunks_processed=1,
        error=error,
        nlp_status="pending",
    )


def _guard_backlog(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("preview validation touched backlog state")

    monkeypatch.setattr(backlog_state, "get_active_defects", refuse)
    monkeypatch.setattr(backlog_state, "complete_defects", refuse)
    monkeypatch.setattr(data_ingestion, "get_effective_defects", refuse)
    monkeypatch.setattr(data_ingestion, "set_last_ingest", refuse)
    monkeypatch.setattr(defects, "compute_priority_urgency_score", refuse)
    monkeypatch.setattr(defects, "cluster_defects_into_bundles", refuse)


@pytest.mark.parametrize(
    ("defect_type", "km", "line", "severity", "days", "has_tsr", "reported_type"),
    [
        (DefectType.RAIL_FRACTURE, 15.2, "DN_FAST", 92, 18.0, False, "rail fracture"),
        (DefectType.WELD_FAILURE, 67.5, "DN", 84, 22.0, False, "weld failure"),
        (DefectType.OHE_SAG, 86.4, "UP", 87, 12.0, True, "ohe sag"),
        (DefectType.SIGNAL_INTERLOCK, 9.4, "UP_FAST", 74, 6.0, False, "signal interlock"),
        (DefectType.TAMPING_REQUIRED, 33.8, "DN_SLOW", 58, 0.0, False, "tamping required"),
        (DefectType.DEEP_SCREENING, 156.0, "DN", 64, 7.0, False, "deep screening"),
    ],
)
def test_every_defect_type_uses_make_defect_rules(
    defect_type: DefectType,
    km: float,
    line: str,
    severity: int,
    days: float,
    has_tsr: bool,
    reported_type: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _guard_backlog(monkeypatch)
    excerpt = f"{reported_type} at KM {km} on the {line} line."
    duration = 15.0 if defect_type is DefectType.RAIL_FRACTURE else None
    candidate = _candidate(
        excerpt,
        department=None,
        reported_type=reported_type,
        defect_type=defect_type.value,
        severity_text=f"severity {severity}",
        severity=severity,
        reported_line=line,
        line=line,
        km=km,
        days=days,
        has_tsr=has_tsr,
        duration=duration,
    )
    preview = validate_candidates(_understanding((candidate,)))
    record = preview.records[0]
    spec_minutes, machine, block_type = DEFECT_TYPE_SPEC[defect_type]

    assert preview.report_id == _REPORT_ID
    assert record.report_id == _REPORT_ID
    assert record.filename == _FILENAME
    assert record.status is PreviewStatus.READY_TO_APPLY
    assert record.missing_fields == ()
    assert record.scheduling_record is not None
    assert record.scheduling_record.defect_id == record.preview_id
    assert record.preview_id.startswith("AI1-")
    assert record.scheduling_record.defect_type is defect_type
    assert record.scheduling_record.km_location == km
    assert record.scheduling_record.track_line == line
    assert record.scheduling_record.severity == severity
    assert record.scheduling_record.days_overdue == days
    assert record.scheduling_record.has_tsr is has_tsr
    assert record.scheduling_record.required_block_min == spec_minutes
    assert record.scheduling_record.required_machine is machine
    assert record.scheduling_record.block_type is block_type
    assert record.scheduling_record.section_id == topology.get_section_id_at_km(km)
    assert record.scheduling_record.power_sector_id == defects.get_power_subsector_for_location(km, line)
    assert record.scheduling_record.is_mandatory is (severity >= defects.MANDATORY_SEVERITY_THRESHOLD)
    assert record.evidence.excerpt == excerpt
    assert record.evidence.references == ("page:1",)
    assert record.evidence.reported_defect_type == reported_type
    assert record.evidence.normalized_defect_type == defect_type.value
    assert record.evidence.severity == severity
    assert record.evidence.severity_origin is ValueOrigin.REPORTED
    assert record.evidence.reported_severity_text == f"severity {severity}"
    if defect_type is DefectType.RAIL_FRACTURE:
        assert record.scheduling_record.required_block_min == 90.0
        assert record.evidence.reported_duration_min == 15.0
        assert any("DEFECT_TYPE_SPEC" in note for note in record.notes)
    if defect_type is DefectType.TAMPING_REQUIRED:
        assert record.evidence.days_overdue == 0.0
        assert record.evidence.days_overdue_origin is ValueOrigin.REPORTED


def test_corridor_ends_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _guard_backlog(monkeypatch)
    start = _candidate(
        "Weld failure at KM 0.0 on the UP fast line.",
        defect_type="WELD_FAILURE",
        line="UP_FAST",
        reported_line="UP_FAST",
        km=0.0,
        severity=60,
        severity_text="severity 60",
        days=1.0,
    )
    end = _candidate(
        "Weld failure at KM 192.0 on the DN line.",
        candidate_id="F2",
        defect_type="WELD_FAILURE",
        line="DN",
        reported_line="DN",
        km=192.0,
        severity=60,
        severity_text="severity 60",
        days=1.0,
        reference="page:2",
        page=2,
    )
    preview = validate_candidates(_understanding((start, end)))
    assert [item.status for item in preview.records] == [
        PreviewStatus.READY_TO_APPLY,
        PreviewStatus.READY_TO_APPLY,
    ]
    assert preview.records[0].scheduling_record is not None
    assert preview.records[1].scheduling_record is not None
    assert preview.records[0].scheduling_record.section_id == "SECTION_1"
    assert preview.records[1].scheduling_record.section_id == "SECTION_4"


@pytest.mark.parametrize(
    ("code", "km", "line_code", "expected_line"),
    [
        ("RF", 15.2, "2", "DN_FAST"),
        ("WF", 67.5, "6", "DN"),
        ("TR", 33.8, "4", "DN_SLOW"),
        ("DS", 156.0, "6", "DN"),
    ],
)
def test_tms_codes_grades_and_line_numbers(code: str, km: float, line_code: str, expected_line: str) -> None:
    excerpt = f"TMS defect code {code} grade 9 at KM {km}."
    candidate = _candidate(
        excerpt,
        department="TMS",
        reported_type=code,
        defect_type=None,
        severity_text="grade 9",
        severity=None,
        reported_line=line_code,
        line=None,
        km=km,
        days=4.0,
        has_tsr=True,
    )
    record = validate_candidates(_understanding((candidate,))).records[0]

    assert record.status is PreviewStatus.READY_TO_APPLY
    assert record.evidence.reported_defect_type == code
    assert record.evidence.normalized_defect_type == TMS_DEFECT_CODES[code].value
    assert record.evidence.defect_type_origin is ValueOrigin.NORMALIZED
    assert record.evidence.reported_severity_text == "grade 9"
    assert record.evidence.severity == min(100, max(1, 9 * 10))
    assert record.evidence.severity_origin is ValueOrigin.NORMALIZED
    assert record.evidence.reported_track_line == line_code
    assert record.evidence.track_line == expected_line
    assert record.evidence.track_line == TMS_LINE_CODES[line_code]
    assert record.scheduling_record is not None
    assert record.scheduling_record.severity == 90
    assert record.scheduling_record.track_line == expected_line
    assert any("original wording" in note for note in record.notes)


def test_reported_code_must_match_the_normalized_type() -> None:
    candidate = _candidate(
        "TMS weld code at KM 67.5 on the DN line.",
        department="TMS",
        reported_type="WF",
        defect_type="RAIL_FRACTURE",
        severity_text="severity 84",
        severity=84,
    )
    record = validate_candidates(_understanding((candidate,))).records[0]
    assert record.status is PreviewStatus.REJECTED
    assert record.scheduling_record is None
    assert "WF" in record.reasons[0]
    assert "RAIL_FRACTURE" in record.reasons[0]


def test_explicit_severity_is_not_replaced_by_the_grade_map() -> None:
    candidate = _candidate(
        "TMS rail fracture grade 9, severity 92, at KM 15.2 on the DN fast line.",
        department="TMS",
        reported_type="rail fracture",
        defect_type="RAIL_FRACTURE",
        severity_text="grade 9",
        severity=92,
        reported_line="DN fast",
        line="DN_FAST",
        km=15.2,
        days=18.0,
        has_tsr=False,
    )
    record = validate_candidates(_understanding((candidate,))).records[0]
    assert record.evidence.severity == 92
    assert record.evidence.severity_origin is ValueOrigin.REPORTED
    assert record.evidence.reported_severity_text == "grade 9"
    assert record.scheduling_record is not None
    assert record.scheduling_record.severity == 92


def test_grade_without_a_source_is_not_mapped() -> None:
    candidate = _candidate(
        "Rail fracture grade 9 at KM 15.2 on the DN fast line.",
        department=None,
        reported_type="rail fracture",
        defect_type="RAIL_FRACTURE",
        severity_text="grade 9",
        severity=None,
        reported_line="DN_FAST",
        line="DN_FAST",
        km=15.2,
        days=4.0,
        has_tsr=False,
    )
    record = validate_candidates(_understanding((candidate,))).records[0]
    assert record.status is PreviewStatus.NEEDS_REVIEW
    assert record.missing_fields == ("severity",)
    assert record.evidence.severity is None
    assert record.evidence.reported_severity_text == "grade 9"
    assert record.scheduling_record is None
    assert any("no source map" in note for note in record.notes)


@pytest.mark.parametrize("band", ["CRITICAL", "MAJOR", "MINOR", "ROUTINE"])
def test_smms_priority_bands_keep_the_original_word(band: str) -> None:
    candidate = _candidate(
        f"SMMS point machine fault, priority {band}, at KM 9.4 on the UP fast line.",
        department="SMMS",
        reported_type="POINT_MACHINE_FAULT",
        defect_type=None,
        severity_text=band,
        severity=None,
        reported_line="UP FAST",
        line=None,
        km=9.4,
        days=6.0,
        has_tsr=False,
    )
    record = validate_candidates(_understanding((candidate,))).records[0]
    assert record.status is PreviewStatus.READY_TO_APPLY
    assert record.evidence.reported_defect_type == "POINT_MACHINE_FAULT"
    assert record.evidence.normalized_defect_type == SMMS_FAULT_TYPES["POINT_MACHINE_FAULT"].value
    assert record.evidence.reported_severity_text == band
    assert record.evidence.severity == SMMS_PRIORITY_SEVERITY[band]
    assert record.evidence.severity_origin is ValueOrigin.NORMALIZED
    assert record.evidence.track_line == "UP_FAST"
    assert record.scheduling_record is not None
    assert record.scheduling_record.defect_type is DefectType.SIGNAL_INTERLOCK


@pytest.mark.parametrize("fault", sorted(SMMS_FAULT_TYPES))
def test_every_smms_fault_code_maps_onto_signal_interlock(fault: str) -> None:
    candidate = _candidate(
        f"SMMS {fault} at KM 9.4.",
        department="SMMS",
        reported_type=fault,
        defect_type=None,
        severity_text="MINOR",
        severity=None,
        reported_line="UP_FAST",
        line="UP_FAST",
        km=9.4,
        days=1.0,
        has_tsr=False,
    )
    record = validate_candidates(_understanding((candidate,))).records[0]
    assert record.evidence.normalized_defect_type == DefectType.SIGNAL_INTERLOCK.value
    assert record.evidence.reported_defect_type == fault
    assert record.scheduling_record is not None


@pytest.mark.parametrize("category", sorted(TDMS_CATEGORY_MAP))
def test_tdms_categories_and_sag_use_the_existing_divisor(category: str) -> None:
    candidate = _candidate(
        f"TDMS {category} sag 90 mm at KM 86.4 on the UP line.",
        department="TDMS",
        reported_type=category,
        defect_type=None,
        severity_text="sag 90 mm",
        severity=None,
        reported_line="UP",
        line="UP",
        km=86.4,
        days=12.0,
        has_tsr=True,
    )
    record = validate_candidates(_understanding((candidate,))).records[0]
    expected = min(100, max(1, round(90 / TDMS_SAG_TO_SEVERITY_DIVISOR)))
    assert record.status is PreviewStatus.READY_TO_APPLY
    assert record.evidence.reported_defect_type == category
    assert record.evidence.normalized_defect_type == DefectType.OHE_SAG.value
    assert record.evidence.reported_severity_text == "sag 90 mm"
    assert record.evidence.severity == expected
    assert record.evidence.severity_origin is ValueOrigin.NORMALIZED
    assert record.scheduling_record is not None
    assert record.scheduling_record.required_machine is DEFECT_TYPE_SPEC[DefectType.OHE_SAG][1]


def test_large_sag_is_clamped_by_the_existing_map_and_the_wording_remains() -> None:
    candidate = _candidate(
        "TDMS conductor sag sag 900 mm at KM 86.4 on the UP line.",
        department="TDMS",
        reported_type="CONDUCTOR_SAG",
        defect_type=None,
        severity_text="sag 900 mm",
        severity=None,
        reported_line="UP",
        line="UP",
        km=86.4,
        days=1.0,
        has_tsr=False,
    )
    record = validate_candidates(_understanding((candidate,))).records[0]
    assert record.evidence.reported_severity_text == "sag 900 mm"
    assert record.evidence.severity == 100
    assert record.scheduling_record is not None
    assert record.scheduling_record.severity == 100


def test_invalid_chainage_line_severity_and_source_values_are_rejected() -> None:
    outside = _candidate(
        "Weld failure at KM 210 on the DN line.",
        candidate_id="F-out",
        km=210.0,
        reference="page:2",
    )
    below = _candidate(
        "Weld failure at KM -0.1 on the DN line.",
        candidate_id="F-below",
        km=-0.1,
        line="DN",
        reported_line="DN",
        reference="page:3",
    )
    mismatch = _candidate(
        "Rail fracture at KM 102.3 on the UP fast line.",
        candidate_id="F-line",
        defect_type="RAIL_FRACTURE",
        reported_type="rail fracture",
        line="UP_FAST",
        reported_line="UP fast",
        km=102.3,
        severity=96,
        severity_text="severity 96",
        days=28.0,
        has_tsr=True,
        reference="page:4",
    )
    low = _candidate("Weld failure severity 0.", candidate_id="F-low", severity=0, severity_text="severity 0")
    high = _candidate("Weld failure severity 101.", candidate_id="F-high", severity=101, severity_text="severity 101")
    fraction = _candidate("Weld failure severity 92.5.", candidate_id="F-fraction", severity=92.5, severity_text="severity 92.5")
    grade = _candidate(
        "TMS grade 11 weld failure at KM 67.5 on the DN line.",
        candidate_id="F-grade",
        department="TMS",
        severity=None,
        severity_text="grade 11",
    )
    band = _candidate(
        "SMMS priority EXTREME at KM 9.4 on the UP fast line.",
        candidate_id="F-band",
        department="SMMS",
        defect_type="SIGNAL_INTERLOCK",
        line="UP_FAST",
        reported_line="UP_FAST",
        km=9.4,
        severity=None,
        severity_text="EXTREME",
        days=1.0,
    )
    sag = _candidate(
        "TDMS sag 0 mm at KM 86.4 on the UP line.",
        candidate_id="F-sag",
        department="TDMS",
        defect_type="OHE_SAG",
        line="UP",
        reported_line="UP",
        km=86.4,
        severity=None,
        severity_text="sag 0 mm",
        days=1.0,
    )
    unknown_type = _candidate(
        "A pothole was logged.",
        candidate_id="F-type",
        reported_type="POTHOLE",
        defect_type="POTHOLE",
    )
    unknown_line = _candidate(
        "TMS line 9 weld failure at KM 67.5.",
        candidate_id="F-line-code",
        department="TMS",
        reported_line="9",
        line=None,
    )
    negative_days = _candidate("Weld failure overdue -3 days.", candidate_id="F-days", days=-3.0)

    preview = validate_candidates(
        _understanding((outside, below, mismatch, low, high, fraction, grade, band, sag, unknown_type, unknown_line, negative_days))
    )
    by_id = {item.source_finding_id: item for item in preview.records}

    assert all(item.scheduling_record is None for item in preview.records)
    assert all(item.status is PreviewStatus.REJECTED for item in preview.records)
    assert "outside the corridor" in by_id["F-out"].reasons[0]
    assert "page:2" in by_id["F-out"].reasons[0]
    assert "outside the corridor" in by_id["F-below"].reasons[0]
    assert "not present" in by_id["F-line"].reasons[0]
    assert "UP_FAST" in by_id["F-line"].reasons[0]
    assert "102.3" in by_id["F-line"].reasons[0]
    assert "page:4" in by_id["F-line"].reasons[0]
    assert "[1, 100]" in by_id["F-low"].reasons[0]
    assert "[1, 100]" in by_id["F-high"].reasons[0]
    assert "[1, 100]" in by_id["F-fraction"].reasons[0]
    assert "GRADE must be in [1, 10]" in by_id["F-grade"].reasons[0]
    assert "PRIORITY_BAND" in by_id["F-band"].reasons[0]
    assert "sag_measurement_mm must be positive" in by_id["F-sag"].reasons[0]
    assert "POTHOLE" in by_id["F-type"].reasons[0]
    assert "LINE_ID" in by_id["F-line-code"].reasons[0]
    assert "cannot be negative" in by_id["F-days"].reasons[0]


def test_missing_severity_tsr_and_overdue_stay_unknown() -> None:
    candidate = _candidate(
        "Weld failure at KM 67.5 on the DN line.",
        severity=None,
        severity_text=None,
        days=None,
        has_tsr=None,
        due=None,
    )
    record = validate_candidates(_understanding((candidate,))).records[0]
    assert record.status is PreviewStatus.NEEDS_REVIEW
    assert record.missing_fields == ("severity", "days_overdue", "has_tsr")
    assert record.scheduling_record is None
    assert record.evidence.severity is None
    assert record.evidence.days_overdue is None
    assert record.evidence.has_tsr is None
    assert record.evidence.excerpt == candidate.excerpt
    assert "page:1" in record.reasons[0]


def test_tms_overdue_is_computed_only_from_a_supplied_reference_date() -> None:
    excerpt = "TMS weld failure at KM 67.5 on the DN line, severity 84, due 03-09-2026."
    candidate = _candidate(
        excerpt,
        department="TMS",
        severity_text="severity 84",
        severity=84,
        due="03-09-2026",
        days=None,
        has_tsr=False,
    )
    missing_reference = validate_candidates(_understanding((candidate,))).records[0]
    computed = validate_candidates(
        _understanding((candidate,)),
        as_of=date(2026, 9, 21),
    ).records[0]
    future = _candidate(
        "TMS weld failure at KM 67.5 on the DN line, severity 84, due 01-10-2026.",
        department="TMS",
        due="01-10-2026",
        days=None,
        has_tsr=False,
    )
    future_record = validate_candidates(_understanding((future,)), as_of=date(2026, 9, 21)).records[0]

    assert missing_reference.status is PreviewStatus.NEEDS_REVIEW
    assert missing_reference.missing_fields == ("days_overdue",)
    assert missing_reference.evidence.days_overdue is None
    assert missing_reference.scheduling_record is None
    assert computed.status is PreviewStatus.READY_TO_APPLY
    assert computed.evidence.days_overdue == 18.0
    assert computed.evidence.days_overdue_origin is ValueOrigin.NORMALIZED
    assert computed.evidence.reported_due_date == "03-09-2026"
    assert computed.scheduling_record is not None
    assert computed.scheduling_record.days_overdue == 18.0
    assert future_record.evidence.days_overdue == 0.0
    assert future_record.evidence.days_overdue_origin is ValueOrigin.NORMALIZED
    assert any("computed from the stated due date" in note for note in future_record.notes)


def test_smms_does_not_invent_overdue_days_from_a_date() -> None:
    candidate = _candidate(
        "SMMS signal interlock at KM 9.4 on the UP fast line, priority MINOR, due 03-09-2026.",
        department="SMMS",
        defect_type="SIGNAL_INTERLOCK",
        line="UP_FAST",
        reported_line="UP_FAST",
        km=9.4,
        severity_text="MINOR",
        severity=None,
        due="03-09-2026",
        days=None,
        has_tsr=False,
    )
    record = validate_candidates(_understanding((candidate,)), as_of=date(2026, 9, 21)).records[0]
    assert record.status is PreviewStatus.NEEDS_REVIEW
    assert record.missing_fields == ("days_overdue",)
    assert record.evidence.days_overdue is None
    assert record.evidence.severity == SMMS_PRIORITY_SEVERITY["MINOR"]
    assert record.scheduling_record is None


def test_possible_duplicate_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    _guard_backlog(monkeypatch)
    near = _candidate(
        "Weld failure at KM 67.5 on the DN line.",
        candidate_id="F-near",
        reference="page:1",
    )
    also_near = _candidate(
        "Weld failure at KM 70.5 on the DN line.",
        candidate_id="F-also",
        km=70.5,
        reference="page:2",
        page=2,
    )
    far = _candidate(
        "Weld failure at KM 74.0 on the DN line.",
        candidate_id="F-far",
        km=74.0,
        reference="page:3",
        page=3,
    )
    other_type = _candidate(
        "OHE sag at KM 67.5 on the DN line.",
        candidate_id="F-other",
        defect_type="OHE_SAG",
        reported_type="ohe sag",
        line="DN",
        reported_line="DN",
        km=67.5,
        severity=87,
        severity_text="severity 87",
        days=12.0,
        has_tsr=True,
        reference="page:4",
        page=4,
    )
    existing = _make_defect("DEF_EXISTING", 68.0, "DN", DefectType.WELD_FAILURE, 70, days_overdue=1.0, has_tsr=False)
    preview = validate_candidates(
        _understanding((near, also_near, far, other_type)),
        existing_defects=(existing,),
    )
    by_id = {item.source_finding_id: item for item in preview.records}

    assert by_id["F-near"].status is PreviewStatus.POSSIBLE_DUPLICATE
    assert by_id["F-also"].status is PreviewStatus.POSSIBLE_DUPLICATE
    assert by_id["F-near"].scheduling_record is not None
    assert by_id["F-also"].scheduling_record is not None
    assert by_id["F-also"].preview_id in by_id["F-near"].duplicate_of
    assert "DEF_EXISTING" in by_id["F-near"].duplicate_of
    assert by_id["F-near"].preview_id in by_id["F-also"].duplicate_of
    assert "DEF_EXISTING" in by_id["F-also"].duplicate_of
    assert by_id["F-far"].status is PreviewStatus.READY_TO_APPLY
    assert by_id["F-far"].duplicate_of == ()
    assert by_id["F-other"].status is PreviewStatus.READY_TO_APPLY
    assert by_id["F-other"].duplicate_of == ()
    assert any("proximity" in reason.casefold() for reason in by_id["F-near"].reasons)
    assert existing.defect_id == "DEF_EXISTING"


def test_same_problem_fields_match_the_backlog_predicate() -> None:
    left = _make_defect("LEFT", 10.0, "UP_FAST", DefectType.RAIL_FRACTURE, 80, days_overdue=1.0, has_tsr=False)
    close = _make_defect("CLOSE", 13.0, "UP_FAST", DefectType.RAIL_FRACTURE, 80, days_overdue=1.0, has_tsr=False)
    edge = _make_defect("EDGE", 13.01, "UP_FAST", DefectType.RAIL_FRACTURE, 80, days_overdue=1.0, has_tsr=False)
    other_line = _make_defect("OTHER", 10.0, "DN_FAST", DefectType.RAIL_FRACTURE, 80, days_overdue=1.0, has_tsr=False)
    pairs = ((left, close), (left, edge), (left, other_line))
    for first, second in pairs:
        assert same_problem_fields(
            first.defect_type,
            first.track_line,
            first.km_location,
            second.defect_type,
            second.track_line,
            second.km_location,
        ) is backlog_state._same_problem(first, second)


def test_ruled_out_finding_and_negated_candidate_are_not_schedulable() -> None:
    excerpt = "No rail fracture was observed."
    negated = _candidate(
        excerpt,
        candidate_id="F-no",
        defect_type="RAIL_FRACTURE",
        reported_type="rail fracture",
        severity=90,
        severity_text="severity 90",
        line="DN_FAST",
        reported_line="DN_FAST",
        km=15.2,
        days=1.0,
        has_tsr=False,
    )
    ruled = RuledOutFinding(
        finding_id="R1",
        statement="TSR removed at KM 86.4.",
        excerpt="TSR removed at KM 86.4.",
        evidence=(_evidence("TSR removed at KM 86.4.", "page:5", 5),),
        reason="tsr_lifted",
        extraction_method=FindingMethod.NATIVE_TEXT,
    )
    preview = validate_candidates(_understanding((negated,), (ruled,)))
    assert [item.status for item in preview.records] == [PreviewStatus.RULED_OUT, PreviewStatus.RULED_OUT]
    assert all(item.scheduling_record is None for item in preview.records)
    assert preview.records[0].evidence.excerpt == excerpt
    assert "negated_finding" in preview.records[0].reasons[0]
    assert "page:1" in preview.records[0].reasons[0]
    assert preview.records[1].evidence.references == ("page:5",)
    assert preview.records[1].evidence.page_numbers == (5,)
    assert "tsr_lifted" in preview.records[1].reasons[0]
    assert "page:5" in preview.records[1].reasons[0]


def test_ambiguous_and_visual_findings_stay_visible_without_a_track_defect() -> None:
    ambiguous = _candidate(
        "Inspectors cannot rule out a rail crack at KM 10.0 on the UP fast line.",
        candidate_id="F-maybe",
        defect_type="RAIL_FRACTURE",
        reported_type="rail crack",
        line="UP_FAST",
        reported_line="UP fast",
        km=10.0,
        severity=80,
        severity_text="severity 80",
        days=2.0,
        has_tsr=False,
        uncertainty=Uncertainty.HIGH,
    )
    visual = _candidate(
        "Possible unusual rail head in the attached photograph.",
        candidate_id="F-photo",
        defect_type="RAIL_FRACTURE",
        line="DN_FAST",
        reported_line="DN_FAST",
        km=15.2,
        severity=90,
        severity_text="severity 90",
        days=1.0,
        has_tsr=False,
        method=FindingMethod.VISUAL,
        reference="page:1/image:1",
        page=1,
    )
    preview = validate_candidates(_understanding((ambiguous, visual)))
    assert preview.records[0].status is PreviewStatus.NEEDS_REVIEW
    assert preview.records[0].scheduling_record is None
    assert preview.records[0].missing_fields == ()
    assert "ambiguous" in preview.records[0].reasons[0]
    assert preview.records[1].status is PreviewStatus.NEEDS_REVIEW
    assert preview.records[1].scheduling_record is None
    assert preview.records[1].evidence.references == ("page:1/image:1",)
    assert "photograph" in preview.records[1].reasons[0]


def test_failed_understanding_and_bad_fields_do_not_call_make_defect(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = defects._make_defect

    def wrapped(*args: object, **kwargs: object):
        calls.append("called")
        return real(*args, **kwargs)

    monkeypatch.setattr(defects, "_make_defect", wrapped)
    sneaky = _candidate("Weld failure at KM 67.5 on the DN line.", candidate_id="F-sneaky")
    failed = validate_candidates(
        _understanding((sneaky,), status=UnderstandingStatus.FAILED, error="Provider output is not valid JSON.")
    )
    empty = validate_candidates(
        _understanding(status=UnderstandingStatus.FAILED, error="Provider output is not valid JSON.")
    )
    assert calls == []
    assert failed.records[0].status is PreviewStatus.REJECTED
    assert failed.records[0].scheduling_record is None
    assert "not valid JSON" in failed.records[0].reasons[0]
    assert "page:1" in failed.records[0].reasons[0]
    assert empty.records == ()
    assert empty.understanding_error == "Provider output is not valid JSON."


def test_one_report_keeps_several_records_and_distinct_ids() -> None:
    ready = _candidate("Weld failure at KM 67.5 on the DN line.", candidate_id="F1", reference="page:1")
    incomplete = _candidate(
        "OHE sag at KM 86.4 on the UP line.",
        candidate_id="F2",
        defect_type="OHE_SAG",
        reported_type="ohe sag",
        line="UP",
        reported_line="UP",
        km=86.4,
        severity=87,
        severity_text="severity 87",
        days=12.0,
        has_tsr=None,
        reference="page:2",
        page=2,
    )
    ruled = RuledOutFinding(
        finding_id="R1",
        statement="Nil defect was recorded.",
        excerpt="Nil defect was recorded.",
        evidence=(_evidence("Nil defect was recorded.", "page:3", 3),),
        reason="nil_report",
        extraction_method=FindingMethod.OCR,
    )
    preview = validate_candidates(_understanding((ready, incomplete), (ruled,)))
    ids = [item.preview_id for item in preview.records]

    assert [item.status for item in preview.records] == [
        PreviewStatus.READY_TO_APPLY,
        PreviewStatus.NEEDS_REVIEW,
        PreviewStatus.RULED_OUT,
    ]
    assert len(set(ids)) == 3
    assert all(item.report_id == _REPORT_ID for item in preview.records)
    assert preview.records[1].missing_fields == ("has_tsr",)
    assert preview.records[1].scheduling_record is None
    assert preview.records[1].evidence.page_numbers == (2,)
    assert preview.records[2].evidence.extraction_method is FindingMethod.OCR
    again = validate_candidates(_understanding((ready, incomplete), (ruled,)))
    assert [item.preview_id for item in again.records] == ids


def test_a_different_report_gets_a_different_defect_id() -> None:
    candidate = _candidate("Weld failure at KM 67.5 on the DN line.")
    first = validate_candidates(_understanding((candidate,), report_id="report-aaaaaaaaaaaa")).records[0]
    second = validate_candidates(_understanding((candidate,), report_id="report-bbbbbbbbbbbb")).records[0]
    assert first.preview_id != second.preview_id
    assert first.report_id == "report-aaaaaaaaaaaa"
    assert second.scheduling_record is not None
    assert second.scheduling_record.defect_id == second.preview_id
