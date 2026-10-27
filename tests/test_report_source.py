"""Report-derived backlog: no synthetic seed, append apply, and a separate portal."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app as app_module
import backlog_state
import data_ingestion
import defects
from backlog_state import CompletedMaintenance
from defects import DefectType, TrackDefect, _make_defect
from persistence import _SessionLocal
from report_provider import ProviderTimeoutError
from report_service import ReportRequestError, apply_stored_report
from report_store import applied_report_summaries
from tests.test_report_apply import (
    _ScriptedProvider,
    _clear_possession_flags,
    _completed_ids,
    _finding,
    _open_ids,
    _preview,
    workspace,
)
from tests.test_report_documents import _text_pdf

_PAGE = Path(__file__).resolve().parents[1] / "index.html"
_READY = "Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR."
_SECOND = "Rail fracture at KM 40.0 on the UP fast line, severity 92, 18 days overdue, TSR imposed."


def _install_open(items: list[TrackDefect]) -> None:
    backlog_state._ensure_loaded()
    with backlog_state._LOCK:
        backlog_state._ACTIVE = list(items)
        backlog_state._save_snapshot()


def _purge_completed(defect_id: str) -> None:
    session = _SessionLocal()
    try:
        rows = session.scalars(select(CompletedMaintenance).where(CompletedMaintenance.defect_id == defect_id)).all()
        for row in rows:
            session.delete(row)
        session.commit()
    finally:
        session.close()
    backlog_state._COMPLETED_IDS.discard(defect_id)


def test_fresh_load_does_not_seed_synthetic_defaults(workspace: dict[str, object]) -> None:
    del workspace
    session = _SessionLocal()
    try:
        row = session.get(backlog_state.BacklogSnapshot, 1)
        if row is not None:
            session.delete(row)
            session.commit()
    finally:
        session.close()
    backlog_state._ACTIVE = None
    loaded = backlog_state.get_active_defects()
    assert loaded == ()
    assert {item.defect_id for item in defects.get_default_defects()}.isdisjoint(
        item.defect_id for item in loaded
    )


def test_effective_backlog_ignores_the_ingest_cache(workspace: dict[str, object]) -> None:
    del workspace
    before = _open_ids()
    hold = _make_defect("STAGE4-HOLD", 80.0, "UP", DefectType.OHE_SAG, 70, days_overdue=1.0, has_tsr=False)
    data_ingestion.set_last_ingest(
        data_ingestion.IngestSummary(
            tms=data_ingestion.SourceIngestResult(defects.SOURCE_TMS, 0, (), ()),
            smms=data_ingestion.SourceIngestResult(defects.SOURCE_SMMS, 0, (), ()),
            tdms=data_ingestion.SourceIngestResult(defects.SOURCE_TDMS, 1, (hold,), ()),
            coa=data_ingestion.CoaIngestResult(0, (), ()),
            used_fallback=False,
        )
    )
    assert [item.defect_id for item in data_ingestion.get_effective_defects()] == before
    assert "STAGE4-HOLD" not in _open_ids()


def test_applying_two_reports_appends_and_keeps_each_summary(workspace: dict[str, object]) -> None:
    del workspace
    _clear_possession_flags()
    _install_open([])
    first = _preview(_READY, _ScriptedProvider([_finding(_READY)]))
    applied = apply_stored_report(
        str(first["report_id"]),
        {"operation": "append_report_defects", "candidate_ids": [first["candidates"][0]["candidate_id"]]},
    )
    first_id = applied["applied_defects"][0]["defect_id"]
    assert _open_ids() == [first_id]
    second = _preview(_SECOND, _ScriptedProvider([_finding(_SECOND)]))
    again = apply_stored_report(
        str(second["report_id"]),
        {"operation": "append_report_defects", "candidate_ids": [second["candidates"][0]["candidate_id"]]},
    )
    second_id = again["applied_defects"][0]["defect_id"]
    assert _open_ids() == [first_id, second_id]
    assert again["kept_defect_ids"] == [first_id]
    assert again["replaced_defect_ids"] == []
    summaries = {item["report_id"]: item["summary"] for item in applied_report_summaries()}
    assert first["report_id"] in summaries
    assert second["report_id"] in summaries
    assert summaries[str(first["report_id"])] != summaries[str(second["report_id"])]
    assert _READY in str(summaries[str(first["report_id"])])
    repeat = apply_stored_report(
        str(first["report_id"]),
        {"operation": "append_report_defects", "candidate_ids": [first["candidates"][0]["candidate_id"]]},
    )
    assert repeat["status"] == "ALREADY_APPLIED"
    assert _open_ids() == [first_id, second_id]
    with pytest.raises(ReportRequestError) as unknown:
        apply_stored_report(
            str(first["report_id"]),
            {"operation": "append_report_defects", "candidate_ids": ["F-other"]},
        )
    assert unknown.value.status_code == 400
    assert unknown.value.code == "UNKNOWN_CANDIDATE"
    assert _open_ids() == [first_id, second_id]


def test_preview_failure_and_mock_do_not_inject_defects(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    monkeypatch.delenv("REPORT_AI_PROVIDER", raising=False)
    _clear_possession_flags()
    _install_open([])
    empty = _preview(_READY, _ScriptedProvider([]))
    assert empty["candidates"] == []
    assert empty["provider"]["live"] is False
    assert empty["provider"]["provider"] == "mock"
    assert "deterministic mock" in str(empty["provider"]["limitation"])
    with pytest.raises(ReportRequestError) as empty_apply:
        apply_stored_report(str(empty["report_id"]), {"operation": "append_report_defects", "candidate_ids": []})
    assert empty_apply.value.code == "NO_CANDIDATES"
    with pytest.raises(ReportRequestError) as timed:
        _preview(_READY, _ScriptedProvider(ProviderTimeoutError("timed out after 30 seconds")))
    assert timed.value.status_code == 503
    assert _open_ids() == []


def test_next_cycle_and_completion_do_not_generate_synthetic_defects(workspace: dict[str, object]) -> None:
    del workspace
    waiting = _make_defect("AI1-cycle", 80.0, "DN", DefectType.WELD_FAILURE, 70, days_overdue=4.0, has_tsr=False)
    _install_open([waiting])
    aged = backlog_state.next_maintenance_cycle(seed=1)
    assert aged["new_defects"] == 0
    assert aged["carried_over"] == 1
    assert _open_ids() == ["AI1-cycle"]
    assert backlog_state.get_active_defects()[0].days_overdue == 5.0
    finished = _make_defect("SOURCE-DONE", 90.0, "DN", DefectType.OHE_SAG, 60, days_overdue=1.0, has_tsr=False)
    try:
        _install_open([finished])
        logged = backlog_state.complete_defects([finished], "B-SOURCE", 0, 60, "OTHER", "TEST")
        assert logged == 1
        assert _open_ids() == []
        assert "SOURCE-DONE" in _completed_ids()
        empty_cycle = backlog_state.next_maintenance_cycle(seed=2)
        assert empty_cycle["new_defects"] == 0
        assert _open_ids() == []
    finally:
        _purge_completed("SOURCE-DONE")


def test_restart_keeps_the_applied_defect_summary_and_evidence(workspace: dict[str, object]) -> None:
    del workspace
    _clear_possession_flags()
    _install_open([])
    before_completed = _completed_ids()
    preview = _preview(_READY, _ScriptedProvider([_finding(_READY)]))
    applied = apply_stored_report(
        str(preview["report_id"]),
        {"operation": "append_report_defects", "candidate_ids": [preview["candidates"][0]["candidate_id"]]},
    )
    expected = applied["applied_defects"][0]["defect_id"]
    backlog_state._ACTIVE = None
    assert [item.defect_id for item in backlog_state.get_active_defects()] == [expected]
    summaries = applied_report_summaries()
    stored = next(item for item in summaries if item["report_id"] == preview["report_id"])
    assert expected in stored["defect_ids"]
    assert stored["summary"]
    assert _completed_ids() == before_completed


def test_reserved_work_is_kept_and_blocks_synthetic_archive(workspace: dict[str, object]) -> None:
    del workspace
    _clear_possession_flags()
    reserved = _make_defect("FIELD-KEEP", 90.0, "DN", DefectType.OHE_SAG, 60, days_overdue=2.0, has_tsr=False)
    catalog = defects.get_default_defects()[0]
    _install_open([reserved, catalog])
    backlog_state.reserve_defects([reserved.defect_id, catalog.defect_id])
    app_module.SYSTEM_STATE["sanctioned"] = True
    preview = _preview(_READY, _ScriptedProvider([_finding(_READY)]), possession_active=True)
    added = apply_stored_report(
        str(preview["report_id"]),
        {"operation": "append_report_defects", "candidate_ids": [preview["candidates"][0]["candidate_id"]]},
        possession_active=True,
    )
    added_id = added["applied_defects"][0]["defect_id"]
    assert _open_ids() == [reserved.defect_id, catalog.defect_id, added_id]
    assert reserved.defect_id in backlog_state.scheduled_defect_ids()
    other = _preview(_SECOND, _ScriptedProvider([_finding(_SECOND)]), possession_active=True)
    with pytest.raises(ReportRequestError) as conflict:
        apply_stored_report(
            str(other["report_id"]),
            {"operation": "replace_open_backlog", "candidate_ids": [other["candidates"][0]["candidate_id"]]},
            possession_active=True,
        )
    assert conflict.value.status_code == 409
    assert catalog.defect_id in _open_ids()
    blocked = backlog_state.migrate_legacy_synthetic_open_records()
    assert blocked["status"] == "CONFLICT"
    assert catalog.defect_id in blocked["blocked_defect_ids"]
    assert catalog.defect_id in _open_ids()
    assert reserved.defect_id in _open_ids()


def test_legacy_synthetic_rows_are_archived_and_can_be_restored(workspace: dict[str, object]) -> None:
    del workspace
    _clear_possession_flags()
    catalog = defects.get_default_defects()[1]
    aged = _make_defect(
        catalog.defect_id,
        catalog.km_location,
        catalog.track_line,
        catalog.defect_type,
        catalog.severity,
        catalog.days_overdue + 6.0,
        catalog.has_tsr,
    )
    generated = backlog_state.generate_random_defects(1, 3, seed=4)[0]
    report_row = _make_defect("AI1-keep", 80.0, "DN", DefectType.WELD_FAILURE, 70, days_overdue=1.0, has_tsr=False)
    custom = _make_defect("FIELD-REAL", 100.0, "UP", DefectType.RAIL_FRACTURE, 80, days_overdue=3.0, has_tsr=True)
    altered = _make_defect("DEF_S2_01", 67.5, "DN", DefectType.WELD_FAILURE, 99, days_overdue=22.0, has_tsr=False)
    linked = defects.get_default_defects()[0]
    assert backlog_state.synthetic_origin(linked, {linked.defect_id}) is None
    assert backlog_state.synthetic_origin(altered) is None
    assert backlog_state.synthetic_origin(aged) == "default_catalog"
    assert backlog_state.synthetic_origin(generated) == "random_cycle_generator"
    _install_open([aged, generated, report_row, custom, altered])
    before_completed = _completed_ids()
    archived = backlog_state.migrate_legacy_synthetic_open_records()
    assert archived["status"] == "ARCHIVED"
    assert aged.defect_id in archived["archived_defect_ids"]
    assert generated.defect_id in archived["archived_defect_ids"]
    assert archived["kept_defect_ids"] == [report_row.defect_id, custom.defect_id, altered.defect_id]
    assert _completed_ids() == before_completed
    restored = backlog_state.restore_archived_synthetic_defects([aged.defect_id, generated.defect_id])
    assert restored["status"] == "RESTORED"
    assert aged.defect_id in restored["restored_defect_ids"]
    assert generated.defect_id in restored["restored_defect_ids"]
    assert report_row.defect_id in _open_ids()
    assert custom.defect_id in _open_ids()
    assert altered.defect_id in _open_ids()


def test_demo_reset_does_not_restore_synthetic_defects(workspace: dict[str, object]) -> None:
    del workspace
    _clear_possession_flags()
    kept = _make_defect("AI1-stay", 80.0, "DN", DefectType.WELD_FAILURE, 70, days_overdue=1.0, has_tsr=False)
    with TestClient(app_module.app) as client:
        _install_open([kept])
        before = _open_ids()
        before_completed = _completed_ids()
        response = client.post("/api/backlog/reset-demo")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "RETIRED"
        assert "not loaded" in body["message"]
        assert _open_ids() == before
        assert _completed_ids() == before_completed
        assert {item.defect_id for item in defects.get_default_defects()}.isdisjoint(_open_ids())


def test_empty_backlog_optimize_returns_no_defects(workspace: dict[str, object]) -> None:
    del workspace
    _clear_possession_flags()
    _install_open([])
    before_completed = _completed_ids()
    catalog = {item.defect_id for item in defects.get_default_defects()}
    with TestClient(app_module.app) as client:
        response = client.get("/api/optimize")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "NO_DEFECTS"
    assert body["target_bundle"] is None
    assert body["candidate_options"] == []
    assert body["freight_forecast_range"] == {"optimistic": 0, "expected": 0, "pessimistic": 0}
    assert _open_ids() == []
    assert _completed_ids() == before_completed
    assert catalog.isdisjoint(_open_ids())
    assert "DEF_" not in response.text


def test_reserved_open_defects_still_conflict_on_optimize(workspace: dict[str, object]) -> None:
    del workspace
    _clear_possession_flags()
    hold = _make_defect("OPT-HOLD", 80.0, "UP", DefectType.OHE_SAG, 70, days_overdue=1.0, has_tsr=False)
    _install_open([hold])
    backlog_state.reserve_defects([hold.defect_id])
    before_completed = _completed_ids()
    with TestClient(app_module.app) as client:
        response = client.get("/api/optimize")
    assert response.status_code == 409
    assert response.json()["detail"] == "Every open defect is already scheduled."
    assert _open_ids() == [hold.defect_id]
    assert hold.defect_id in backlog_state.scheduled_defect_ids()
    assert _completed_ids() == before_completed
    assert {item.defect_id for item in defects.get_default_defects()}.isdisjoint(_open_ids())


def test_portal_is_separate_and_the_officer_console_has_no_upload() -> None:
    page = _PAGE.read_text(encoding="utf-8")
    nav = page.split('<div class="nav-bar">', 1)[1].split("</div>", 1)[0]
    officer = page.split('id="secOfficer"', 1)[1].split('id="secChief"', 1)[0]
    portal = page.split('id="secReport"', 1)[1].split('id="reportReview"', 1)[0]
    assert nav.index("Weekly / Monthly Plan") < nav.index("BDMS Report Portal")
    assert 'id="tabReport"' in nav
    assert 'id="reportFile"' in portal
    assert 'id="btnSendReport"' in portal
    assert 'id="reportFile"' not in officer
    assert 'id="btnSendReport"' not in officer
    assert "LOAD DEMO SET" not in page
    assert "No report-derived defects have been applied yet. Upload a report in BDMS Report Portal." in officer


def test_provider_status_does_not_claim_a_live_model(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    monkeypatch.delenv("REPORT_AI_PROVIDER", raising=False)
    monkeypatch.delenv("REPORT_AI_API_KEY", raising=False)
    with TestClient(app_module.app) as client:
        response = client.get("/api/ingest/report/status")
    assert response.status_code == 200
    body = response.json()
    assert body["live"] is False
    assert body["provider"] == "mock"
    assert body["model"] is None
    assert body["nlp_status"] == "pending"
    assert "deterministic mock" in body["limitation"]
    assert "secret" not in response.text
    assert isinstance(body["ocr_available"], bool)
