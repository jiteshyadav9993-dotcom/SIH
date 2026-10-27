"""Findings stay visible after preview, including when none can be applied.

The preview response is the existing validation result. These tests use a
temporary SQLite file and put the process engine back afterwards. They do
not write the live block_planning.db.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app as app_module
import backlog_state
import persistence
import report_store
import sim_clock
from tests.test_report_dashboard import _source
from tests.test_report_documents import _text_pdf
from tests.test_report_planner_integration import _use_local_provider

_PAGE = Path(__file__).resolve().parents[1] / "index.html"
_INCOMPLETE = "Signal interlock at KM 9.4 on the UP fast line."
_NEGATED = "No rail fracture was observed at KM 12.0 on the UP fast line."
_READY = "Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR."
_NO_ELIGIBLE = "Report processed — no eligible records"


@pytest.fixture
def isolated_report_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point report and backlog sessions at a new SQLite file for one test."""
    url = "sqlite:///" + (tmp_path / "findings_preview.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    session_local = sessionmaker(bind=engine, expire_on_commit=False)
    saved = {
        "engine": persistence._engine,
        "session": persistence._SessionLocal,
        "backlog_engine": backlog_state._engine,
        "backlog_session": backlog_state._SessionLocal,
        "report_session": report_store._SessionLocal,
        "active": backlog_state._ACTIVE,
        "cycle": backlog_state._CYCLE,
        "scheduled": set(backlog_state._SCHEDULED_IDS),
        "completed": set(backlog_state._COMPLETED_IDS),
        "summary": app_module.data_ingestion._LAST_SUMMARY,
        "sanctioned": app_module.SYSTEM_STATE.get("sanctioned"),
        "possession": app_module.SYSTEM_STATE.get("possession"),
        "legacy": app_module._LEGACY_MIGRATION,
        "clock": (
            sim_clock._absolute_day,
            sim_clock._minute,
            sim_clock._multiplier,
            sim_clock._last_poll,
            sim_clock._last_tick,
            sim_clock._last_save_mono,
        ),
    }
    persistence._engine = engine
    persistence._SessionLocal = session_local
    backlog_state._engine = engine
    backlog_state._SessionLocal = session_local
    report_store._SessionLocal = session_local
    backlog_state._ACTIVE = []
    app_module.SYSTEM_STATE["sanctioned"] = False
    app_module.SYSTEM_STATE["possession"] = None
    _use_local_provider(monkeypatch)
    try:
        yield
    finally:
        persistence._engine = saved["engine"]
        persistence._SessionLocal = saved["session"]
        backlog_state._engine = saved["backlog_engine"]
        backlog_state._SessionLocal = saved["backlog_session"]
        report_store._SessionLocal = saved["report_session"]
        backlog_state._ACTIVE = saved["active"]
        backlog_state._CYCLE = saved["cycle"]
        backlog_state._SCHEDULED_IDS.clear()
        backlog_state._SCHEDULED_IDS.update(saved["scheduled"])
        backlog_state._COMPLETED_IDS.clear()
        backlog_state._COMPLETED_IDS.update(saved["completed"])
        app_module.data_ingestion._LAST_SUMMARY = saved["summary"]
        app_module.SYSTEM_STATE["sanctioned"] = saved["sanctioned"]
        app_module.SYSTEM_STATE["possession"] = saved["possession"]
        app_module._LEGACY_MIGRATION = saved["legacy"]
        (
            sim_clock._absolute_day,
            sim_clock._minute,
            sim_clock._multiplier,
            sim_clock._last_poll,
            sim_clock._last_tick,
            sim_clock._last_save_mono,
        ) = saved["clock"]
        engine.dispose()


def _upload(client: TestClient, pages: list[str], filename: str) -> dict[str, object]:
    response = client.post(
        "/api/ingest/report/preview",
        files={"file": (filename, _text_pdf(pages), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _open_ids(client: TestClient) -> list[str]:
    response = client.get("/api/master-data")
    assert response.status_code == 200
    return [item["task_id"] for item in response.json()["defects"]]


def _candidate(preview: dict[str, object], excerpt: str) -> dict[str, object]:
    rows = preview["candidates"]
    assert isinstance(rows, list)
    for item in rows:
        assert isinstance(item, dict)
        evidence = item["evidence"]
        assert isinstance(evidence, dict)
        if evidence.get("excerpt") == excerpt:
            return item
    raise AssertionError(excerpt)


def test_findings_preview_section_keeps_rejected_rows_and_requires_approval() -> None:
    page = _PAGE.read_text(encoding="utf-8")
    portal = page.split('id="secReport"', 1)[1]
    assert portal.index('id="reportJourney"') < portal.index('id="findingsPreviewReview"')
    assert "Findings Preview &amp; Review" in portal
    assert 'id="findingsPreviewNotice"' in portal
    assert 'id="findingsPreviewCounts"' in portal
    assert 'id="findingsPreviewList"' in portal
    assert 'id="btnApproveFindings"' in portal
    assert "APPROVE SELECTED RECORDS" in portal
    assert "Nothing is applied automatically." in portal
    assert 'id="btnReviewReplace"' not in portal
    assert 'id="btnCancelReport"' not in portal
    assert 'id="btnConfirmApply"' not in portal
    assert _NO_ELIGIBLE in page
    painter = _source("paintFindingsPreview", page)
    assert "renderFinding" in painter
    assert "renderRuledOut" in painter
    assert 'reportCountChip("Proposed"' in painter
    assert 'reportCountChip("Ready"' in painter
    assert 'reportCountChip("Needs review"' in painter
    assert 'reportCountChip("Ruled out"' in painter
    assert _NO_ELIGIBLE in painter
    finding = _source("renderFinding", page)
    for label in ("Department:", "Defect type:", "Chainage:", "Running line:", "Missing or invalid fields:", "Page:"):
        assert label in finding
    assert "candidate.missing_fields" in finding
    assert "candidate.reasons" in finding
    assert "evidence.excerpt" in finding
    assert "evidence.page_numbers" in finding
    ruled = _source("renderRuledOut", page)
    assert "item.excerpt" in ruled
    assert "item.page_numbers" in ruled
    assert "item.reason" in ruled
    assert "This ruled-out finding is not an operational defect." in ruled
    send = _source("reportSend", page)
    assert "reportAcceptedIds" in send
    assert "await reloadBacklog();" in send
    assert "reportHoldPreview" in send
    assert "/apply" not in send
    assert "append_report_defects" not in send
    apply_fn = _source("reportApply", page)
    assert "append_report_defects" in apply_fn
    assert apply_fn.index("reportApplyIsConfirmed") < apply_fn.index("reloadBacklog")
    assert 'getElementById("btnApproveFindings")' in _source("bindReportReview", page)
    assert "reportApply" in _source("bindReportReview", page)
    assert "replace_open_backlog" not in page


def test_preview_exposes_findings_when_none_are_eligible(isolated_report_db: None) -> None:
    del isolated_report_db
    with TestClient(app_module.app) as client:
        before = _open_ids(client)
        preview = _upload(client, [_INCOMPLETE, _NEGATED], "needs-review-only.pdf")
        incomplete = _candidate(preview, _INCOMPLETE)
        evidence = incomplete["evidence"]
        assert isinstance(evidence, dict)
        assert incomplete["status"] == "NEEDS_REVIEW"
        assert evidence["normalized_defect_type"] == "SIGNAL_INTERLOCK"
        assert evidence["chainage_km"] == 9.4
        assert evidence["track_line"] == "UP_FAST"
        assert evidence["excerpt"] == _INCOMPLETE
        assert evidence["page_numbers"] == [1]
        assert "severity" in incomplete["missing_fields"]
        assert "days_overdue" in incomplete["missing_fields"]
        assert "has_tsr" in incomplete["missing_fields"]
        assert any("Required data is missing" in str(reason) for reason in incomplete["reasons"])
        ruled = preview["ruled_out"]
        assert isinstance(ruled, list) and len(ruled) == 1
        assert ruled[0]["status"] == "RULED_OUT"
        assert ruled[0]["excerpt"] == _NEGATED
        assert ruled[0]["reason"]
        assert ruled[0]["page_numbers"] == [2]
        assert _open_ids(client) == before
        refused = client.post(
            f"/api/ingest/report/{preview['report_id']}/apply",
            json={"operation": "append_report_defects", "candidate_ids": [incomplete["candidate_id"]]},
        )
        assert refused.status_code == 422
        assert "severity" in refused.json()["detail"]["message"]
        assert _open_ids(client) == before
        assert str(persistence._engine.url).startswith("sqlite:///")
        assert "block_planning.db" not in str(persistence._engine.url)


def test_preview_exposes_eligible_records_until_they_are_approved(isolated_report_db: None) -> None:
    del isolated_report_db
    with TestClient(app_module.app) as client:
        before = _open_ids(client)
        preview = _upload(client, [_READY, _INCOMPLETE, _NEGATED], "one-ready-record.pdf")
        ready = _candidate(preview, _READY)
        incomplete = _candidate(preview, _INCOMPLETE)
        ready_evidence = ready["evidence"]
        assert isinstance(ready_evidence, dict)
        assert ready["status"] == "READY_TO_APPLY"
        assert ready["missing_fields"] == []
        assert ready_evidence["normalized_defect_type"] == "WELD_FAILURE"
        assert ready_evidence["department"] in {"TMS", None}
        assert ready_evidence["chainage_km"] == 80.0
        assert ready_evidence["track_line"] == "DN"
        assert ready_evidence["excerpt"] == _READY
        assert ready_evidence["page_numbers"] == [1]
        assert incomplete["status"] == "NEEDS_REVIEW"
        assert any("Required data is missing" in str(reason) for reason in incomplete["reasons"])
        ruled = preview["ruled_out"]
        assert isinstance(ruled, list) and ruled[0]["excerpt"] == _NEGATED
        assert _open_ids(client) == before
        applied = client.post(
            f"/api/ingest/report/{preview['report_id']}/apply",
            json={"operation": "append_report_defects", "candidate_ids": [str(ready["candidate_id"])]},
        )
        assert applied.status_code == 200, applied.text
        body = applied.json()
        assert body["status"] == "APPLIED"
        added = [item["defect_id"] for item in body["applied_defects"]]
        assert added
        assert _open_ids(client) == before + added
        still_refused = client.post(
            f"/api/ingest/report/{preview['report_id']}/apply",
            json={"operation": "append_report_defects", "candidate_ids": [str(incomplete["candidate_id"])]},
        )
        assert still_refused.status_code == 422
        assert _open_ids(client) == before + added
