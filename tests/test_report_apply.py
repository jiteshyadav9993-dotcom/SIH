"""Preview stays read-only. Apply replaces the open backlog only after a commit."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import app as app_module
import backlog_state
import data_ingestion
import sim_clock
from backlog_state import ArchivedSyntheticDefect, BacklogSnapshot
from data_ingestion import IngestSummary, SourceIngestResult
from defects import SOURCE_SMMS, SOURCE_TDMS, SOURCE_TMS, DefectType, _make_defect
from persistence import _SessionLocal
from report_documents import DocumentLimits
from report_provider import ProviderResponse, ProviderTimeoutError
from report_service import ReportRequestError, apply_stored_report, preview_uploaded_report
from report_store import MaintenanceReport, ReportEvidence, evidence_for_report, load_preview
from tests.test_report_documents import _jpeg_bytes, _scanned_pdf, _text_pdf

_READY = "Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR."
_SECOND = "Rail fracture at KM 40.0 on the UP fast line, severity 92, 18 days overdue, TSR imposed."
_INCOMPLETE = "Weld failure at KM 80.0 on the DN line."


class _ScriptedProvider:
    name = "mock"
    supports_images = False

    def __init__(self, rows: list[dict[str, object]] | Exception) -> None:
        self.rows = rows

    def complete(self, request: object) -> ProviderResponse:
        if isinstance(self.rows, Exception):
            raise self.rows
        prompt = request.user_prompt  # type: ignore[attr-defined]
        found = [row for row in self.rows if str(row["excerpt"]) in prompt]
        return ProviderResponse(raw_text=json.dumps({"summary": "Checked report.", "findings": found}))


def _finding(excerpt: str, reference: str = "page:1") -> dict[str, object]:
    return {
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


def _open_ids() -> list[str]:
    return [item.defect_id for item in data_ingestion.get_effective_defects()]


def _snapshot_json() -> str | None:
    session = _SessionLocal()
    try:
        row = session.get(BacklogSnapshot, 1)
        return None if row is None else row.defects_json
    finally:
        session.close()


def _completed_ids() -> list[str]:
    return [str(item["defect_id"]) for item in backlog_state.get_completed_log(1000)]


_REPORT_FIELDS: tuple[str, ...] = (
    "filename",
    "status",
    "summary",
    "preview_json",
    "created_at",
    "applied_at",
    "result_json",
)
_EVIDENCE_FIELDS: tuple[str, ...] = (
    "source_finding_id",
    "defect_id",
    "filename",
    "reference",
    "page_number",
    "excerpt",
    "statement",
    "extraction_method",
    "reported_defect_type",
    "normalized_defect_type",
    "reported_severity_text",
    "severity",
    "reported_track_line",
    "track_line",
    "chainage_km",
    "reported_due_date",
    "days_overdue",
    "has_tsr",
    "department",
    "preview_status",
)


def _capture_report_rows(session: Session) -> dict[str, dict[str, object]]:
    """Copy report rows that already exist so a test can put them back."""
    captured: dict[str, dict[str, object]] = {}
    for row in session.scalars(select(MaintenanceReport)).all():
        evidence = session.scalars(
            select(ReportEvidence).where(ReportEvidence.report_id == row.report_id)
        ).all()
        captured[row.report_id] = {
            "report": {name: getattr(row, name) for name in _REPORT_FIELDS},
            "evidence": [{name: getattr(item, name) for name in _EVIDENCE_FIELDS} for item in evidence],
        }
    return captured


def _restore_report_rows(session: Session, captured: dict[str, dict[str, object]]) -> None:
    """Put snapshotted report rows and their evidence back. New rows stay untouched."""
    for report_id, item in captured.items():
        fields = item["report"]
        assert isinstance(fields, dict)
        row = session.get(MaintenanceReport, report_id)
        if row is None:
            row = MaintenanceReport(report_id=report_id, **fields)
            session.add(row)
        else:
            for name, value in fields.items():
                setattr(row, name, value)
        for evidence in session.scalars(
            select(ReportEvidence).where(ReportEvidence.report_id == report_id)
        ).all():
            session.delete(evidence)
        session.flush()
        evidence_rows = item["evidence"]
        assert isinstance(evidence_rows, list)
        for evidence_fields in evidence_rows:
            assert isinstance(evidence_fields, dict)
            session.add(ReportEvidence(report_id=report_id, **evidence_fields))


@pytest.fixture
def workspace() -> Iterator[dict[str, object]]:
    backlog_state._ensure_loaded()
    session = _SessionLocal()
    try:
        row = session.get(BacklogSnapshot, 1)
        saved_json = row.defects_json if row is not None else None
        saved_cycle = row.cycle_number if row is not None else 0
        existing_report_rows = _capture_report_rows(session)
        existing_archives = set(session.scalars(select(ArchivedSyntheticDefect.id)).all())
    finally:
        session.close()
    saved = {
        "json": saved_json,
        "cycle": saved_cycle,
        "active": list(backlog_state._ACTIVE or []),
        "scheduled": set(backlog_state._SCHEDULED_IDS),
        "summary": data_ingestion._LAST_SUMMARY,
        "sanctioned": app_module.SYSTEM_STATE.get("sanctioned"),
        "possession": app_module.SYSTEM_STATE.get("possession"),
        "reports": set(existing_report_rows),
        "report_rows": existing_report_rows,
        "archives": existing_archives,
    }
    try:
        yield saved
    finally:
        app_module.SYSTEM_STATE["sanctioned"] = saved["sanctioned"]
        app_module.SYSTEM_STATE["possession"] = saved["possession"]
        data_ingestion._LAST_SUMMARY = saved["summary"]
        backlog_state._ACTIVE = list(saved["active"])  # type: ignore[arg-type]
        backlog_state._CYCLE = int(saved["cycle"])  # type: ignore[arg-type]
        backlog_state._SCHEDULED_IDS.clear()
        backlog_state._SCHEDULED_IDS.update(saved["scheduled"])  # type: ignore[arg-type]
        session = _SessionLocal()
        try:
            row = session.get(BacklogSnapshot, 1)
            if row is not None and isinstance(saved["json"], str):
                row.defects_json = saved["json"]
                row.cycle_number = int(saved["cycle"])  # type: ignore[arg-type]
            fresh_reports = set(session.scalars(select(MaintenanceReport.report_id)).all())
            for report_id in fresh_reports - saved["reports"]:  # type: ignore[operator]
                for evidence in session.scalars(select(ReportEvidence).where(ReportEvidence.report_id == report_id)).all():
                    session.delete(evidence)
                report = session.get(MaintenanceReport, report_id)
                if report is not None:
                    session.delete(report)
            session.flush()
            _restore_report_rows(session, saved["report_rows"])  # type: ignore[arg-type]
            for archive in session.scalars(select(ArchivedSyntheticDefect)).all():
                if archive.id not in saved["archives"]:  # type: ignore[operator]
                    session.delete(archive)
            marker = session.scalars(
                select(backlog_state.CompletedMaintenance).where(
                    backlog_state.CompletedMaintenance.defect_id == "STAGE4-HISTORY"
                )
            ).first()
            if marker is not None:
                session.delete(marker)
                backlog_state._COMPLETED_IDS.discard("STAGE4-HISTORY")
            session.commit()
        finally:
            session.close()


def _clear_possession_flags() -> None:
    app_module.SYSTEM_STATE["sanctioned"] = False
    app_module.SYSTEM_STATE["possession"] = None
    backlog_state.release_defects(list(backlog_state.scheduled_defect_ids()))


def _preview(text: str, provider: _ScriptedProvider, **kwargs: object) -> dict[str, object]:
    return preview_uploaded_report(
        _text_pdf([text]),
        "inspection.pdf",
        provider=provider,
        possession_active=bool(kwargs.get("possession_active", False)),
    )


def test_preview_is_read_only_and_returns_candidates(workspace: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_possession_flags()
    before_ids = _open_ids()
    before_snapshot = _snapshot_json()
    before_completed = _completed_ids()
    monkeypatch.setattr(data_ingestion, "get_effective_defects", lambda: ())
    preview = _preview(_READY, _ScriptedProvider([_finding(_READY)]))

    assert preview["report_id"]
    assert preview["filename"] == "inspection.pdf"
    assert preview["summary"]
    assert preview["candidates"]
    candidate = preview["candidates"][0]
    assert candidate["status"] == "READY_TO_APPLY"
    assert candidate["missing_fields"] == []
    assert candidate["evidence"]["references"] == ["page:1"]
    assert candidate["evidence"]["excerpt"] == _READY
    assert preview["replacement"]["blocked"] is False
    rendered = json.dumps(preview)
    assert "Temp" not in rendered
    assert "secret" not in rendered.casefold()
    assert "block_planning.db" not in rendered
    monkeypatch.undo()
    assert _open_ids() == before_ids
    assert _snapshot_json() == before_snapshot
    assert _completed_ids() == before_completed
    assert data_ingestion._LAST_SUMMARY is workspace["summary"]

    nearby = _make_defect("DEF_NEAR", 80.0, "DN", DefectType.WELD_FAILURE, 70, days_overdue=1.0, has_tsr=False)
    monkeypatch.setattr(data_ingestion, "get_effective_defects", lambda: (nearby,))
    duplicate = _preview(_READY, _ScriptedProvider([_finding(_READY)]))
    flagged = duplicate["candidates"][0]
    assert flagged["status"] == "POSSIBLE_DUPLICATE"
    assert "DEF_NEAR" in flagged["duplicate_of"]
    assert flagged["scheduling_preview"]["defect_id"]
    monkeypatch.undo()
    assert _snapshot_json() == before_snapshot


def test_empty_extraction_and_review_only_candidates_do_not_write(workspace: dict[str, object]) -> None:
    del workspace
    _clear_possession_flags()
    before = _open_ids()
    empty = _preview(_READY, _ScriptedProvider([]))
    assert empty["candidates"] == []
    with pytest.raises(ReportRequestError) as empty_apply:
        apply_stored_report(str(empty["report_id"]), {"operation": "replace_open_backlog", "candidate_ids": []})
    assert empty_apply.value.code == "NO_CANDIDATES"
    assert _open_ids() == before

    incomplete = _preview(_INCOMPLETE, _ScriptedProvider([_finding(_INCOMPLETE)]))
    assert incomplete["candidates"][0]["status"] == "NEEDS_REVIEW"
    assert "severity" in incomplete["candidates"][0]["missing_fields"]
    with pytest.raises(ReportRequestError) as review_apply:
        apply_stored_report(
            str(incomplete["report_id"]),
            {"operation": "replace_open_backlog", "candidate_ids": [incomplete["candidates"][0]["candidate_id"]]},
        )
    assert review_apply.value.status_code == 422
    assert "severity" in review_apply.value.message
    assert _open_ids() == before


def test_explicit_apply_replaces_open_backlog_and_keeps_completed_history(workspace: dict[str, object]) -> None:
    del workspace
    _clear_possession_flags()
    data_ingestion.clear_last_ingest()
    session = _SessionLocal()
    try:
        session.add(
            backlog_state.CompletedMaintenance(
                defect_id="STAGE4-HISTORY",
                defect_type="WELD_FAILURE",
                section_id="SECTION_2",
                track_line="DN",
                km_location=67.5,
                severity=70,
                days_overdue=1.0,
                required_block_min=60,
                bundle_id="B-TEST",
                start_min=0,
                end_min=60,
                window_label="OTHER",
                completed_by="TEST",
                completed_at=backlog_state.sim_clock.now_datetime(),
                cycle_number=0,
            )
        )
        session.commit()
    finally:
        session.close()
    backlog_state._COMPLETED_IDS.add("STAGE4-HISTORY")
    before_completed = _completed_ids()
    before_open = _open_ids()
    text = _READY + "\n" + _SECOND
    preview = _preview(text, _ScriptedProvider([_finding(_READY), _finding(_SECOND)]))
    ids = [item["candidate_id"] for item in preview["candidates"]]
    assert len(ids) == 2
    assert preview["replacement"]["eligible_open_defect_ids"] == before_open
    assert preview["replacement"]["blocked"] is False

    applied = apply_stored_report(
        str(preview["report_id"]),
        {"operation": "replace_open_backlog", "candidate_ids": ids},
    )
    assert applied["status"] == "APPLIED"
    assert applied["replaced_defect_ids"] == before_open
    assert len(applied["applied_defects"]) == 2
    assert _completed_ids() == before_completed
    assert "STAGE4-HISTORY" in _completed_ids()
    effective = _open_ids()
    assert effective == [item["defect_id"] for item in applied["applied_defects"]]
    assert not set(before_open).intersection(effective)
    assert all(item["defect_id"] for item in applied["evidence"])
    assert {item["defect_id"] for item in applied["evidence"]} == set(effective)


def test_invalid_edits_and_unknown_ids_do_not_apply(workspace: dict[str, object]) -> None:
    del workspace
    _clear_possession_flags()
    before = _open_ids()
    preview = _preview(_READY, _ScriptedProvider([_finding(_READY)]))
    report_id = str(preview["report_id"])
    candidate_id = preview["candidates"][0]["candidate_id"]
    with pytest.raises(ReportRequestError) as bad_km:
        apply_stored_report(
            report_id,
            {
                "operation": "replace_open_backlog",
                "candidate_ids": [candidate_id],
                "edits": {candidate_id: {"chainage_km": 400}},
            },
        )
    assert bad_km.value.status_code == 422
    assert "outside the corridor" in bad_km.value.message
    with pytest.raises(ReportRequestError) as fake:
        apply_stored_report(
            report_id,
            {"operation": "replace_open_backlog", "candidate_ids": ["F-made-up"]},
        )
    assert fake.value.code == "UNKNOWN_CANDIDATE"
    with pytest.raises(ReportRequestError) as missing:
        apply_stored_report("report-does-not-exist", {"operation": "replace_open_backlog", "candidate_ids": ["F1"]})
    assert missing.value.status_code == 404
    assert _open_ids() == before


def test_completed_edit_can_make_a_review_row_schedulable(workspace: dict[str, object]) -> None:
    del workspace
    _clear_possession_flags()
    preview = _preview(_INCOMPLETE, _ScriptedProvider([_finding(_INCOMPLETE)]))
    candidate_id = preview["candidates"][0]["candidate_id"]
    applied = apply_stored_report(
        str(preview["report_id"]),
        {
            "operation": "replace_open_backlog",
            "candidate_ids": [candidate_id],
            "edits": {candidate_id: {"severity": 84, "days_overdue": 22, "has_tsr": False}},
        },
    )
    assert applied["status"] == "APPLIED"
    defect = applied["applied_defects"][0]
    assert defect["severity"] == 84
    assert defect["days_overdue"] == 22.0
    assert defect["has_tsr"] is False
    assert defect["defect_type"] == DefectType.WELD_FAILURE.value


def test_repeat_apply_does_not_insert_the_report_twice(workspace: dict[str, object]) -> None:
    del workspace
    _clear_possession_flags()
    preview = _preview(_READY, _ScriptedProvider([_finding(_READY)]))
    candidate_id = preview["candidates"][0]["candidate_id"]
    body = {"operation": "replace_open_backlog", "candidate_ids": [candidate_id]}
    first = apply_stored_report(str(preview["report_id"]), body)
    with pytest.raises(ReportRequestError) as blocked:
        apply_stored_report(
            str(preview["report_id"]),
            {"operation": "replace_open_backlog", "candidate_ids": ["F-other"]},
        )
    assert blocked.value.status_code == 409
    assert blocked.value.code == "REPORT_ALREADY_APPLIED"
    second = apply_stored_report(
        str(preview["report_id"]),
        {"operation": "append_report_defects", "candidate_ids": [candidate_id]},
    )
    assert first["status"] == "APPLIED"
    assert second["status"] == "ALREADY_APPLIED"
    assert [item["defect_id"] for item in second["applied_defects"]] == [
        item["defect_id"] for item in first["applied_defects"]
    ]
    assert _open_ids() == [item["defect_id"] for item in first["applied_defects"]]
    session = _SessionLocal()
    try:
        reports = session.scalars(select(MaintenanceReport).where(MaintenanceReport.report_id == preview["report_id"])).all()
        assert len(reports) == 1
    finally:
        session.close()


def test_failed_commit_rolls_back_the_backlog_and_the_report(workspace: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    del workspace
    _clear_possession_flags()
    hold = _make_defect("STAGE4-HOLD", 80.0, "UP", DefectType.OHE_SAG, 70, days_overdue=1.0, has_tsr=False)
    data_ingestion.set_last_ingest(
        IngestSummary(
            tms=SourceIngestResult(SOURCE_TMS, 0, (), ()),
            smms=SourceIngestResult(SOURCE_SMMS, 0, (), ()),
            tdms=SourceIngestResult(SOURCE_TDMS, 1, (hold,), ()),
            coa=data_ingestion.CoaIngestResult(0, (), ()),
            used_fallback=False,
        )
    )
    before = _open_ids()
    before_snapshot = _snapshot_json()
    assert "STAGE4-HOLD" not in before
    preview = _preview(_READY, _ScriptedProvider([_finding(_READY)]))

    def explode(_session: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr("report_service.commit_session", explode)
    with pytest.raises(ReportRequestError) as failure:
        apply_stored_report(
            str(preview["report_id"]),
            {"operation": "replace_open_backlog", "candidate_ids": [preview["candidates"][0]["candidate_id"]]},
        )
    assert failure.value.code == "APPLY_FAILED"
    assert "not applied" in failure.value.message
    assert "disk full" not in failure.value.message
    assert _open_ids() == before
    assert "STAGE4-HOLD" not in _open_ids()
    assert _snapshot_json() == before_snapshot
    session = _SessionLocal()
    try:
        row = session.get(MaintenanceReport, preview["report_id"])
        assert row is not None
        assert row.status == "pending"
        evidence = session.scalars(select(ReportEvidence).where(ReportEvidence.report_id == preview["report_id"])).all()
        assert evidence
        assert all(item.defect_id is None for item in evidence)
    finally:
        session.close()


def test_reload_after_memory_reset_keeps_the_applied_backlog_and_evidence(workspace: dict[str, object]) -> None:
    del workspace
    _clear_possession_flags()
    data_ingestion.clear_last_ingest()
    preview = _preview(_READY, _ScriptedProvider([_finding(_READY)]))
    applied = apply_stored_report(
        str(preview["report_id"]),
        {"operation": "replace_open_backlog", "candidate_ids": [preview["candidates"][0]["candidate_id"]]},
    )
    expected = [item["defect_id"] for item in applied["applied_defects"]]
    backlog_state._ACTIVE = None
    data_ingestion.clear_last_ingest()
    assert [item.defect_id for item in backlog_state.get_active_defects()] == expected
    rows = evidence_rows(str(preview["report_id"]))
    assert rows
    assert rows[0]["defect_id"] == expected[0]
    assert rows[0]["report_id"] == preview["report_id"]
    assert rows[0]["excerpt"] == _READY


def evidence_rows(report_id: str) -> list[dict[str, object]]:
    session = _SessionLocal()
    try:
        rows = session.scalars(select(ReportEvidence).where(ReportEvidence.report_id == report_id)).all()
        return [{"report_id": row.report_id, "defect_id": row.defect_id, "excerpt": row.excerpt} for row in rows]
    finally:
        session.close()


def test_active_possession_conflicts_without_changes(workspace: dict[str, object]) -> None:
    del workspace
    _clear_possession_flags()
    if not _open_ids():
        hold = _make_defect("POSSESS-HOLD", 80.0, "UP", DefectType.OHE_SAG, 70, days_overdue=1.0, has_tsr=False)
        backlog_state._ensure_loaded()
        with backlog_state._LOCK:
            backlog_state._ACTIVE = [hold]
            backlog_state._save_snapshot()
    current = _open_ids()
    reserved = current[0]
    backlog_state.reserve_defects([reserved])
    app_module.SYSTEM_STATE["sanctioned"] = True
    app_module.SYSTEM_STATE["possession"] = {"defect_ids": [reserved], "bundle_id": "B-OPEN"}
    preview = _preview(_READY, _ScriptedProvider([_finding(_READY)]), possession_active=True)
    assert preview["replacement"]["blocked"] is True
    with pytest.raises(ReportRequestError) as conflict:
        apply_stored_report(
            str(preview["report_id"]),
            {"operation": "replace_open_backlog", "candidate_ids": [preview["candidates"][0]["candidate_id"]]},
            possession_active=True,
        )
    assert conflict.value.status_code == 409
    assert reserved in conflict.value.message
    assert "Nothing was changed" in conflict.value.message
    assert _open_ids() == current
    assert reserved in backlog_state.scheduled_defect_ids()
    session = _SessionLocal()
    try:
        row = session.get(MaintenanceReport, preview["report_id"])
        assert row is not None
        assert row.status == "pending"
    finally:
        session.close()


def test_synthetic_fallback_remains_when_the_report_is_not_applied(workspace: dict[str, object]) -> None:
    del workspace
    _clear_possession_flags()
    data_ingestion.clear_last_ingest()
    before = [item.defect_id for item in backlog_state.get_active_defects()]
    preview = _preview("No rail fracture was observed.", _ScriptedProvider([_finding("No rail fracture was observed.")]))
    assert preview["candidates"] == []
    assert preview["ruled_out"]
    assert _open_ids() == before
    with pytest.raises(ReportRequestError):
        apply_stored_report(str(preview["report_id"]), {"operation": "replace_open_backlog", "candidate_ids": ["R1"]})
    assert _open_ids() == before


def test_ocr_and_provider_failures_are_explicit_and_do_not_store_a_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnavailableOcr:
        def available(self) -> bool:
            return False

        def read_image(self, image: object) -> str:
            del image
            raise RuntimeError("OCR is not available.")

    before = _open_ids()
    with pytest.raises(ReportRequestError) as ocr_error:
        preview_uploaded_report(_scanned_pdf(_jpeg_bytes()), "scan.pdf", provider=_ScriptedProvider([]), ocr=UnavailableOcr())
    assert ocr_error.value.code == "OCR_UNAVAILABLE"
    assert "Tesseract" in ocr_error.value.message
    assert "Nothing was added" in ocr_error.value.message

    with pytest.raises(ReportRequestError) as timed_out:
        preview_uploaded_report(
            _text_pdf([_READY]),
            "inspection.pdf",
            provider=_ScriptedProvider(ProviderTimeoutError("timed out after 30 seconds")),
        )
    assert timed_out.value.status_code == 503
    assert "timed out" in timed_out.value.message
    assert _open_ids() == before

    monkeypatch.setenv("REPORT_AI_PROVIDER", "http")
    monkeypatch.setenv("REPORT_AI_API_KEY", "secret-key")
    with pytest.raises(Exception) as missing_provider:
        preview_uploaded_report(_text_pdf([_READY]), "inspection.pdf")
    assert "secret-key" not in str(missing_provider.value)


def test_http_ingest_still_works_and_report_routes_do_not_merge_silently(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    provider = _ScriptedProvider([_finding(_READY)])
    monkeypatch.setattr("report_service.build_report_provider", lambda: provider)
    csv_text = (
        "ASSET_ID,LINE_ID,CHAINAGE_KM,DEFECT_CODE,GRADE,DATE_REPORTED,DATE_DUE,TSR_FLAG,REMARKS\n"
        "STAGE4-INGEST-1,6,67.5,WF,8,01-08-2026,03-09-2026,N,test row\n"
    )
    with TestClient(app_module.app) as client:
        _clear_possession_flags()
        before = _open_ids()
        ingested = client.post(
            "/api/ingest",
            files={"tms_file": ("tms_defects.csv", csv_text, "text/csv")},
        )
        assert ingested.status_code == 200
        body = ingested.json()
        assert body["status"] == "INGESTED"
        assert body["total_defects_accepted"] == 1
        assert "not changed" in body["message"]
        assert "STAGE4-INGEST-1" not in _open_ids()
        assert _open_ids() == before

        preview = client.post(
            "/api/ingest/report/preview",
            files={"file": ("inspection.pdf", _text_pdf([_READY]), "application/pdf")},
        )
        assert preview.status_code == 200
        preview_body = preview.json()
        assert "STAGE4-INGEST-1" not in _open_ids()
        assert _open_ids() == before
        assert preview_body["candidates"][0]["status"] == "READY_TO_APPLY"
        refused = client.post("/api/ingest/report/not-a-real-report/apply", json={"operation": "replace_open_backlog", "candidate_ids": ["F1"]})
        assert refused.status_code == 404
        assert _open_ids() == before

        rejected = client.post(
            "/api/ingest/report/preview",
            files={"file": ("notes.txt", b"not a report", "text/plain")},
        )
        assert rejected.status_code == 415
        assert _open_ids() == before
        assert "STAGE4-INGEST-1" not in [item["task_id"] for item in client.get("/api/master-data").json()["defects"]]


def test_live_provider_without_an_adapter_returns_503(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    monkeypatch.setenv("REPORT_AI_PROVIDER", "http")
    monkeypatch.setenv("REPORT_AI_API_KEY", "secret-key")
    with TestClient(app_module.app) as client:
        unavailable = client.post(
            "/api/ingest/report/preview",
            files={"file": ("inspection.pdf", _text_pdf([_READY]), "application/pdf")},
        )
    assert unavailable.status_code == 503
    detail = unavailable.json()["detail"]
    assert detail["code"] == "PROVIDER_UNAVAILABLE"
    assert "secret-key" not in unavailable.text
    assert "Nothing was added" in detail["message"]


def test_preexisting_report_row_is_restored_after_an_apply_mutation() -> None:
    report_id = "report-restore-probe"
    session = _SessionLocal()
    try:
        for evidence in session.scalars(select(ReportEvidence).where(ReportEvidence.report_id == report_id)).all():
            session.delete(evidence)
        existing = session.get(MaintenanceReport, report_id)
        if existing is not None:
            session.delete(existing)
        session.flush()
        session.add(
            MaintenanceReport(
                report_id=report_id,
                filename="probe.pdf",
                status="pending",
                summary="pending probe",
                preview_json="{}",
                created_at=sim_clock.now_datetime(),
                applied_at=None,
                result_json=None,
            )
        )
        session.add(
            ReportEvidence(
                report_id=report_id,
                source_finding_id="F1",
                defect_id=None,
                filename="probe.pdf",
                reference="page:1",
                page_number=1,
                excerpt="probe",
                statement="probe",
                extraction_method="native_text",
                reported_defect_type=None,
                normalized_defect_type=None,
                reported_severity_text=None,
                severity=None,
                reported_track_line=None,
                track_line=None,
                chainage_km=None,
                reported_due_date=None,
                days_overdue=None,
                has_tsr=None,
                department=None,
                preview_status="READY_TO_APPLY",
            )
        )
        session.commit()
        captured = _capture_report_rows(session)
        row = session.get(MaintenanceReport, report_id)
        assert row is not None
        row.status = "applied"
        row.result_json = '{"status": "APPLIED"}'
        evidence = session.scalars(select(ReportEvidence).where(ReportEvidence.report_id == report_id)).one()
        evidence.defect_id = "AI1-probe"
        session.commit()
        _restore_report_rows(session, {report_id: captured[report_id]})
        session.commit()
        session.expire_all()
        restored = session.get(MaintenanceReport, report_id)
        assert restored is not None
        assert restored.status == "pending"
        assert restored.result_json is None
        assert restored.summary == "pending probe"
        restored_evidence = session.scalars(select(ReportEvidence).where(ReportEvidence.report_id == report_id)).one()
        assert restored_evidence.defect_id is None
    finally:
        for evidence in session.scalars(select(ReportEvidence).where(ReportEvidence.report_id == report_id)).all():
            session.delete(evidence)
        leftover = session.get(MaintenanceReport, report_id)
        if leftover is not None:
            session.delete(leftover)
        session.commit()
        session.close()


def _candidate_for(preview: dict[str, object], excerpt: str) -> dict[str, object]:
    rows = preview["candidates"]
    assert isinstance(rows, list)
    for item in rows:
        assert isinstance(item, dict)
        evidence = item["evidence"]
        assert isinstance(evidence, dict)
        if evidence.get("excerpt") == excerpt:
            return item
    raise AssertionError(excerpt)


def test_partial_apply_keeps_remaining_candidates_and_audit_rows(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    monkeypatch.setenv("REPORT_AI_PROVIDER", "mock")
    monkeypatch.delenv("REPORT_AI_MOCK_FIXTURE", raising=False)
    monkeypatch.delenv("REPORT_AI_API_KEY", raising=False)
    _clear_possession_flags()
    backlog_state._ensure_loaded()
    with backlog_state._LOCK:
        backlog_state._ACTIVE = []
        backlog_state._save_snapshot()
    data_ingestion.clear_last_ingest()
    completed = _completed_ids()
    fracture = "Rail fracture at KM 40.0 on the UP fast line, severity 92, 18 days overdue, TSR imposed."
    ohe = "OHE sag at KM 150.0 on the UP line, severity 76, 9 days overdue, no TSR."
    negated = "No rail fracture was observed at KM 12.0 on the UP fast line."
    uncertain = "Possible weld failure at KM 70.0 on the DN line, severity 50, 3 days overdue, no TSR."
    text = "\n".join((_READY, fracture, ohe, uncertain, negated))
    payload = _text_pdf([text])
    with TestClient(app_module.app) as client:
        first = client.post(
            "/api/ingest/report/preview",
            files={"file": ("three-findings.pdf", payload, "application/pdf")},
        )
        assert first.status_code == 200
        preview = first.json()
        report_id = str(preview["report_id"])
        weld = _candidate_for(preview, _READY)
        rail = _candidate_for(preview, fracture)
        wire = _candidate_for(preview, ohe)
        review = _candidate_for(preview, uncertain)
        assert weld["status"] == "READY_TO_APPLY"
        assert rail["status"] == "READY_TO_APPLY"
        assert wire["status"] == "READY_TO_APPLY"
        assert review["status"] == "NEEDS_REVIEW"
        assert preview["ruled_out"][0]["excerpt"] == negated
        assert preview["ruled_out"][0]["status"] == "RULED_OUT"
        before_rows = evidence_for_report(report_id)
        ruled_rows = [row for row in before_rows if row["preview_status"] == "RULED_OUT"]
        review_rows = [row for row in before_rows if row["source_finding_id"] == review["candidate_id"]]
        assert len(ruled_rows) == 1
        assert ruled_rows[0]["defect_id"] is None
        assert ruled_rows[0]["excerpt"] == negated
        assert ruled_rows[0]["reference"] == "page:1"
        assert ruled_rows[0]["filename"] == "three-findings.pdf"
        assert ruled_rows[0]["severity"] is None
        assert ruled_rows[0]["chainage_km"] is None
        assert "negated_finding" in str(ruled_rows[0]["reported_severity_text"])
        assert len(review_rows) == 1
        assert review_rows[0]["defect_id"] is None
        assert review_rows[0]["preview_status"] == "NEEDS_REVIEW"
        assert review_rows[0]["excerpt"] == uncertain

        again = client.post(
            "/api/ingest/report/preview",
            files={"file": ("three-findings.pdf", payload, "application/pdf")},
        )
        assert again.status_code == 200
        preview = again.json()
        assert preview["report_id"] == report_id
        weld = _candidate_for(preview, _READY)
        rail = _candidate_for(preview, fracture)
        wire = _candidate_for(preview, ohe)
        review = _candidate_for(preview, uncertain)
        assert len(evidence_for_report(report_id)) == len(before_rows)
        assert len([row for row in evidence_for_report(report_id) if row["preview_status"] == "RULED_OUT"]) == 1

        weld_id = str(weld["scheduling_preview"]["defect_id"])
        rail_id = str(rail["scheduling_preview"]["defect_id"])
        wire_id = str(wire["scheduling_preview"]["defect_id"])
        applied_a = client.post(
            f"/api/ingest/report/{report_id}/apply",
            json={"operation": "append_report_defects", "candidate_ids": [weld["candidate_id"]]},
        )
        assert applied_a.status_code == 200
        assert applied_a.json()["status"] == "APPLIED"
        assert [item["defect_id"] for item in applied_a.json()["applied_defects"]] == [weld_id]
        assert _open_ids() == [weld_id]
        linked = {row["source_finding_id"]: row for row in evidence_for_report(report_id)}
        assert linked[weld["candidate_id"]]["defect_id"] == weld_id
        assert linked[rail["candidate_id"]]["defect_id"] is None
        assert linked[wire["candidate_id"]]["defect_id"] is None
        assert linked[preview["ruled_out"][0]["finding_id"]]["defect_id"] is None

        opened = client.get(f"/api/ingest/report/{report_id}")
        assert opened.status_code == 200
        reopened = opened.json()
        by_candidate = {item["candidate_id"]: item for item in reopened["candidates"]}
        assert by_candidate[weld["candidate_id"]]["applied_defect_id"] == weld_id
        assert by_candidate[rail["candidate_id"]]["applied_defect_id"] is None
        assert reopened["application"]["keeps_open_defect_ids"] == [weld_id]
        assert by_candidate[wire["candidate_id"]]["applied_defect_id"] is None
        assert by_candidate[review["candidate_id"]]["status"] == "NEEDS_REVIEW"
        audit = reopened["audit_evidence"]
        assert any(row["preview_status"] == "RULED_OUT" and row["excerpt"] == negated and row["defect_id"] is None for row in audit)
        assert any(row["preview_status"] == "NEEDS_REVIEW" and row["excerpt"] == uncertain and row["defect_id"] is None for row in audit)
        assert len(evidence_for_report(report_id)) == len(before_rows)
        master = client.get("/api/master-data")
        master_defects = master.json()["defects"]
        assert [item["task_id"] for item in master_defects] == [weld_id]
        defect_text = json.dumps(master_defects)
        assert negated not in defect_text
        assert uncertain not in defect_text

        import solver
        from solver import SolverResult

        seen: list[tuple[str, ...]] = []

        def _record(bundles: object, trains: object, **kwargs: object) -> SolverResult:
            del trains, kwargs
            assert isinstance(bundles, list)
            seen.append(tuple(item.defect_id for bundle in bundles for item in bundle.defects))
            return SolverResult("FEASIBLE", 0.0, (), (), 0.0, 0.0)

        monkeypatch.setattr(solver, "solve_corridor_schedule", _record)
        planned = client.get("/api/optimize")
        assert planned.status_code == 200
        assert seen
        assert all(call == (weld_id,) for call in seen)
        assert negated not in planned.text

        repeat_a = client.post(
            f"/api/ingest/report/{report_id}/apply",
            json={"operation": "append_report_defects", "candidate_ids": [weld["candidate_id"]]},
        )
        assert repeat_a.status_code == 200
        assert repeat_a.json()["status"] == "ALREADY_APPLIED"
        assert _open_ids() == [weld_id]
        mixed = client.post(
            f"/api/ingest/report/{report_id}/apply",
            json={"operation": "append_report_defects", "candidate_ids": [weld["candidate_id"], rail["candidate_id"]]},
        )
        assert mixed.status_code == 200
        assert mixed.json()["status"] == "APPLIED"
        assert [item["defect_id"] for item in mixed.json()["applied_defects"]] == [rail_id]
        assert _open_ids() == [weld_id, rail_id]
        assert evidence_for_report(report_id)
        assert {row["source_finding_id"]: row["defect_id"] for row in evidence_for_report(report_id)}[weld["candidate_id"]] == weld_id
        applied_c = client.post(
            f"/api/ingest/report/{report_id}/apply",
            json={"operation": "append_report_defects", "candidate_ids": [wire["candidate_id"]]},
        )
        assert applied_c.status_code == 200
        assert [item["defect_id"] for item in applied_c.json()["applied_defects"]] == [wire_id]
        assert _open_ids() == [weld_id, rail_id, wire_id]
        blocked_review = client.post(
            f"/api/ingest/report/{report_id}/apply",
            json={"operation": "append_report_defects", "candidate_ids": [review["candidate_id"]]},
        )
        assert blocked_review.status_code == 422
        all_again = client.post(
            f"/api/ingest/report/{report_id}/apply",
            json={
                "operation": "append_report_defects",
                "candidate_ids": [weld["candidate_id"], rail["candidate_id"], wire["candidate_id"]],
            },
        )
        assert all_again.status_code == 200
        assert all_again.json()["status"] == "ALREADY_APPLIED"
        assert _open_ids() == [weld_id, rail_id, wire_id]
        replaced = client.post(
            f"/api/ingest/report/{report_id}/apply",
            json={"operation": "replace_open_backlog", "candidate_ids": [wire["candidate_id"]]},
        )
        assert replaced.status_code == 409
        assert replaced.json()["detail"]["code"] == "REPORT_ALREADY_APPLIED"
        uploaded = client.post(
            "/api/ingest/report/preview",
            files={"file": ("three-findings.pdf", payload, "application/pdf")},
        )
        assert uploaded.status_code == 409
        assert uploaded.json()["detail"]["code"] == "REPORT_ALREADY_APPLIED"
        assert len(evidence_for_report(report_id)) == len(before_rows)
        assert load_preview(report_id)["stored_status"] == "applied"
        final_defects = client.get("/api/master-data").json()["defects"]
        assert [item["task_id"] for item in final_defects] == [weld_id, rail_id, wire_id]
        final_text = json.dumps(final_defects)
        assert negated not in final_text
        assert uncertain not in final_text
        assert _completed_ids() == completed
        assert client.get(f"/api/defects/{preview['ruled_out'][0]['finding_id']}/evidence").status_code == 404


def test_limits_reject_an_oversized_upload_without_a_backlog_write() -> None:
    before = _open_ids()
    with pytest.raises(ReportRequestError) as too_big:
        preview_uploaded_report(b"%PDF-1.4\n" + (b"x" * 40), "big.pdf", limits=DocumentLimits(max_bytes=32))
    assert too_big.value.status_code == 413
    assert _open_ids() == before
    assert "commit_session" not in too_big.value.message
