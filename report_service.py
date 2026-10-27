"""
report_service.py
====================================================================
Preview a maintenance report, then apply selected records.

Preview does not change the open backlog. The default apply appends the
selected records to the persisted backlog. A later append can add remaining
ready candidates from the same stored preview. A candidate that already has
an evidence defect id is not inserted again. An explicit replace lists the
open rows it would remove and is refused while a possession or reserved
defect would be dropped, and it stays refused after the report has been
applied. The browser's copy of the preview is not the source of truth.
====================================================================
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Sequence

import backlog_state
import data_ingestion
import sim_clock
from defects import TrackDefect
from report_documents import (
    DocumentLimitError,
    DocumentLimits,
    DocumentReadError,
    UnsupportedFormatError,
    read_report_document,
)
from report_nlp import local_text_or_keep
from report_pipeline import simplify_report
from report_preview import PreviewStatus, validate_candidates
from report_provider import SCRIPTED_MOCK_LABEL, ProviderNotConfiguredError, build_report_provider, runtime_status
from report_test_fixtures import maybe_scripted_fixture
from report_schema import (
    DefectCandidate,
    Evidence,
    FindingMethod,
    ReportUnderstanding,
    ReviewStatus,
    RuledOutFinding,
    SourcedValue,
    Uncertainty,
    UnderstandingStatus,
    ValueOrigin,
)
from report_store import (
    STATUS_APPLIED,
    STATUS_PENDING,
    ReportEvidence,
    commit_session,
    evidence_for_report,
    load_preview,
    open_session,
    save_preview,
)
from sqlalchemy import select

APPEND_REPORT_DEFECTS: str = "append_report_defects"
REPLACE_OPEN_BACKLOG: str = "replace_open_backlog"
_APPLY_OPERATIONS = frozenset({APPEND_REPORT_DEFECTS, REPLACE_OPEN_BACKLOG})
_EDIT_FIELDS = frozenset(
    {
        "normalized_defect_type",
        "severity",
        "track_line",
        "chainage_km",
        "days_overdue",
        "has_tsr",
        "department",
    }
)
_READY_STATUSES = frozenset({PreviewStatus.READY_TO_APPLY, PreviewStatus.POSSIBLE_DUPLICATE})


class ReportRequestError(Exception):
    """A preview or apply request that must not change the backlog."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def safe_upload_name(filename: str | None) -> str:
    """Keep the file's own name and drop any directory the client sent."""
    if filename is None or not str(filename).strip():
        raise ReportRequestError(400, "FILENAME_REQUIRED", "Choose a report file. Nothing was added to the backlog.")
    name = Path(str(filename)).name
    if not name or name in {".", ".."}:
        raise ReportRequestError(400, "FILENAME_REQUIRED", "Choose a report file. Nothing was added to the backlog.")
    return name


def preview_uploaded_report(
    file_bytes: bytes,
    filename: str,
    *,
    provider: object | None = None,
    ocr: object | None = None,
    limits: DocumentLimits | None = None,
    possession_active: bool = False,
) -> dict[str, object]:
    """Read and validate one report. The open backlog is only read."""
    safe_name = safe_upload_name(filename)
    active_limits = limits if limits is not None else DocumentLimits()
    if len(file_bytes) > active_limits.max_bytes:
        raise ReportRequestError(
            413,
            "FILE_TOO_LARGE",
            f"The file is larger than {active_limits.max_bytes} bytes. Nothing was added to the backlog.",
        )
    try:
        bundle = read_report_document(file_bytes, safe_name, limits=active_limits, ocr=ocr)  # type: ignore[arg-type]
    except DocumentLimitError as exc:
        raise ReportRequestError(413, "FILE_LIMIT", f"{exc} Nothing was added to the backlog.") from exc
    except UnsupportedFormatError as exc:
        raise ReportRequestError(415, "UNSUPPORTED_FORMAT", f"{exc} Nothing was added to the backlog.") from exc
    except DocumentReadError as exc:
        raise ReportRequestError(400, "UNREADABLE_REPORT", f"{exc} Nothing was added to the backlog.") from exc
    _raise_if_ocr_unavailable(bundle.warnings)
    model = provider if provider is not None else build_report_provider()
    if provider is None:
        model = maybe_scripted_fixture(model, bundle)
        model = local_text_or_keep(model)
    understanding = simplify_report(bundle, model)  # type: ignore[arg-type]
    if understanding.status is UnderstandingStatus.FAILED:
        _raise_failed_understanding(understanding.error)
    existing = data_ingestion.get_effective_defects()
    validated = validate_candidates(understanding, existing_defects=existing)
    blocked, blocked_reason = replacement_block(possession_active)
    payload = _public_preview(understanding, validated, existing, blocked, blocked_reason)
    stored = save_preview(payload)
    if stored == STATUS_APPLIED:
        raise ReportRequestError(
            409,
            "REPORT_ALREADY_APPLIED",
            "This report was already applied. Upload was not stored again and the backlog was not changed.",
        )
    shown = dict(payload)
    shown["source_images"] = _public_images(bundle)
    return shown


def apply_stored_report(
    report_id: str,
    body: dict[str, object],
    *,
    possession_active: bool = False,
) -> dict[str, object]:
    """Append selected stored candidates, or replace only when that operation is explicit."""
    if not isinstance(body, dict):
        raise ReportRequestError(400, "INVALID_BODY", "The apply request must be a JSON object. Nothing was changed.")
    operation = body.get("operation")
    if operation not in _APPLY_OPERATIONS:
        raise ReportRequestError(
            400,
            "OPERATION_REQUIRED",
            "Set operation to append_report_defects or replace_open_backlog. Nothing was changed.",
        )
    raw_ids = body.get("candidate_ids")
    if not isinstance(raw_ids, list) or not raw_ids or not all(isinstance(item, str) and item.strip() for item in raw_ids):
        raise ReportRequestError(
            400,
            "NO_CANDIDATES",
            "Select at least one stored candidate. An empty selection is not written to the backlog.",
        )
    candidate_ids = list(dict.fromkeys(str(item) for item in raw_ids))
    edits = body.get("edits") or {}
    if not isinstance(edits, dict):
        raise ReportRequestError(400, "INVALID_EDITS", "edits must be an object keyed by candidate id. Nothing was changed.")
    unknown_edit_ids = [str(key) for key in edits if str(key) not in candidate_ids]
    if unknown_edit_ids:
        raise ReportRequestError(
            400,
            "UNKNOWN_CANDIDATE",
            "Edits were sent for a candidate that is not selected. Nothing was changed.",
        )
    for candidate_id in candidate_ids:
        _reject_unknown_edit_fields(edits.get(candidate_id))

    stored = load_preview(report_id)
    if stored is None:
        raise ReportRequestError(404, "REPORT_NOT_FOUND", "No stored preview exists for that report id. Nothing was changed.")
    if stored.get("stored_status") == STATUS_APPLIED and operation == REPLACE_OPEN_BACKLOG:
        raise ReportRequestError(
            409,
            "REPORT_ALREADY_APPLIED",
            "This report was already applied. Replacement of the open backlog remains blocked. Nothing was changed.",
        )

    linked_ids = {
        str(row.get("source_finding_id") or "")
        for row in evidence_for_report(report_id)
        if isinstance(row.get("defect_id"), str) and str(row.get("defect_id")).strip()
    }
    if linked_ids and all(candidate_id in linked_ids for candidate_id in candidate_ids):
        previous = stored.get("stored_result")
        return _already_applied(previous if isinstance(previous, dict) else {}, report_id)
    understanding = _understanding_from_storage(stored, candidate_ids, edits)
    if understanding is None:
        raise ReportRequestError(400, "UNKNOWN_CANDIDATE", "A selected candidate id is not in the stored preview. Nothing was changed.")
    validated = validate_candidates(understanding, existing_defects=data_ingestion.get_effective_defects())
    selected, problems = _selected_records(candidate_ids, validated.records, linked_ids)
    if problems:
        raise ReportRequestError(422, "CANDIDATE_NOT_READY", " ".join(problems) + " Nothing was changed.")
    if not selected:
        raise ReportRequestError(
            422,
            "NOTHING_TO_APPLY",
            "The report has no complete defect to apply. The backlog was not changed.",
        )
    if operation == REPLACE_OPEN_BACKLOG:
        blocked, blocked_reason = replacement_block(possession_active)
        if blocked:
            raise ReportRequestError(409, "POSSESSION_CONFLICT", blocked_reason or "Replacement is blocked. Nothing was changed.")

    new_defects = [item.scheduling_record for item in selected if item.scheduling_record is not None]
    if len(new_defects) != len(selected):
        raise ReportRequestError(422, "CANDIDATE_NOT_READY", "A selected candidate has no schedulable record. Nothing was changed.")
    defect_by_finding = {item.source_finding_id: item.scheduling_record.defect_id for item in selected if item.scheduling_record is not None}
    return _commit_replacement(report_id, new_defects, defect_by_finding, selected, possession_active, str(operation))


def replacement_block(possession_active: bool) -> tuple[bool, str | None]:
    """True when replacing the open backlog would drop reserved work."""
    reserved = sorted(backlog_state.scheduled_defect_ids())
    if not possession_active and not reserved:
        return False, None
    listed = ", ".join(reserved) if reserved else "the active possession"
    return True, (
        "A sanctioned possession or reserved defect blocks replacement of the open backlog. "
        f"Reserved work: {listed}. Nothing was changed."
    )


def _commit_replacement(
    report_id: str,
    _new_defects: list[TrackDefect],
    _defect_by_finding: dict[str, str],
    selected: Sequence[object],
    possession_active: bool,
    operation: str,
) -> dict[str, object]:
    backlog_state._ensure_loaded()
    already_result: dict[str, object] | None = None
    result: dict[str, object] | None = None
    staged: list[TrackDefect] = []
    with backlog_state._LOCK:
        if operation == REPLACE_OPEN_BACKLOG:
            blocked, blocked_reason = replacement_block(possession_active)
            if blocked:
                raise ReportRequestError(409, "POSSESSION_CONFLICT", blocked_reason or "Replacement is blocked. Nothing was changed.")
        session = open_session()
        try:
            from report_store import MaintenanceReport

            row = session.get(MaintenanceReport, report_id)
            if row is None:
                raise ReportRequestError(404, "REPORT_NOT_FOUND", "No stored preview exists for that report id. Nothing was changed.")
            if operation == REPLACE_OPEN_BACKLOG and row.status == STATUS_APPLIED:
                raise ReportRequestError(
                    409,
                    "REPORT_ALREADY_APPLIED",
                    "This report was already applied. Replacement of the open backlog remains blocked. Nothing was changed.",
                )
            current = list(backlog_state._ACTIVE or [])
            present = {item.defect_id for item in current}
            linked = _linked_finding_ids(session, report_id)
            if operation == APPEND_REPORT_DEFECTS:
                fresh: list[object] = []
                for item in selected:
                    scheduling = getattr(item, "scheduling_record")
                    finding_id = str(getattr(item, "source_finding_id"))
                    if scheduling is None or finding_id in linked or scheduling.defect_id in present:
                        continue
                    fresh.append(item)
                    present.add(scheduling.defect_id)
                if not fresh:
                    already_result = _stored_result(row.result_json)
                else:
                    staged = current + [getattr(item, "scheduling_record") for item in fresh]
                    result = _write_apply(
                        session,
                        row,
                        report_id,
                        operation,
                        staged,
                        [],
                        [item.defect_id for item in current],
                        fresh,
                    )
            else:
                staged = [getattr(item, "scheduling_record") for item in selected]
                result = _write_apply(
                    session,
                    row,
                    report_id,
                    operation,
                    staged,
                    [item.defect_id for item in current],
                    [],
                    list(selected),
                )
        except ReportRequestError:
            session.rollback()
            raise
        except Exception as exc:
            session.rollback()
            raise ReportRequestError(
                500,
                "APPLY_FAILED",
                "The report was not applied. The backlog was not changed.",
            ) from exc
        finally:
            session.close()
        if result is not None:
            backlog_state.install_open_backlog(staged)
    if already_result is not None:
        return _already_applied(already_result, report_id)
    if result is None:
        raise ReportRequestError(500, "APPLY_FAILED", "The report was not applied. The backlog was not changed.")
    data_ingestion.clear_last_ingest()
    result["evidence"] = evidence_for_report(report_id)
    return result


def reopen_stored_report(report_id: str) -> dict[str, object]:
    """Return the stored preview with applied ids marked. Nothing is written."""
    stored = load_preview(report_id)
    if stored is None:
        raise ReportRequestError(
            404,
            "REPORT_NOT_FOUND",
            "No stored preview exists for that report id. Nothing was changed.",
        )
    evidence = evidence_for_report(report_id)
    applied: dict[str, str] = {}
    for row in evidence:
        defect_id = row.get("defect_id")
        finding_id = str(row.get("source_finding_id") or "")
        if isinstance(defect_id, str) and defect_id.strip() and finding_id:
            applied.setdefault(finding_id, defect_id)
    candidates = stored.get("candidates")
    annotated: list[dict[str, object]] = []
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            item = dict(candidate)
            item["applied_defect_id"] = applied.get(str(item.get("candidate_id") or ""))
            annotated.append(item)
    stored["candidates"] = annotated
    stored["applied_candidate_ids"] = [
        str(item["candidate_id"]) for item in annotated if item.get("applied_defect_id")
    ]
    stored["audit_evidence"] = [row for row in evidence if not row.get("defect_id")]
    application = stored.get("application")
    if isinstance(application, dict):
        refreshed = dict(application)
        refreshed["keeps_open_defect_ids"] = [item.defect_id for item in data_ingestion.get_effective_defects()]
        stored["application"] = refreshed
    return stored


def _linked_finding_ids(session: object, report_id: str) -> dict[str, str]:
    linked: dict[str, str] = {}
    rows = session.scalars(select(ReportEvidence).where(ReportEvidence.report_id == report_id)).all()  # type: ignore[attr-defined]
    for evidence in rows:
        if evidence.defect_id:
            linked[str(evidence.source_finding_id)] = str(evidence.defect_id)
    return linked


def _stored_result(raw: str | None) -> dict[str, object]:
    if not raw:
        return {}
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        return parsed
    return {}


def _write_apply(
    session: object,
    row: object,
    report_id: str,
    operation: str,
    staged: list[TrackDefect],
    replaced_ids: list[str],
    kept_ids: list[str],
    chosen: Sequence[object],
) -> dict[str, object]:
    new_defects = [getattr(item, "scheduling_record") for item in chosen]
    defect_by_finding = {
        str(getattr(item, "source_finding_id")): getattr(item, "scheduling_record").defect_id for item in chosen
    }
    backlog_state.stage_open_backlog(session, staged)  # type: ignore[arg-type]
    for evidence in session.scalars(select(ReportEvidence).where(ReportEvidence.report_id == report_id)).all():  # type: ignore[attr-defined]
        linked = defect_by_finding.get(evidence.source_finding_id)
        if linked is None:
            continue
        evidence.defect_id = linked
        record = _record_by_id(chosen, evidence.source_finding_id)
        if record is not None:
            _copy_applied_evidence(evidence, record)
    response = _applied_result(report_id, operation, replaced_ids, kept_ids, new_defects)
    stored = _cumulative_result(getattr(row, "result_json"), response)
    row.status = STATUS_APPLIED  # type: ignore[attr-defined]
    row.applied_at = sim_clock.now_datetime()  # type: ignore[attr-defined]
    row.result_json = json.dumps(stored)  # type: ignore[attr-defined]
    commit_session(session)  # type: ignore[arg-type]
    return response


def _cumulative_result(previous_json: str | None, response: dict[str, object]) -> dict[str, object]:
    previous_rows: list[dict[str, object]] = []
    if previous_json:
        parsed = json.loads(previous_json)
        raw_rows = parsed.get("applied_defects") if isinstance(parsed, dict) else None
        if isinstance(raw_rows, list):
            previous_rows = [item for item in raw_rows if isinstance(item, dict)]
    current_rows = response.get("applied_defects")
    merged = list(previous_rows)
    seen = {item.get("defect_id") for item in previous_rows}
    if isinstance(current_rows, list):
        for item in current_rows:
            if isinstance(item, dict) and item.get("defect_id") not in seen:
                merged.append(item)
                seen.add(item.get("defect_id"))
    stored = dict(response)
    stored["applied_defects"] = merged
    return stored


def _already_applied(previous: dict[str, object], report_id: str) -> dict[str, object]:
    repeat = dict(previous)
    repeat["status"] = "ALREADY_APPLIED"
    repeat["report_id"] = report_id
    repeat["message"] = "This report was already applied. The backlog was not changed again."
    evidence = evidence_for_report(report_id)
    repeat["evidence"] = evidence
    applied = repeat.get("applied_defects")
    if not isinstance(applied, list) or not applied:
        seen: set[str] = set()
        rows: list[dict[str, object]] = []
        for item in evidence:
            defect_id = item.get("defect_id")
            if isinstance(defect_id, str) and defect_id and defect_id not in seen:
                seen.add(defect_id)
                rows.append({"defect_id": defect_id})
        repeat["applied_defects"] = rows
    return repeat


def _applied_result(
    report_id: str,
    operation: str,
    replaced_ids: list[str],
    kept_ids: list[str],
    new_defects: Sequence[TrackDefect],
) -> dict[str, object]:
    if operation == APPEND_REPORT_DEFECTS:
        message = "The selected report defects were added to the open backlog. Earlier records and completed history were kept."
    else:
        message = "The open backlog was replaced with the selected report defects. Completed history was kept."
    return {
        "status": "APPLIED",
        "report_id": report_id,
        "operation": operation,
        "message": message,
        "replaced_defect_ids": replaced_ids,
        "kept_defect_ids": kept_ids,
        "applied_defects": [_defect_public(item) for item in new_defects],
        "completed_history_preserved": True,
    }


def _copy_applied_evidence(row: ReportEvidence, record: object) -> None:
    evidence = getattr(record, "evidence")
    scheduling = getattr(record, "scheduling_record")
    row.preview_status = getattr(record, "status").value
    row.normalized_defect_type = evidence.normalized_defect_type
    row.severity = evidence.severity
    row.track_line = evidence.track_line
    row.chainage_km = evidence.chainage_km
    row.days_overdue = evidence.days_overdue
    row.has_tsr = evidence.has_tsr
    row.department = evidence.department
    row.reported_severity_text = evidence.reported_severity_text
    if scheduling is not None:
        row.defect_id = scheduling.defect_id


def _record_by_id(selected: Sequence[object], finding_id: str) -> object | None:
    for record in selected:
        if getattr(record, "source_finding_id") == finding_id:
            return record
    return None


def _selected_records(
    candidate_ids: list[str],
    records: Sequence[object],
    linked_ids: set[str] | None = None,
) -> tuple[list[object], list[str]]:
    already = linked_ids or set()
    by_id = {getattr(record, "source_finding_id"): record for record in records}
    chosen: list[object] = []
    problems: list[str] = []
    for candidate_id in candidate_ids:
        if candidate_id in already:
            continue
        record = by_id.get(candidate_id)
        if record is None or getattr(record, "status") is PreviewStatus.RULED_OUT:
            problems.append(f"Candidate {candidate_id} is not an open finding in the stored preview.")
            continue
        status = getattr(record, "status")
        scheduling = getattr(record, "scheduling_record")
        if scheduling is None or status not in _READY_STATUSES:
            missing = ", ".join(getattr(record, "missing_fields")) or "validation"
            reasons = " ".join(getattr(record, "reasons"))
            problems.append(f"Candidate {candidate_id} is {status.value} ({missing}). {reasons}".strip())
            continue
        chosen.append(record)
    return chosen, problems


def _understanding_from_storage(
    stored: dict[str, object],
    candidate_ids: list[str],
    edits: dict[object, object],
) -> ReportUnderstanding | None:
    raw_candidates = stored.get("candidates")
    if not isinstance(raw_candidates, list):
        return None
    known = {str(item.get("candidate_id")) for item in raw_candidates if isinstance(item, dict)}
    if any(candidate_id not in known for candidate_id in candidate_ids):
        return None
    rebuilt: list[DefectCandidate] = []
    for item in raw_candidates:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id") or "")
        fields = item.get("fields")
        if not isinstance(fields, dict):
            continue
        if candidate_id in candidate_ids:
            fields = _with_edits(fields, edits.get(candidate_id))
        rebuilt.append(_candidate_from_fields(candidate_id, fields))
    ruled = _ruled_from_storage(stored.get("ruled_out"), str(stored.get("filename") or ""))
    return ReportUnderstanding(
        report_id=str(stored["report_id"]),
        filename=str(stored.get("filename") or ""),
        summary=str(stored.get("summary") or ""),
        candidates=tuple(rebuilt),
        ruled_out=ruled,
        warnings=(),
        status=UnderstandingStatus.COMPLETE,
        provider_name="stored-preview",
        chunks_processed=1,
        error=None,
        nlp_status="pending",
    )


def _with_edits(fields: dict[str, object], raw_edit: object) -> dict[str, object]:
    if raw_edit is None:
        return dict(fields)
    if not isinstance(raw_edit, dict):
        raise ReportRequestError(400, "INVALID_EDITS", "Each edit must be an object. Nothing was changed.")
    updated = dict(fields)
    for key, value in raw_edit.items():
        name = str(key)
        _check_edit_value(name, value)
        if name == "department":
            updated["department"] = value if isinstance(value, str) else None
            continue
        if name == "normalized_defect_type":
            updated["normalized_defect_type"] = _edited_source(value)
            updated["reported_defect_type"] = value if isinstance(value, str) else updated.get("reported_defect_type")
            continue
        updated[name] = _edited_source(value)
    return updated


def _edited_source(value: object) -> dict[str, object]:
    if value is None:
        return {"value": None, "origin": ValueOrigin.UNKNOWN.value}
    return {"value": value, "origin": ValueOrigin.REPORTED.value}


def _check_edit_value(name: str, value: object) -> None:
    if name not in _EDIT_FIELDS:
        raise ReportRequestError(400, "UNKNOWN_EDIT", f"Unknown edit field {name}. Nothing was changed.")
    if value is None:
        return
    if name in {"severity", "chainage_km", "days_overdue"} and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise ReportRequestError(400, "INVALID_EDIT", f"{name} must be a number. Nothing was changed.")
    if name == "has_tsr" and not isinstance(value, bool):
        raise ReportRequestError(400, "INVALID_EDIT", "has_tsr must be true or false. Nothing was changed.")
    if name in {"track_line", "normalized_defect_type", "department"} and not isinstance(value, str):
        raise ReportRequestError(400, "INVALID_EDIT", f"{name} must be text. Nothing was changed.")


def _reject_unknown_edit_fields(raw_edit: object) -> None:
    if raw_edit is None:
        return
    if not isinstance(raw_edit, dict):
        raise ReportRequestError(400, "INVALID_EDITS", "Each edit must be an object. Nothing was changed.")
    for key, value in raw_edit.items():
        _check_edit_value(str(key), value)


def _candidate_from_fields(candidate_id: str, fields: dict[str, object]) -> DefectCandidate:
    evidence_rows = fields.get("evidence")
    evidence: list[Evidence] = []
    if isinstance(evidence_rows, list):
        for index, item in enumerate(evidence_rows, start=1):
            if not isinstance(item, dict):
                continue
            page = item.get("page_number")
            method_name = str(item.get("extraction_method") or fields.get("extraction_method") or FindingMethod.NATIVE_TEXT.value)
            evidence.append(
                Evidence(
                    evidence_id=f"{candidate_id}-E{index}",
                    filename=str(item.get("filename") or ""),
                    reference=str(item.get("reference") or ""),
                    page_number=page if isinstance(page, int) else None,
                    excerpt=str(fields.get("excerpt") or ""),
                    extraction_method=FindingMethod(method_name),
                )
            )
    review_name = str(fields.get("review_status") or ReviewStatus.NEEDS_REVIEW.value)
    if review_name not in {ReviewStatus.NEEDS_REVIEW.value, ReviewStatus.READY.value}:
        review_name = ReviewStatus.NEEDS_REVIEW.value
    missing = fields.get("missing_fields")
    warnings = fields.get("validation_warnings")
    return DefectCandidate(
        candidate_id=candidate_id,
        statement=str(fields.get("statement") or ""),
        excerpt=str(fields.get("excerpt") or ""),
        evidence=tuple(evidence),
        department=_text(fields.get("department")),
        reported_defect_type=_text(fields.get("reported_defect_type")),
        normalized_defect_type=_sourced(fields.get("normalized_defect_type")),
        reported_severity_text=_text(fields.get("reported_severity_text")),
        severity=_sourced(fields.get("severity")),
        reported_track_line=_text(fields.get("reported_track_line")),
        track_line=_sourced(fields.get("track_line")),
        chainage_km=_sourced(fields.get("chainage_km")),
        due_date=_sourced(fields.get("due_date")),
        days_overdue=_sourced(fields.get("days_overdue")),
        has_tsr=_sourced(fields.get("has_tsr")),
        tsr_speed_kmh=None,
        reported_duration_min=_sourced(fields.get("reported_duration_min")),
        reported_asset_ref=_text(fields.get("reported_asset_ref")),
        extraction_method=FindingMethod(str(fields.get("extraction_method") or FindingMethod.NATIVE_TEXT.value)),
        missing_fields=tuple(str(item) for item in missing) if isinstance(missing, list) else (),
        uncertainty=Uncertainty(str(fields.get("uncertainty") or Uncertainty.LOW.value)),
        validation_warnings=tuple(str(item) for item in warnings) if isinstance(warnings, list) else (),
        review_status=ReviewStatus(review_name),
    )


def _ruled_from_storage(raw: object, filename: str) -> tuple[RuledOutFinding, ...]:
    if not isinstance(raw, list):
        return ()
    findings: list[RuledOutFinding] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        references = item.get("references")
        pages = item.get("page_numbers")
        evidence: list[Evidence] = []
        if isinstance(references, list):
            for index, reference in enumerate(references):
                page = pages[index] if isinstance(pages, list) and index < len(pages) else None
                evidence.append(
                    Evidence(
                        evidence_id=f"{item.get('finding_id')}-E{index + 1}",
                        filename=filename,
                        reference=str(reference),
                        page_number=page if isinstance(page, int) else None,
                        excerpt=str(item.get("excerpt") or ""),
                        extraction_method=FindingMethod(str(item.get("extraction_method") or FindingMethod.NATIVE_TEXT.value)),
                    )
                )
        findings.append(
            RuledOutFinding(
                finding_id=str(item.get("finding_id") or ""),
                statement=str(item.get("statement") or ""),
                excerpt=str(item.get("excerpt") or ""),
                evidence=tuple(evidence),
                reason=str(item.get("reason") or ""),
                extraction_method=FindingMethod(str(item.get("extraction_method") or FindingMethod.NATIVE_TEXT.value)),
            )
        )
    return tuple(findings)


def _public_images(bundle: object) -> list[dict[str, object]]:
    """Real extracted images for the reviewer. Paths and package names stay off the response."""
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for section in getattr(bundle, "sections", ()):
        for image in getattr(section, "images", ()):
            reference = str(getattr(image, "reference", "") or "")
            media_type = str(getattr(image, "media_type", "") or "").split(";", 1)[0].strip().lower()
            data = getattr(image, "data", b"")
            if not reference or reference in seen:
                continue
            if not media_type.startswith("image/") or not isinstance(data, (bytes, bytearray)) or not bytes(data):
                continue
            seen.add(reference)
            page = getattr(image, "page_number", None)
            encoded = base64.b64encode(bytes(data)).decode("ascii")
            rows.append(
                {
                    "reference": reference,
                    "page_number": page if isinstance(page, int) else None,
                    "media_type": media_type,
                    "data_url": f"data:{media_type};base64,{encoded}",
                }
            )
    return rows


def _public_preview(
    understanding: ReportUnderstanding,
    validated: object,
    existing: Sequence[TrackDefect],
    blocked: bool,
    blocked_reason: str | None,
) -> dict[str, object]:
    records = list(getattr(validated, "records"))
    candidates = [item for item in records if item.status is not PreviewStatus.RULED_OUT]
    ruled = [item for item in records if item.status is PreviewStatus.RULED_OUT]
    by_candidate = {item.candidate_id: item for item in understanding.candidates}
    provider_view = runtime_status()
    provider_view["scripted"] = False
    fixture_id = ""
    if understanding.provider_name.startswith("mock:"):
        fixture_id = understanding.provider_name.split(":", 1)[1].strip()
    if fixture_id:
        provider_view["provider"] = "mock"
        provider_view["live"] = False
        provider_view["model"] = None
        provider_view["scripted"] = True
        provider_view["fixture"] = fixture_id
        provider_view["label"] = SCRIPTED_MOCK_LABEL
    else:
        provider_view["label"] = None
        provider_view["fixture"] = None
    return {
        "report_id": understanding.report_id,
        "filename": understanding.filename,
        "summary": understanding.summary,
        "understanding_status": understanding.status.value,
        "provider_name": understanding.provider_name,
        "warnings": [{"code": item.code, "message": item.message, "reference": item.reference} for item in understanding.warnings],
        "candidates": [_public_candidate(item, by_candidate.get(item.source_finding_id)) for item in candidates],
        "ruled_out": [_public_ruled(item) for item in ruled],
        "provider": provider_view,
        "application": {
            "operation": APPEND_REPORT_DEFECTS,
            "keeps_open_defect_ids": [item.defect_id for item in existing],
            "blocked": False,
            "blocked_reason": None,
        },
        "replacement": {
            "operation": REPLACE_OPEN_BACKLOG,
            "eligible_open_defect_ids": [item.defect_id for item in existing],
            "blocked": blocked,
            "blocked_reason": blocked_reason,
        },
    }


def _public_candidate(record: object, source: DefectCandidate | None) -> dict[str, object]:
    evidence = getattr(record, "evidence")
    scheduling = getattr(record, "scheduling_record")
    fields = _fields_from_source(source) if source is not None else _fields_from_preview(record)
    return {
        "candidate_id": getattr(record, "source_finding_id"),
        "preview_id": getattr(record, "preview_id"),
        "status": getattr(record, "status").value,
        "missing_fields": list(getattr(record, "missing_fields")),
        "reasons": list(getattr(record, "reasons")),
        "notes": list(getattr(record, "notes")),
        "duplicate_of": list(getattr(record, "duplicate_of")),
        "evidence": {
            "references": list(evidence.references),
            "page_numbers": list(evidence.page_numbers),
            "excerpt": evidence.excerpt,
            "statement": evidence.statement,
            "extraction_method": evidence.extraction_method.value,
            "reported_defect_type": evidence.reported_defect_type,
            "normalized_defect_type": evidence.normalized_defect_type,
            "reported_severity_text": evidence.reported_severity_text,
            "severity": evidence.severity,
            "severity_origin": evidence.severity_origin.value,
            "reported_track_line": evidence.reported_track_line,
            "track_line": evidence.track_line,
            "track_line_origin": evidence.track_line_origin.value,
            "chainage_km": evidence.chainage_km,
            "days_overdue": evidence.days_overdue,
            "days_overdue_origin": evidence.days_overdue_origin.value,
            "has_tsr": evidence.has_tsr,
            "has_tsr_origin": evidence.has_tsr_origin.value,
            "department": evidence.department,
        },
        "scheduling_preview": None if scheduling is None else _defect_public(scheduling),
        "fields": fields,
    }


def _public_ruled(record: object) -> dict[str, object]:
    evidence = getattr(record, "evidence")
    return {
        "finding_id": getattr(record, "source_finding_id"),
        "status": PreviewStatus.RULED_OUT.value,
        "reason": getattr(record, "reasons")[0] if getattr(record, "reasons") else "",
        "statement": evidence.statement,
        "excerpt": evidence.excerpt,
        "references": list(evidence.references),
        "page_numbers": list(evidence.page_numbers),
        "extraction_method": evidence.extraction_method.value,
    }


def _fields_from_source(candidate: DefectCandidate) -> dict[str, object]:
    return {
        "statement": candidate.statement,
        "excerpt": candidate.excerpt,
        "department": candidate.department,
        "reported_defect_type": candidate.reported_defect_type,
        "normalized_defect_type": _source_dict(candidate.normalized_defect_type),
        "reported_severity_text": candidate.reported_severity_text,
        "severity": _source_dict(candidate.severity),
        "reported_track_line": candidate.reported_track_line,
        "track_line": _source_dict(candidate.track_line),
        "chainage_km": _source_dict(candidate.chainage_km),
        "due_date": _source_dict(candidate.due_date),
        "days_overdue": _source_dict(candidate.days_overdue),
        "has_tsr": _source_dict(candidate.has_tsr),
        "reported_duration_min": _source_dict(candidate.reported_duration_min),
        "tsr_speed_kmh": candidate.tsr_speed_kmh,
        "reported_asset_ref": candidate.reported_asset_ref,
        "extraction_method": candidate.extraction_method.value,
        "uncertainty": candidate.uncertainty.value,
        "validation_warnings": list(candidate.validation_warnings),
        "review_status": candidate.review_status.value,
        "missing_fields": list(candidate.missing_fields),
        "evidence": [
            {
                "filename": item.filename,
                "reference": item.reference,
                "page_number": item.page_number,
                "excerpt": item.excerpt,
                "extraction_method": item.extraction_method.value,
            }
            for item in candidate.evidence
        ],
    }


def _fields_from_preview(record: object) -> dict[str, object]:
    evidence = getattr(record, "evidence")
    return {
        "statement": evidence.statement,
        "excerpt": evidence.excerpt,
        "department": evidence.department,
        "reported_defect_type": evidence.reported_defect_type,
        "normalized_defect_type": {"value": evidence.normalized_defect_type, "origin": evidence.defect_type_origin.value},
        "reported_severity_text": evidence.reported_severity_text,
        "severity": {"value": evidence.severity, "origin": evidence.severity_origin.value},
        "reported_track_line": evidence.reported_track_line,
        "track_line": {"value": evidence.track_line, "origin": evidence.track_line_origin.value},
        "chainage_km": {"value": evidence.chainage_km, "origin": evidence.chainage_origin.value},
        "due_date": {"value": evidence.reported_due_date, "origin": ValueOrigin.REPORTED.value},
        "days_overdue": {"value": evidence.days_overdue, "origin": evidence.days_overdue_origin.value},
        "has_tsr": {"value": evidence.has_tsr, "origin": evidence.has_tsr_origin.value},
        "reported_duration_min": {"value": evidence.reported_duration_min, "origin": ValueOrigin.UNKNOWN.value},
        "reported_asset_ref": None,
        "extraction_method": evidence.extraction_method.value,
        "uncertainty": Uncertainty.LOW.value,
        "validation_warnings": [],
        "review_status": ReviewStatus.NEEDS_REVIEW.value,
        "missing_fields": list(getattr(record, "missing_fields")),
        "evidence": [
            {
                "filename": evidence.filename,
                "reference": reference,
                "page_number": evidence.page_numbers[index] if index < len(evidence.page_numbers) else None,
                "excerpt": evidence.excerpt,
                "extraction_method": evidence.extraction_method.value,
            }
            for index, reference in enumerate(evidence.references)
        ],
    }


def _source_dict(value: SourcedValue) -> dict[str, object]:
    return {"value": value.value, "origin": value.origin.value}


def _sourced(value: object) -> SourcedValue:
    if not isinstance(value, dict):
        return SourcedValue(value=None, origin=ValueOrigin.UNKNOWN)
    raw = value.get("value")
    origin_name = str(value.get("origin") or ValueOrigin.UNKNOWN.value)
    if origin_name not in {item.value for item in ValueOrigin}:
        origin_name = ValueOrigin.UNKNOWN.value
    if raw is None:
        return SourcedValue(value=None, origin=ValueOrigin.UNKNOWN)
    return SourcedValue(value=raw, origin=ValueOrigin(origin_name))  # type: ignore[arg-type]


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _defect_public(defect: TrackDefect) -> dict[str, object]:
    return {
        "defect_id": defect.defect_id,
        "section_id": defect.section_id,
        "km_location": defect.km_location,
        "track_line": defect.track_line,
        "defect_type": defect.defect_type.value,
        "severity": defect.severity,
        "required_block_min": defect.required_block_min,
        "required_machine": defect.required_machine.value,
        "block_type": defect.block_type.value,
        "power_sector_id": defect.power_sector_id,
        "is_mandatory": defect.is_mandatory,
        "days_overdue": defect.days_overdue,
        "has_tsr": defect.has_tsr,
    }


def _raise_if_ocr_unavailable(warnings: Sequence[object]) -> None:
    unread = [item for item in warnings if getattr(item, "code", "") == "OCR_UNAVAILABLE"]
    if not unread:
        return
    refs = ", ".join(str(getattr(item, "reference", None) or f"page {getattr(item, 'page_number', '?')}") for item in unread)
    raise ReportRequestError(
        503,
        "OCR_UNAVAILABLE",
        "OCR is not available for "
        f"{refs}. Install Tesseract and pytesseract, or upload a digital PDF with selectable text. "
        "Nothing was added to the backlog.",
    )


def _raise_failed_understanding(error: str | None) -> None:
    message = error or "The report could not be understood."
    lowered = message.casefold()
    if "timed out" in lowered or "not configured" in lowered or "unavailable" in lowered:
        raise ReportRequestError(503, "PROVIDER_UNAVAILABLE", f"{message} Nothing was added to the backlog.")
    raise ReportRequestError(422, "REPORT_NOT_UNDERSTOOD", f"{message} Nothing was added to the backlog.")


__all__ = [
    "APPEND_REPORT_DEFECTS",
    "REPLACE_OPEN_BACKLOG",
    "ReportRequestError",
    "apply_stored_report",
    "preview_uploaded_report",
    "reopen_stored_report",
    "replacement_block",
    "safe_upload_name",
]
