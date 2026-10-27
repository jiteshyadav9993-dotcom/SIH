"""
report_preview.py
====================================================================
Turn report candidates into preview records. This does not write the
backlog, score urgency, or choose a possession window.

A scheduling TrackDefect is built only after defect type, line, chainage,
severity, days overdue, and TSR have all been checked. _make_defect()
then fills section, power sector, block minutes, machine, block type,
and the mandatory flag. Formula 1 consumes those inputs and does not
calculate them. Formula 2 groups nearby work; it is not used here.

The 3 km duplicate check is the same type-and-line test as
backlog_state._same_problem. A hit is a warning, not a rejection.
====================================================================
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Sequence

import backlog_state
import defects
import topology
from data_ingestion import (
    SMMS_FAULT_TYPES,
    SMMS_PRIORITY_SEVERITY,
    TDMS_CATEGORY_MAP,
    TDMS_SAG_TO_SEVERITY_DIVISOR,
    TMS_DEFECT_CODES,
    TMS_LINE_CODES,
    _parse_ddmmyyyy,
    _parse_iso_date,
)
from defects import (
    SOURCE_SMMS,
    SOURCE_TDMS,
    SOURCE_TMS,
    DefectType,
    TrackDefect,
)
from report_schema import (
    DefectCandidate,
    FindingMethod,
    ReportUnderstanding,
    RuledOutFinding,
    SourcedValue,
    UnderstandingStatus,
    ValueOrigin,
)
from report_validate import classify_polarity

_LINE_CODES = frozenset(
    {
        topology.LINE_UP_FAST,
        topology.LINE_DN_FAST,
        topology.LINE_UP_SLOW,
        topology.LINE_DN_SLOW,
        topology.LINE_UP,
        topology.LINE_DN,
        topology.LINE_MID,
    }
)
_REQUIRED_FIELDS = (
    "defect_type",
    "track_line",
    "chainage_km",
    "severity",
    "days_overdue",
    "has_tsr",
)
_GRADE_RE = re.compile(r"\bgrade\s*[:=]?\s*(\d{1,2})\b", re.IGNORECASE)
_SEVERITY_RE = re.compile(r"\bseverity(?:\s+of|\s+is|\s*[:=])?\s*(\d{1,3})\b", re.IGNORECASE)
_BAND_RE = re.compile(r"\b(CRITICAL|MAJOR|MINOR|ROUTINE)\b", re.IGNORECASE)
_SAG_RE = re.compile(
    r"\bsag(?:\s+measurement)?(?:\s+of)?(?:_mm)?\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*(?:mm)?\b",
    re.IGNORECASE,
)
_RULED_OUT_REASONS = {
    "negated": "negated_finding",
    "nil": "nil_report",
    "tsr_lifted": "tsr_lifted",
}


class PreviewStatus(str, Enum):
    """Preview disposition. This is not a possession outcome and not ReviewStatus."""

    READY_TO_APPLY = "READY_TO_APPLY"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    RULED_OUT = "RULED_OUT"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class PreviewEvidence:
    """Report evidence kept beside the scheduling record, not inside it."""

    filename: str
    references: tuple[str, ...]
    page_numbers: tuple[int | None, ...]
    statement: str
    excerpt: str
    extraction_method: FindingMethod
    department: str | None
    reported_defect_type: str | None
    normalized_defect_type: str | None
    defect_type_origin: ValueOrigin
    reported_severity_text: str | None
    severity: int | None
    severity_origin: ValueOrigin
    reported_track_line: str | None
    track_line: str | None
    track_line_origin: ValueOrigin
    chainage_km: float | None
    chainage_origin: ValueOrigin
    reported_due_date: str | None
    days_overdue: float | None
    days_overdue_origin: ValueOrigin
    has_tsr: bool | None
    has_tsr_origin: ValueOrigin
    reported_duration_min: float | None
    reported_asset_ref: str | None


@dataclass(frozen=True, slots=True)
class CandidatePreview:
    """One finding after deterministic checks. scheduling_record is set only when complete."""

    preview_id: str
    report_id: str
    filename: str
    source_finding_id: str
    status: PreviewStatus
    missing_fields: tuple[str, ...]
    reasons: tuple[str, ...]
    notes: tuple[str, ...]
    duplicate_of: tuple[str, ...]
    evidence: PreviewEvidence
    scheduling_record: TrackDefect | None


@dataclass(frozen=True, slots=True)
class ReportPreview:
    """Preview for one report. Nothing here has been written to the backlog."""

    report_id: str
    filename: str
    understanding_status: UnderstandingStatus
    understanding_error: str | None
    records: tuple[CandidatePreview, ...]


@dataclass(frozen=True, slots=True)
class _Field:
    value: object
    origin: ValueOrigin
    reported_text: str | None
    error: str | None = None
    missing_name: str | None = None


@dataclass
class _Draft:
    preview_id: str
    source_finding_id: str
    status: PreviewStatus
    missing_fields: list[str]
    reasons: list[str]
    notes: list[str]
    duplicate_of: list[str]
    evidence: PreviewEvidence
    scheduling_record: TrackDefect | None
    identity: tuple[DefectType, str, float] | None


def validate_candidates(
    understanding: ReportUnderstanding,
    *,
    existing_defects: Sequence[TrackDefect] = (),
    as_of: date | None = None,
) -> ReportPreview:
    """Validate every finding. Existing defects are only read for the duplicate warning."""
    if understanding.status is UnderstandingStatus.FAILED:
        drafts = [
            _failed_candidate(understanding, candidate) for candidate in understanding.candidates
        ]
        drafts.extend(_ruled_out_draft(understanding, item) for item in understanding.ruled_out)
    else:
        drafts = [_candidate_draft(understanding, candidate, as_of) for candidate in understanding.candidates]
        drafts.extend(_ruled_out_draft(understanding, item) for item in understanding.ruled_out)
        _mark_duplicates(drafts, existing_defects)
    return ReportPreview(
        report_id=understanding.report_id,
        filename=understanding.filename,
        understanding_status=understanding.status,
        understanding_error=understanding.error,
        records=tuple(_freeze(understanding, draft) for draft in drafts),
    )


def same_problem_fields(
    left_type: DefectType,
    left_line: str,
    left_km: float,
    right_type: DefectType,
    right_line: str,
    right_km: float,
) -> bool:
    """Same test as backlog_state._same_problem, without creating a backlog row."""
    return (
        left_type == right_type
        and left_line == right_line
        and abs(float(left_km) - float(right_km)) <= backlog_state.SAME_PROBLEM_KM
    )


def _candidate_draft(
    understanding: ReportUnderstanding,
    candidate: DefectCandidate,
    as_of: date | None,
) -> _Draft:
    references = _references(candidate)
    preview_id = _stable_id(understanding.report_id, understanding.filename, candidate.candidate_id, references)
    polarity = classify_polarity(candidate.excerpt)
    if polarity in _RULED_OUT_REASONS:
        evidence = _evidence_shell(understanding.filename, candidate)
        return _Draft(
            preview_id=preview_id,
            source_finding_id=candidate.candidate_id,
            status=PreviewStatus.RULED_OUT,
            missing_fields=[],
            reasons=[_with_source(_RULED_OUT_REASONS[polarity], references)],
            notes=[],
            duplicate_of=[],
            evidence=evidence,
            scheduling_record=None,
            identity=None,
        )

    defect_type = _resolve_type(candidate)
    line = _resolve_line(candidate)
    chainage = _resolve_chainage(candidate)
    severity = _resolve_severity(candidate)
    overdue = _resolve_overdue(candidate, as_of)
    tsr = _resolve_tsr(candidate)
    fields = {
        "defect_type": defect_type,
        "track_line": line,
        "chainage_km": chainage,
        "severity": severity,
        "days_overdue": overdue,
        "has_tsr": tsr,
    }
    errors = [item.error for item in fields.values() if item.error]
    missing = [name for name in _REQUIRED_FIELDS if fields[name].missing_name == name]
    presence_error = _line_at_chainage(line, chainage)
    if presence_error:
        errors.append(presence_error)
    ambiguous = _ambiguous_reason(candidate)
    notes = _notes_for(candidate, severity, overdue)
    identity = _identity(defect_type, line, chainage, presence_error)
    evidence = _evidence_from_candidate(understanding.filename, candidate, fields)
    if errors:
        return _Draft(
            preview_id=preview_id,
            source_finding_id=candidate.candidate_id,
            status=PreviewStatus.REJECTED,
            missing_fields=missing,
            reasons=[_with_source(error, references) for error in errors],
            notes=notes,
            duplicate_of=[],
            evidence=evidence,
            scheduling_record=None,
            identity=None,
        )
    if missing or ambiguous:
        reasons: list[str] = []
        if missing:
            reasons.append(_with_source("Required data is missing: " + ", ".join(missing) + ".", references))
        if ambiguous:
            reasons.append(_with_source(ambiguous, references))
        return _Draft(
            preview_id=preview_id,
            source_finding_id=candidate.candidate_id,
            status=PreviewStatus.NEEDS_REVIEW,
            missing_fields=missing,
            reasons=reasons,
            notes=notes,
            duplicate_of=[],
            evidence=evidence,
            scheduling_record=None,
            identity=identity,
        )
    assert isinstance(defect_type.value, DefectType)
    assert isinstance(line.value, str)
    assert isinstance(chainage.value, float)
    assert isinstance(severity.value, int)
    assert isinstance(overdue.value, float)
    assert isinstance(tsr.value, bool)
    try:
        scheduling = defects._make_defect(
            preview_id,
            chainage.value,
            line.value,
            defect_type.value,
            severity.value,
            days_overdue=overdue.value,
            has_tsr=tsr.value,
        )
    except ValueError as exc:
        return _Draft(
            preview_id=preview_id,
            source_finding_id=candidate.candidate_id,
            status=PreviewStatus.REJECTED,
            missing_fields=missing,
            reasons=[_with_source(str(exc), references)],
            notes=notes,
            duplicate_of=[],
            evidence=evidence,
            scheduling_record=None,
            identity=None,
        )
    duration_note = _duration_note(candidate, scheduling.required_block_min)
    if duration_note:
        notes.append(duration_note)
    return _Draft(
        preview_id=preview_id,
        source_finding_id=candidate.candidate_id,
        status=PreviewStatus.READY_TO_APPLY,
        missing_fields=[],
        reasons=[],
        notes=notes,
        duplicate_of=[],
        evidence=evidence,
        scheduling_record=scheduling,
        identity=(defect_type.value, line.value, float(chainage.value)),
    )


def _ruled_out_draft(understanding: ReportUnderstanding, finding: RuledOutFinding) -> _Draft:
    references = tuple(item.reference for item in finding.evidence)
    evidence = PreviewEvidence(
        filename=understanding.filename,
        references=references,
        page_numbers=tuple(item.page_number for item in finding.evidence),
        statement=finding.statement,
        excerpt=finding.excerpt,
        extraction_method=finding.extraction_method,
        department=None,
        reported_defect_type=None,
        normalized_defect_type=None,
        defect_type_origin=ValueOrigin.UNKNOWN,
        reported_severity_text=None,
        severity=None,
        severity_origin=ValueOrigin.UNKNOWN,
        reported_track_line=None,
        track_line=None,
        track_line_origin=ValueOrigin.UNKNOWN,
        chainage_km=None,
        chainage_origin=ValueOrigin.UNKNOWN,
        reported_due_date=None,
        days_overdue=None,
        days_overdue_origin=ValueOrigin.UNKNOWN,
        has_tsr=None,
        has_tsr_origin=ValueOrigin.UNKNOWN,
        reported_duration_min=None,
        reported_asset_ref=None,
    )
    return _Draft(
        preview_id=_stable_id(understanding.report_id, understanding.filename, finding.finding_id, references),
        source_finding_id=finding.finding_id,
        status=PreviewStatus.RULED_OUT,
        missing_fields=[],
        reasons=[_with_source(finding.reason, references)],
        notes=[],
        duplicate_of=[],
        evidence=evidence,
        scheduling_record=None,
        identity=None,
    )


def _failed_candidate(understanding: ReportUnderstanding, candidate: DefectCandidate) -> _Draft:
    references = _references(candidate)
    reason = understanding.error or "The report understanding failed, so this candidate was not converted."
    return _Draft(
        preview_id=_stable_id(understanding.report_id, understanding.filename, candidate.candidate_id, references),
        source_finding_id=candidate.candidate_id,
        status=PreviewStatus.REJECTED,
        missing_fields=[],
        reasons=[_with_source(reason, references)],
        notes=[],
        duplicate_of=[],
        evidence=_evidence_shell(understanding.filename, candidate),
        scheduling_record=None,
        identity=None,
    )


def _mark_duplicates(drafts: list[_Draft], existing_defects: Sequence[TrackDefect]) -> None:
    comparable = [draft for draft in drafts if draft.identity is not None and draft.status is not PreviewStatus.REJECTED]
    for index, draft in enumerate(comparable):
        assert draft.identity is not None
        for other in comparable[index + 1 :]:
            assert other.identity is not None
            if _identities_match(draft.identity, other.identity):
                _link_duplicate(draft, other.preview_id)
                _link_duplicate(other, draft.preview_id)
        for prior in existing_defects:
            if same_problem_fields(
                draft.identity[0],
                draft.identity[1],
                draft.identity[2],
                prior.defect_type,
                prior.track_line,
                prior.km_location,
            ):
                _link_duplicate(draft, prior.defect_id)


def _identities_match(
    left: tuple[DefectType, str, float],
    right: tuple[DefectType, str, float],
) -> bool:
    if left[0] == right[0] and left[1] == right[1]:
        return same_problem_fields(left[0], left[1], left[2], right[0], right[1], right[2])
    return False


def _link_duplicate(draft: _Draft, other_id: str) -> None:
    if other_id in draft.duplicate_of or other_id == draft.preview_id:
        return
    draft.duplicate_of.append(other_id)
    if draft.status is PreviewStatus.READY_TO_APPLY:
        draft.status = PreviewStatus.POSSIBLE_DUPLICATE
    draft.reasons.append(
        "Possible duplicate of "
        f"{other_id}: same defect type and line within {backlog_state.SAME_PROBLEM_KM:g} km. "
        "Proximity leaves the record available for a reviewer to decide."
    )


def _freeze(understanding: ReportUnderstanding, draft: _Draft) -> CandidatePreview:
    return CandidatePreview(
        preview_id=draft.preview_id,
        report_id=understanding.report_id,
        filename=understanding.filename,
        source_finding_id=draft.source_finding_id,
        status=draft.status,
        missing_fields=tuple(draft.missing_fields),
        reasons=tuple(draft.reasons),
        notes=tuple(draft.notes),
        duplicate_of=tuple(draft.duplicate_of),
        evidence=draft.evidence,
        scheduling_record=draft.scheduling_record,
    )


def _resolve_type(candidate: DefectCandidate) -> _Field:
    reported = _clean(candidate.reported_defect_type)
    raw = candidate.normalized_defect_type.value
    if isinstance(raw, DefectType):
        return _accept_type(raw, candidate.normalized_defect_type.origin, reported, candidate.department)
    if isinstance(raw, str) and raw.strip():
        token = raw.strip()
        enum_value = _enum_type(token)
        if enum_value is not None:
            return _accept_type(enum_value, candidate.normalized_defect_type.origin, reported or token, candidate.department)
        mapped, error = _type_from_source_code(token, candidate.department)
        if error is not None:
            return _Field(None, ValueOrigin.UNKNOWN, reported or token, error=error)
        if mapped is not None:
            return _accept_type(mapped, ValueOrigin.NORMALIZED, reported or token, candidate.department)
        return _Field(None, ValueOrigin.UNKNOWN, reported or token, error=f"Unsupported defect type {token!r}.")
    if reported:
        enum_value = _enum_type(reported)
        if enum_value is not None:
            return _Field(enum_value, ValueOrigin.REPORTED, reported)
        mapped, error = _type_from_source_code(reported, candidate.department)
        if mapped is not None:
            return _Field(mapped, ValueOrigin.NORMALIZED, reported)
        if error is not None:
            return _Field(None, ValueOrigin.UNKNOWN, reported, error=error)
        return _Field(None, ValueOrigin.UNKNOWN, reported, error=f"Unsupported defect type {reported!r}.")
    return _Field(None, ValueOrigin.UNKNOWN, None, missing_name="defect_type")


def _accept_type(
    chosen: DefectType,
    origin: ValueOrigin,
    reported: str | None,
    department: str | None,
) -> _Field:
    conflict = _type_conflict(reported, department, chosen)
    if conflict:
        return _Field(None, ValueOrigin.UNKNOWN, reported, error=conflict)
    return _Field(chosen, origin, reported)


def _type_conflict(reported: str | None, department: str | None, chosen: DefectType) -> str | None:
    if not reported:
        return None
    other = _enum_type(reported)
    source_error: str | None = None
    if other is None:
        other, source_error = _type_from_source_code(reported, department)
    if other is not None and other != chosen:
        return f"The reported defect type {reported!r} does not match the normalized type {chosen.value}."
    if other is None and source_error and " " not in reported.strip():
        return source_error
    return None


def _type_from_source_code(token: str, department: str | None) -> tuple[DefectType | None, str | None]:
    code = token.strip().upper().replace(" ", "_")
    if department == SOURCE_TMS:
        if code in TMS_DEFECT_CODES:
            return TMS_DEFECT_CODES[code], None
        if code not in DefectType._value2member_map_:
            return None, f"Unknown TMS DEFECT_CODE {code!r}"
    if department == SOURCE_SMMS:
        if code in SMMS_FAULT_TYPES:
            return SMMS_FAULT_TYPES[code], None
        if code not in DefectType._value2member_map_:
            return None, f"Unknown SMMS FAULT_TYPE {code!r}"
    if department == SOURCE_TDMS:
        if code in TDMS_CATEGORY_MAP:
            return TDMS_CATEGORY_MAP[code], None
        if code not in DefectType._value2member_map_:
            return None, f"Unknown TDMS defect_category {code!r}"
    return None, None


def _resolve_line(candidate: DefectCandidate) -> _Field:
    reported = _clean(candidate.reported_track_line)
    raw = candidate.track_line.value
    tokens: list[str] = []
    if reported:
        tokens.append(reported)
    if raw is not None and str(raw).strip() and str(raw).strip() not in tokens:
        tokens.append(str(raw).strip())
    if not tokens:
        return _Field(None, ValueOrigin.UNKNOWN, None, missing_name="track_line")
    resolved: list[tuple[str, ValueOrigin]] = []
    for token in tokens:
        line, origin, error = _canonicalize_line(token, candidate.department)
        if error:
            return _Field(None, ValueOrigin.UNKNOWN, reported or token, error=error)
        assert line is not None
        resolved.append((line, origin))
    lines = {item[0] for item in resolved}
    if len(lines) > 1:
        return _Field(
            None,
            ValueOrigin.UNKNOWN,
            reported,
            error="The reported line and the normalized line do not name the same corridor line.",
        )
    line, origin = resolved[-1]
    if isinstance(raw, str) and candidate.track_line.origin is not ValueOrigin.UNKNOWN:
        origin = candidate.track_line.origin
    return _Field(line, origin, reported or (str(raw) if raw is not None else None))


def _canonicalize_line(token: str, department: str | None) -> tuple[str | None, ValueOrigin, str | None]:
    original = token.strip()
    if department == SOURCE_TMS and original in TMS_LINE_CODES:
        return TMS_LINE_CODES[original], ValueOrigin.NORMALIZED, None
    compact = original.upper().replace(" ", "_").replace("-", "_")
    if compact in _LINE_CODES:
        origin = ValueOrigin.REPORTED if compact == original.strip().upper() else ValueOrigin.NORMALIZED
        return compact, origin, None
    if department == SOURCE_TMS and original.isdigit():
        return None, ValueOrigin.UNKNOWN, f"Unknown TMS LINE_ID {original!r}"
    return None, ValueOrigin.UNKNOWN, f"Line {original!r} is not a corridor line."


def _resolve_chainage(candidate: DefectCandidate) -> _Field:
    raw = candidate.chainage_km.value
    reported = None if raw is None else str(raw)
    if raw is None:
        return _Field(None, ValueOrigin.UNKNOWN, None, missing_name="chainage_km")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return _Field(None, ValueOrigin.UNKNOWN, str(raw), error=f"Invalid chainage {raw!r}.")
    chainage = float(raw)
    if chainage < topology.CORRIDOR_START_KM or chainage > topology.CORRIDOR_END_KM:
        return _Field(
            None,
            ValueOrigin.UNKNOWN,
            reported,
            error=(
                f"Chainage KM {chainage} is outside the corridor "
                f"[{topology.CORRIDOR_START_KM}, {topology.CORRIDOR_END_KM}]"
            ),
        )
    return _Field(chainage, candidate.chainage_km.origin, reported)


def _resolve_severity(candidate: DefectCandidate) -> _Field:
    reported_text = _clean(candidate.reported_severity_text)
    corpus = reported_text or candidate.excerpt
    explicit, explicit_text, explicit_error = _explicit_severity(candidate.severity, corpus)
    if explicit_error:
        return _Field(None, ValueOrigin.UNKNOWN, reported_text or explicit_text, error=explicit_error)
    if explicit is not None:
        origin = candidate.severity.origin if candidate.severity.value is not None else ValueOrigin.REPORTED
        if origin is ValueOrigin.UNKNOWN:
            origin = ValueOrigin.REPORTED
        return _Field(explicit, origin, reported_text or explicit_text)
    if candidate.department == SOURCE_TMS:
        return _tms_grade(corpus, reported_text)
    if candidate.department == SOURCE_SMMS:
        return _smms_band(corpus, reported_text)
    if candidate.department == SOURCE_TDMS:
        return _tdms_sag(corpus, reported_text)
    return _Field(None, ValueOrigin.UNKNOWN, reported_text, missing_name="severity")


def _explicit_severity(sourced: SourcedValue, corpus: str | None) -> tuple[int | None, str | None, str | None]:
    raw = sourced.value
    if raw is not None:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None, str(raw), f"Invalid severity {raw!r}."
        if isinstance(raw, float) and not raw.is_integer():
            return None, str(raw), f"Severity must be in [1, 100], got {raw}"
        number = int(raw)
        if number < 1 or number > 100:
            return None, str(number), f"Severity must be in [1, 100], got {number}"
        return number, str(number), None
    if not corpus:
        return None, None, None
    matches = _SEVERITY_RE.findall(corpus)
    if not matches:
        return None, None, None
    if len(set(matches)) > 1:
        return None, corpus, "The report states more than one severity number."
    number = int(matches[0])
    if number < 1 or number > 100:
        return None, matches[0], f"Severity must be in [1, 100], got {number}"
    return number, matches[0], None


def _tms_grade(corpus: str | None, reported_text: str | None) -> _Field:
    grades = _GRADE_RE.findall(corpus or "")
    if not grades:
        if reported_text:
            return _Field(
                None,
                ValueOrigin.UNKNOWN,
                reported_text,
                error="TMS severity must be an explicit severity or a grade from 1 to 10.",
            )
        return _Field(None, ValueOrigin.UNKNOWN, None, missing_name="severity")
    if len(set(grades)) > 1:
        return _Field(None, ValueOrigin.UNKNOWN, reported_text, error="The report states more than one TMS grade.")
    grade = int(grades[0])
    wording = reported_text or f"grade {grade}"
    if grade < 1 or grade > 10:
        return _Field(
            None,
            ValueOrigin.UNKNOWN,
            wording,
            error=f"GRADE must be in [1, 10] (legacy TMS scale), got {grade}",
        )
    return _Field(min(100, max(1, grade * 10)), ValueOrigin.NORMALIZED, wording)


def _smms_band(corpus: str | None, reported_text: str | None) -> _Field:
    bands = [item.upper() for item in _BAND_RE.findall(corpus or "")]
    if not bands:
        if reported_text:
            return _Field(
                None,
                ValueOrigin.UNKNOWN,
                reported_text,
                error="Unknown SMMS PRIORITY_BAND " + repr(reported_text.strip().upper()),
            )
        return _Field(None, ValueOrigin.UNKNOWN, None, missing_name="severity")
    distinct = set(bands)
    if len(distinct) > 1:
        return _Field(None, ValueOrigin.UNKNOWN, reported_text, error="The report states more than one SMMS priority band.")
    band = bands[0]
    if band not in SMMS_PRIORITY_SEVERITY:
        return _Field(None, ValueOrigin.UNKNOWN, reported_text or band, error=f"Unknown SMMS PRIORITY_BAND {band!r}")
    return _Field(SMMS_PRIORITY_SEVERITY[band], ValueOrigin.NORMALIZED, reported_text or band)


def _tdms_sag(corpus: str | None, reported_text: str | None) -> _Field:
    measurements = _SAG_RE.findall(corpus or "")
    if not measurements:
        if reported_text:
            return _Field(
                None,
                ValueOrigin.UNKNOWN,
                reported_text,
                error="TDMS severity must be an explicit severity or a positive sag measurement in millimetres.",
            )
        return _Field(None, ValueOrigin.UNKNOWN, None, missing_name="severity")
    if len(set(measurements)) > 1:
        return _Field(None, ValueOrigin.UNKNOWN, reported_text, error="The report states more than one sag measurement.")
    sag_mm = float(measurements[0])
    wording = reported_text or f"sag {measurements[0]} mm"
    if sag_mm <= 0:
        return _Field(None, ValueOrigin.UNKNOWN, wording, error="sag_measurement_mm must be positive")
    raw_score = round(sag_mm / TDMS_SAG_TO_SEVERITY_DIVISOR)
    return _Field(min(100, max(1, raw_score)), ValueOrigin.NORMALIZED, wording)


def _resolve_overdue(candidate: DefectCandidate, as_of: date | None) -> _Field:
    raw = candidate.days_overdue.value
    due_text = candidate.due_date.value
    due_reported = due_text if isinstance(due_text, str) and due_text.strip() else None
    if raw is not None:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return _Field(None, ValueOrigin.UNKNOWN, due_reported, error=f"Invalid OVERDUE_DAYS {raw!r}")
        if float(raw) < 0:
            return _Field(None, ValueOrigin.UNKNOWN, due_reported, error="OVERDUE_DAYS cannot be negative")
        origin = candidate.days_overdue.origin
        if origin is ValueOrigin.UNKNOWN:
            origin = ValueOrigin.REPORTED
        return _Field(float(raw), origin, due_reported)
    if candidate.department == SOURCE_SMMS or candidate.department not in (SOURCE_TMS, SOURCE_TDMS):
        return _Field(None, ValueOrigin.UNKNOWN, due_reported, missing_name="days_overdue")
    if as_of is None or due_reported is None:
        return _Field(None, ValueOrigin.UNKNOWN, due_reported, missing_name="days_overdue")
    try:
        due = _parse_ddmmyyyy(due_reported) if candidate.department == SOURCE_TMS else _parse_iso_date(due_reported)
    except ValueError as exc:
        return _Field(None, ValueOrigin.UNKNOWN, due_reported, error=str(exc))
    computed = max(0.0, float((as_of - due).days))
    return _Field(computed, ValueOrigin.NORMALIZED, due_reported)


def _resolve_tsr(candidate: DefectCandidate) -> _Field:
    raw = candidate.has_tsr.value
    if isinstance(raw, bool):
        origin = candidate.has_tsr.origin
        if origin is ValueOrigin.UNKNOWN:
            origin = ValueOrigin.REPORTED
        return _Field(raw, origin, None)
    if raw is None:
        return _Field(None, ValueOrigin.UNKNOWN, None, missing_name="has_tsr")
    return _Field(None, ValueOrigin.UNKNOWN, str(raw), error=f"Invalid TSR value {raw!r}.")


def _line_at_chainage(line: _Field, chainage: _Field) -> str | None:
    if line.error or chainage.error or line.missing_name or chainage.missing_name:
        return None
    if not isinstance(line.value, str) or not isinstance(chainage.value, float):
        return None
    available = topology.get_available_lines_at_km(chainage.value)
    if line.value not in available:
        return f"Line {line.value!r} is not present at KM {chainage.value}; available lines: {available}"
    return None


def _identity(
    defect_type: _Field,
    line: _Field,
    chainage: _Field,
    presence_error: str | None,
) -> tuple[DefectType, str, float] | None:
    if presence_error or defect_type.error or line.error or chainage.error:
        return None
    if not isinstance(defect_type.value, DefectType):
        return None
    if not isinstance(line.value, str) or not isinstance(chainage.value, float):
        return None
    return defect_type.value, line.value, chainage.value


def _ambiguous_reason(candidate: DefectCandidate) -> str | None:
    if candidate.extraction_method is FindingMethod.VISUAL:
        return "A photograph is an observation only. It does not prove that a railway asset is safe or defective."
    if candidate.uncertainty.value == "high" or classify_polarity(candidate.excerpt) == "uncertain":
        return "The finding is ambiguous, so it stays in preview until a reviewer confirms it."
    return None


def _notes_for(candidate: DefectCandidate, severity: _Field, overdue: _Field) -> list[str]:
    notes: list[str] = []
    if severity.origin is ValueOrigin.NORMALIZED and isinstance(severity.value, int):
        notes.append(
            f"Numeric severity {severity.value} was mapped from {severity.reported_text!r}. "
            "The original wording is unchanged."
        )
    elif severity.missing_name == "severity" and _unmapped_severity_wording(candidate):
        notes.append(
            "Severity wording was stated, but the source system was not identified, so no source map was applied."
        )
    if (
        overdue.origin is ValueOrigin.NORMALIZED
        and isinstance(overdue.value, float)
        and candidate.days_overdue.value is None
    ):
        notes.append(
            "Days overdue was computed from the stated due date and the supplied reference date "
            "using the TMS/TDMS date rule. A missing overdue value was not replaced with 0."
        )
    return notes


def _unmapped_severity_wording(candidate: DefectCandidate) -> bool:
    if candidate.department in (SOURCE_TMS, SOURCE_SMMS, SOURCE_TDMS):
        return False
    text = f"{candidate.reported_severity_text or ''}\n{candidate.excerpt}"
    return _GRADE_RE.search(text) is not None or _BAND_RE.search(text) is not None or _SAG_RE.search(text) is not None


def _duration_note(candidate: DefectCandidate, spec_minutes: float) -> str | None:
    raw = candidate.reported_duration_min.value
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if abs(float(raw) - float(spec_minutes)) <= 1e-6:
        return None
    return (
        "The reported duration stays on the evidence record. "
        "required_block_min comes from DEFECT_TYPE_SPEC."
    )


def _evidence_from_candidate(
    filename: str,
    candidate: DefectCandidate,
    fields: dict[str, _Field],
) -> PreviewEvidence:
    defect_type = fields["defect_type"]
    line = fields["track_line"]
    chainage = fields["chainage_km"]
    severity = fields["severity"]
    overdue = fields["days_overdue"]
    tsr = fields["has_tsr"]
    duration = _optional_float(candidate.reported_duration_min.value)
    return PreviewEvidence(
        filename=filename,
        references=_references(candidate),
        page_numbers=tuple(item.page_number for item in candidate.evidence),
        statement=candidate.statement,
        excerpt=candidate.excerpt,
        extraction_method=candidate.extraction_method,
        department=candidate.department,
        reported_defect_type=defect_type.reported_text,
        normalized_defect_type=defect_type.value.value if isinstance(defect_type.value, DefectType) else None,
        defect_type_origin=defect_type.origin if defect_type.value is not None else ValueOrigin.UNKNOWN,
        reported_severity_text=severity.reported_text or candidate.reported_severity_text,
        severity=severity.value if isinstance(severity.value, int) else None,
        severity_origin=severity.origin if isinstance(severity.value, int) else ValueOrigin.UNKNOWN,
        reported_track_line=line.reported_text,
        track_line=line.value if isinstance(line.value, str) else None,
        track_line_origin=line.origin if isinstance(line.value, str) else ValueOrigin.UNKNOWN,
        chainage_km=chainage.value if isinstance(chainage.value, float) else None,
        chainage_origin=chainage.origin if isinstance(chainage.value, float) else ValueOrigin.UNKNOWN,
        reported_due_date=overdue.reported_text if isinstance(overdue.reported_text, str) else _clean(candidate.due_date.value if isinstance(candidate.due_date.value, str) else None),
        days_overdue=overdue.value if isinstance(overdue.value, float) else None,
        days_overdue_origin=overdue.origin if isinstance(overdue.value, float) else ValueOrigin.UNKNOWN,
        has_tsr=tsr.value if isinstance(tsr.value, bool) else None,
        has_tsr_origin=tsr.origin if isinstance(tsr.value, bool) else ValueOrigin.UNKNOWN,
        reported_duration_min=duration,
        reported_asset_ref=candidate.reported_asset_ref,
    )


def _evidence_shell(filename: str, candidate: DefectCandidate) -> PreviewEvidence:
    return PreviewEvidence(
        filename=filename,
        references=_references(candidate),
        page_numbers=tuple(item.page_number for item in candidate.evidence),
        statement=candidate.statement,
        excerpt=candidate.excerpt,
        extraction_method=candidate.extraction_method,
        department=candidate.department,
        reported_defect_type=_clean(candidate.reported_defect_type),
        normalized_defect_type=_clean(candidate.normalized_defect_type.value if isinstance(candidate.normalized_defect_type.value, str) else None),
        defect_type_origin=ValueOrigin.UNKNOWN,
        reported_severity_text=_clean(candidate.reported_severity_text),
        severity=None,
        severity_origin=ValueOrigin.UNKNOWN,
        reported_track_line=_clean(candidate.reported_track_line),
        track_line=None,
        track_line_origin=ValueOrigin.UNKNOWN,
        chainage_km=None,
        chainage_origin=ValueOrigin.UNKNOWN,
        reported_due_date=_clean(candidate.due_date.value if isinstance(candidate.due_date.value, str) else None),
        days_overdue=None,
        days_overdue_origin=ValueOrigin.UNKNOWN,
        has_tsr=None,
        has_tsr_origin=ValueOrigin.UNKNOWN,
        reported_duration_min=_optional_float(candidate.reported_duration_min.value),
        reported_asset_ref=candidate.reported_asset_ref,
    )


def _references(candidate: DefectCandidate) -> tuple[str, ...]:
    return tuple(item.reference for item in candidate.evidence)


def _enum_type(token: str) -> DefectType | None:
    compact = token.strip().upper().replace(" ", "_")
    if compact in DefectType._value2member_map_:
        return DefectType(compact)
    return None


def _optional_float(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _clean(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def _with_source(reason: str, references: tuple[str, ...]) -> str:
    if not references:
        return reason
    return f"{reason} Source: {', '.join(references)}."


def _stable_id(report_id: str, filename: str, finding_id: str, references: tuple[str, ...]) -> str:
    material = "|".join((report_id, filename, finding_id, ",".join(references)))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"AI1-{digest}"


__all__ = [
    "CandidatePreview",
    "PreviewEvidence",
    "PreviewStatus",
    "ReportPreview",
    "same_problem_fields",
    "validate_candidates",
]
