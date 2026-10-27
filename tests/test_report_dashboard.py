"""Dashboard report review: the page contract and the preview/apply calls it makes."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import backlog_state
import data_ingestion
from defects import DefectType, _make_defect
from report_service import preview_uploaded_report
from report_store import ReportEvidence, commit_session, evidence_for_report, load_preview, open_session
from tests.test_report_apply import (
    _ScriptedProvider,
    _clear_possession_flags,
    _finding,
    _open_ids,
    _snapshot_json,
    workspace,
)
from tests.test_report_documents import _jpeg_bytes, _text_and_image_pdf, _text_pdf

_PAGE = Path(__file__).resolve().parents[1] / "index.html"
_INCOMPLETE = "Weld failure at KM 80.0 on the DN line."
_READY = "Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR."
_OTHER = "Weld failure at KM 90.0 on the DN line, severity 70, 4 days overdue, no TSR."


def _source(name: str, page: str) -> str:
    marker = f"function {name}("
    start = page.index(marker)
    depth = 0
    started = False
    for index, char in enumerate(page[start:], start=start):
        if char == "{":
            depth += 1
            started = True
        elif char == "}":
            depth -= 1
            if started and depth == 0:
                return page[start : index + 1]
    raise AssertionError(f"{name} is not closed")


def _master_ids(client: TestClient) -> list[str]:
    response = client.get("/api/master-data")
    assert response.status_code == 200
    return [item["task_id"] for item in response.json()["defects"]]


def test_dashboard_review_controls_keep_the_backlog_table() -> None:
    page = _PAGE.read_text(encoding="utf-8")
    nav = page.split('<div class="nav-bar">', 1)[1].split("</div>", 1)[0]
    officer = page.split('id="secOfficer"', 1)[1].split('id="secChief"', 1)[0]
    horizon = page.split('id="secHorizon"', 1)[1].split("<!-- /secHorizon -->", 1)[0]
    portal = page.split('id="secReport"', 1)[1].split("</div>\n\n  </div>", 1)[0]
    assert nav.index('id="tabHorizon"') < nav.index('id="tabReport"')
    assert "BDMS Report Portal" in nav
    assert "LOAD DEMO SET" not in officer
    assert "loadDemoBacklog" not in page
    assert 'id="reportFile"' not in officer
    assert 'id="btnSendReport"' not in officer
    assert "No report-derived defects have been applied yet. Upload a report in BDMS Report Portal." in officer
    assert 'id="appliedReportSummaries"' in officer
    assert 'id="defectBacklogBody"' in officer
    assert "NEXT MAINTENANCE CYCLE" in officer
    assert 'id="reportFile"' in portal
    assert 'id="btnSendReport"' in portal
    assert "SUBMIT REPORT" in portal
    assert 'id="btnSendReport"' in portal
    assert 'id="btnReviewReplace"' not in portal
    assert 'id="btnCancelReport"' not in portal
    assert 'id="btnConfirmApply"' not in portal
    assert "backlogEmptyState" in page
    assert "empty.hidden = (defects || []).length > 0" in page
    for header in (
        "Task ID",
        "Dept",
        "Chainage",
        "Maintenance Item",
        "Urgency",
        "ML ESCALATION FORECAST (DAYS)",
        "Required Duration",
        "Allocated Machine",
        "Manpower",
        "Bundling Cluster",
        "Review",
    ):
        assert f"<th>{header}</th>" in officer
    assert "btnSendReport" not in horizon
    assert "Completed Maintenance Report" in horizon
    assert "photograph" not in page.casefold()

    edits = _source("reportBuildEdits", page)
    assert 'draft.has_tsr === "yes"' in edits
    assert 'draft.has_tsr === "no"' in edits
    assert "edits.has_tsr = null" in edits
    assert 'has_tsr: tsr === true ? "yes" : tsr === false ? "no" : ""' in page
    assert '{ value: "", label: "Unknown" }' in page
    assert page.index('{ value: "", label: "Unknown" }') < page.index('{ value: "yes", label: "Yes" }')
    assert page.index('{ value: "yes", label: "Yes" }') < page.index('{ value: "no", label: "No" }')

    apply_fn = _source("reportApply", page)
    assert apply_fn.index("reportApplyIsConfirmed") < apply_fn.index("reloadBacklog")
    assert apply_fn.index("The report was not applied") < apply_fn.index("reloadBacklog")
    cancel_fn = _source("reportCancel", page)
    assert "/apply" not in cancel_fn
    assert "Report not submitted. The backlog was not changed." in cancel_fn
    assert "OCR_UNAVAILABLE" in page
    assert "PROVIDER_UNAVAILABLE" in page
    assert "POSSESSION_CONFLICT" in page
    assert "UNSUPPORTED_FORMAT" in page
    assert "FILE_TOO_LARGE" in page
    assert "/api/ingest/report/preview" in page
    assert "append_report_defects" in page
    assert "replace_open_backlog" not in page
    assert "applied_defect_id" in page
    assert "bdms-open-report" in page
    assert "Already applied as " in page
    assert "This ruled-out finding is not an operational defect." in page
    assert "This finding stays under review and cannot be applied." in page
    assert "Report submitted." in page
    assert "Report not submitted." in page
    assert "not a live model" in page
    assert "Accepted records are sent to the Divisional Maintenance Officer." in page
    confirmed = _source("reportApplyIsConfirmed", page)
    assert 'body.status !== "APPLIED"' in confirmed
    assert 'body.status !== "ALREADY_APPLIED"' in confirmed
    assert "applied_defects" in confirmed


def test_source_images_are_the_extracted_bytes_and_are_not_stored(workspace: dict[str, object]) -> None:
    del workspace
    _clear_possession_flags()
    before = _snapshot_json()
    jpeg = _jpeg_bytes()
    preview = preview_uploaded_report(
        _text_and_image_pdf("Inspection note without a defect sentence.", jpeg),
        "inspection.pdf",
        provider=_ScriptedProvider([]),
    )
    images = preview["source_images"]
    assert isinstance(images, list) and images
    image = images[0]
    assert image["reference"] == "page:1/image:1"
    assert image["page_number"] == 1
    assert image["media_type"] == "image/jpeg"
    assert image["data_url"].startswith("data:image/jpeg;base64,")
    assert base64.b64decode(image["data_url"].split(",", 1)[1]) == jpeg
    rendered = json.dumps(preview)
    assert "Temp" not in rendered
    assert "word/media" not in rendered
    stored = load_preview(str(preview["report_id"]))
    assert stored is not None
    assert "source_images" not in stored
    assert "data_url" not in json.dumps(stored)
    assert _snapshot_json() == before


def test_dashboard_preview_correction_apply_and_master_refresh(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    provider = _ScriptedProvider([_finding(_INCOMPLETE), _finding(_READY), _finding(_OTHER)])
    monkeypatch.setattr("report_service.build_report_provider", lambda: provider)
    with TestClient(app_module.app) as client:
        _clear_possession_flags()
        data_ingestion.clear_last_ingest()
        page = client.get("/")
        assert page.status_code == 200
        assert 'id="btnSendReport"' in page.text
        before = _master_ids(client)
        before_completed = [item["defect_id"] for item in client.get("/api/completed-log?limit=1000").json()]

        rejected = client.post(
            "/api/ingest/report/preview",
            files={"file": ("notes.txt", b"not a report", "text/plain")},
        )
        assert rejected.status_code == 415
        assert rejected.json()["detail"]["code"] == "UNSUPPORTED_FORMAT"
        assert "Nothing was added" in rejected.json()["detail"]["message"]
        assert _master_ids(client) == before

        preview = client.post(
            "/api/ingest/report/preview",
            files={"file": ("incomplete.pdf", _text_pdf([_INCOMPLETE]), "application/pdf")},
        )
        assert preview.status_code == 200
        body = preview.json()
        assert body["report_id"]
        assert body["summary"]
        candidate = body["candidates"][0]
        assert candidate["status"] == "NEEDS_REVIEW"
        assert "severity" in candidate["missing_fields"]
        assert "days_overdue" in candidate["missing_fields"]
        assert "has_tsr" in candidate["missing_fields"]
        assert candidate["evidence"]["excerpt"] == _INCOMPLETE
        assert candidate["evidence"]["references"] == ["page:1"]
        assert _master_ids(client) == before

        report_id = body["report_id"]
        candidate_id = candidate["candidate_id"]
        needs_review = client.post(
            f"/api/ingest/report/{report_id}/apply",
            json={"operation": "replace_open_backlog", "candidate_ids": [candidate_id]},
        )
        assert needs_review.status_code == 422
        assert "severity" in needs_review.json()["detail"]["message"]
        assert _master_ids(client) == before

        invalid = client.post(
            f"/api/ingest/report/{report_id}/apply",
            json={
                "operation": "replace_open_backlog",
                "candidate_ids": [candidate_id],
                "edits": {candidate_id: {"severity": 84, "days_overdue": 22, "has_tsr": False, "chainage_km": 400}},
            },
        )
        assert invalid.status_code == 422
        assert "outside the corridor" in invalid.json()["detail"]["message"]
        assert _master_ids(client) == before

        applied = client.post(
            f"/api/ingest/report/{report_id}/apply",
            json={
                "operation": "replace_open_backlog",
                "candidate_ids": [candidate_id],
                "edits": {candidate_id: {"severity": 84, "days_overdue": 22, "has_tsr": False}},
            },
        )
        assert applied.status_code == 200
        applied_body = applied.json()
        assert applied_body["status"] == "APPLIED"
        assert applied_body["applied_defects"]
        applied_ids = [item["defect_id"] for item in applied_body["applied_defects"]]
        assert _master_ids(client) == applied_ids
        assert _open_ids() == applied_ids
        completed = [item["defect_id"] for item in client.get("/api/completed-log?limit=1000").json()]
        assert completed == before_completed

        repeat = client.post(
            f"/api/ingest/report/{report_id}/apply",
            json={"operation": "replace_open_backlog", "candidate_ids": ["F-other"]},
        )
        assert repeat.status_code == 409
        assert repeat.json()["detail"]["code"] == "REPORT_ALREADY_APPLIED"
        assert _master_ids(client) == applied_ids
        same = client.post(
            f"/api/ingest/report/{report_id}/apply",
            json={"operation": "append_report_defects", "candidate_ids": [candidate_id]},
        )
        assert same.status_code == 200
        assert same.json()["status"] == "ALREADY_APPLIED"
        assert _master_ids(client) == applied_ids

        ready = client.post(
            "/api/ingest/report/preview",
            files={"file": ("ready.pdf", _text_pdf([_READY]), "application/pdf")},
        )
        assert ready.status_code == 200
        ready_body = ready.json()
        ready_id = ready_body["candidates"][0]["candidate_id"]
        conflict_edit = client.post(
            f"/api/ingest/report/{ready_body['report_id']}/apply",
            json={
                "operation": "replace_open_backlog",
                "candidate_ids": [ready_id],
                "edits": {ready_id: {"chainage_km": 400}},
            },
        )
        assert conflict_edit.status_code == 422
        assert _master_ids(client) == applied_ids

        reserved = applied_ids[0]
        backlog_state.reserve_defects([reserved])
        app_module.SYSTEM_STATE["sanctioned"] = True
        app_module.SYSTEM_STATE["possession"] = {"defect_ids": [reserved]}
        blocked_preview = client.post(
            "/api/ingest/report/preview",
            files={"file": ("other.pdf", _text_pdf([_OTHER]), "application/pdf")},
        )
        assert blocked_preview.status_code == 200
        assert blocked_preview.json()["replacement"]["blocked"] is True
        blocked_id = blocked_preview.json()["candidates"][0]["candidate_id"]
        blocked = client.post(
            f"/api/ingest/report/{blocked_preview.json()['report_id']}/apply",
            json={"operation": "replace_open_backlog", "candidate_ids": [blocked_id]},
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "POSSESSION_CONFLICT"
        assert "Nothing was changed" in blocked.json()["detail"]["message"]
        assert reserved in backlog_state.scheduled_defect_ids()
        assert _master_ids(client) == applied_ids


_WELD = "Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR."
_FRACTURE = "Rail fracture at KM 40.0 on the UP fast line, severity 92, 18 days overdue, TSR imposed."
_OHE = "OHE sag at KM 150.0 on the UP line, severity 76, 9 days overdue, no TSR."
_MARKUP = "Weld failure at KM 110.0 on the UP line, severity 70, 4 days overdue, no TSR <script>alert(1)</script>"


def _install_open(items: list[object]) -> None:
    backlog_state._ensure_loaded()
    with backlog_state._LOCK:
        backlog_state._ACTIVE = list(items)  # type: ignore[arg-type]
        backlog_state._save_snapshot()


def _evidence_snapshot(report_id: str) -> list[dict[str, object]]:
    return evidence_for_report(report_id)


def test_defect_evidence_lookup_returns_only_that_defect(
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del workspace
    monkeypatch.setenv("REPORT_AI_PROVIDER", "mock")
    monkeypatch.delenv("REPORT_AI_MOCK_FIXTURE", raising=False)
    _clear_possession_flags()
    _install_open([])
    data_ingestion.clear_last_ingest()
    with TestClient(app_module.app) as client:
        preview = client.post(
            "/api/ingest/report/preview",
            files={"file": ("three-defects.pdf", _text_pdf([_WELD, _FRACTURE, _OHE]), "application/pdf")},
        )
        assert preview.status_code == 200
        body = preview.json()
        ready = []
        for excerpt in (_WELD, _FRACTURE, _OHE):
            match = next(item for item in body["candidates"] if item["evidence"]["excerpt"] == excerpt)
            assert match["status"] == "READY_TO_APPLY"
            ready.append(match)
        applied = client.post(
            f"/api/ingest/report/{body['report_id']}/apply",
            json={"operation": "append_report_defects", "candidate_ids": [item["candidate_id"] for item in ready]},
        )
        assert applied.status_code == 200
        applied_rows = applied.json()["applied_defects"]
        assert len(applied_rows) == 3
        ids = [item["defect_id"] for item in applied_rows]
        before_open = _open_ids()
        before_snapshot = _snapshot_json()
        before_evidence = _evidence_snapshot(str(body["report_id"]))
        session = open_session()
        try:
            session.add(
                ReportEvidence(
                    report_id=str(body["report_id"]),
                    source_finding_id=str(ready[0]["candidate_id"]),
                    defect_id=ids[0],
                    filename="three-defects.pdf",
                    reference="page:1/note",
                    page_number=1,
                    excerpt="Second stored sentence for the weld only.",
                    statement="Second stored sentence for the weld only.",
                    extraction_method="native_text",
                    reported_defect_type=None,
                    normalized_defect_type="WELD_FAILURE",
                    reported_severity_text=None,
                    severity=84,
                    reported_track_line=None,
                    track_line="DN",
                    chainage_km=80.0,
                    reported_due_date=None,
                    days_overdue=22.0,
                    has_tsr=False,
                    department=None,
                    preview_status="READY_TO_APPLY",
                )
            )
            commit_session(session)
        finally:
            session.close()

        looked_up: list[dict[str, object]] = []
        for defect_id, excerpt in zip(ids, (_WELD, _FRACTURE, _OHE), strict=True):
            response = client.get(f"/api/defects/{defect_id}/evidence")
            assert response.status_code == 200
            payload = response.json()
            assert payload["defect_id"] == defect_id
            assert payload["evidence"]
            assert all(item["defect_id"] == defect_id for item in payload["evidence"])
            assert all(item["excerpt"] != "" for item in payload["evidence"])
            assert any(item["excerpt"] == excerpt for item in payload["evidence"])
            assert "summary" not in payload
            looked_up.append(payload)
        assert len(looked_up[0]["evidence"]) == 2
        assert [item["excerpt"] for item in looked_up[0]["evidence"]] == [
            _WELD,
            "Second stored sentence for the weld only.",
        ]
        assert all(item["reference"] for item in looked_up[0]["evidence"])
        for other in looked_up[1:]:
            assert _WELD not in [item["excerpt"] for item in other["evidence"]]
            assert "Second stored sentence" not in json.dumps(other)
        assert _open_ids() == before_open
        assert _snapshot_json() == before_snapshot
        stored_after = _evidence_snapshot(str(body["report_id"]))
        assert [item["excerpt"] for item in stored_after] == [item["excerpt"] for item in before_evidence] + [
            "Second stored sentence for the weld only."
        ]

        missing = client.get("/api/defects/NOT-A-DEFECT/evidence")
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "EVIDENCE_NOT_FOUND"
        plain = _make_defect("NO-EVIDENCE", 12.0, "UP_FAST", DefectType.SIGNAL_INTERLOCK, 40, days_overdue=1.0, has_tsr=False)
        _install_open([plain])
        empty = client.get("/api/defects/NO-EVIDENCE/evidence")
        assert empty.status_code == 200
        assert empty.json() == {"defect_id": "NO-EVIDENCE", "evidence": []}
        assert _open_ids() == ["NO-EVIDENCE"]

        marked = client.post(
            "/api/ingest/report/preview",
            files={"file": ("note<script>.pdf", _text_pdf([_MARKUP]), "application/pdf")},
        )
        assert marked.status_code == 200
        marked_body = marked.json()
        candidate = next(item for item in marked_body["candidates"] if "<script>" in item["evidence"]["excerpt"])
        saved = client.post(
            f"/api/ingest/report/{marked_body['report_id']}/apply",
            json={"operation": "append_report_defects", "candidate_ids": [candidate["candidate_id"]]},
        )
        assert saved.status_code == 200
        marked_id = saved.json()["applied_defects"][0]["defect_id"]
        shown = client.get(f"/api/defects/{marked_id}/evidence")
        assert shown.status_code == 200
        assert "application/json" in shown.headers["content-type"]
        row = shown.json()["evidence"][0]
        assert row["excerpt"] == _MARKUP
        assert row["filename"] == "note<script>.pdf"
        assert "<script>alert(1)</script>" in shown.text
        assert _open_ids() == ["NO-EVIDENCE", marked_id]


def test_officer_review_panel_is_bound_to_each_operational_row() -> None:
    page = _PAGE.read_text(encoding="utf-8")
    officer = page.split('id="secOfficer"', 1)[1].split('id="secChief"', 1)[0]
    portal = page.split('id="secReport"', 1)[1].split('id="reportReview"', 1)[0]
    renderer = _source("renderDefectTable", page)
    binder = _source("bindDefectReviewControls", page)
    opener = _source("openDefectReview", page)
    evidence = _source("paintDefectEvidence", page)
    planner = _source("paintDefectReview", page)
    closer = _source("closeDefectReview", page)
    assert "<th>Review</th>" in officer
    assert 'id="reportFile"' in portal
    assert 'id="btnSendReport"' in portal
    assert 'id="defectReviewModal"' in page
    assert "SCHEDULED" in renderer
    assert "CLUSTERED" in renderer
    assert "NEXT CYCLE" in renderer
    assert 'badge.textContent = "SCHEDULED"' in renderer
    assert 'button.textContent = "Review"' in renderer
    assert 'setAttribute("data-review-defect", String(d.task_id))' in renderer
    assert 'setAttribute("data-cluster-state", clusterState)' in renderer
    assert "addEventListener" not in renderer
    assert 'tbody.dataset.reviewBound === "1"' in binder
    assert 'tbody.addEventListener("click", onDefectReviewClick)' in binder
    assert 'event.key === "Escape"' in binder
    assert "encodeURIComponent(taskId)" in opener
    assert '"/api/defects/"' in opener
    assert "result.payload.defect_id !== taskId" in opener
    assert "row.priority_score" in planner
    assert "row.predicted_days_to_critical" in planner
    assert "row.required_duration_min" in planner
    assert "row.machine_id" in planner
    assert "row.crew_headcount" in planner
    assert "row.backlog_status" in planner
    assert "compute_priority_urgency_score" not in planner
    assert "predict_days_to_critical" not in planner
    assert 'return "RESERVED"' in page
    assert 'return "TARGET"' in page
    assert 'return "BACKLOG"' in page
    assert 'heading.textContent = "SOURCE"' in evidence
    assert 'note.textContent = "not a live model."' in evidence
    assert 'empty.textContent = "NONE"' in evidence
    assert "excerpt.textContent" in evidence
    assert "innerHTML" not in evidence
    assert 'error.textContent = "EVIDENCE UNAVAILABLE"' in page
    assert 'loading.textContent = "LOADING"' in planner
    assert "modal.hidden = true" in closer
    assert "body.replaceChildren()" in closer
    assert 'id="btnReviewReplace"' not in page
    assert 'id="btnCancelReport"' not in page
    assert "replace_open_backlog" not in page
