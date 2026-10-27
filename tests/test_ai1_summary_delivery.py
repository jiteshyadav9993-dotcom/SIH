"""Report summary delivery: local-text summary -> stored preview -> explicit apply -> DMO payload.

Isolated database only. The fictional demo PDF is read by the ordinary
local-text path with the scripted fixture disabled. The same summary string
must be present at generation (preview response), in storage, in the
/api/master-data payload the DMO paints, and in the reopened stored report.
A preview that was never applied is stored but is not an applied report, so
the DMO list omits it until the report is explicitly applied.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import data_ingestion
import defects
from defects import DEFECT_TYPE_SPEC, DefectType
from report_store import load_report
from tests.test_report_apply import _clear_possession_flags, _completed_ids, _open_ids, _snapshot_json, workspace
from tests.test_report_dashboard import _source
from tests.test_report_documents import _text_pdf
from tests.test_report_planner_integration import _apply, _install_open, _use_local_provider

_PDF = Path(__file__).resolve().parent / "fixtures" / "ai1_railway_maintenance_demo_report_4_pages.pdf"
_PAGE = Path(__file__).resolve().parents[1] / "index.html"
_SECOND = "Weld failure at KM 90.0 on the DN line, severity 70, 4 days overdue, no TSR."
_LABEL = re.compile(r"\b(?:FINDING|OBSERVATION)\s+([A-Z]-\d+)\b")
_SCRIPTED = "SCRIPTED MOCK RESPONSE - NOT A LIVE AI RESULT"


@pytest.fixture
def local_backlog(workspace: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    del workspace
    _use_local_provider(monkeypatch)
    _clear_possession_flags()
    _install_open([])
    data_ingestion.clear_last_ingest()


def _upload_demo(client: TestClient) -> dict[str, object]:
    with _PDF.open("rb") as handle:
        response = client.post(
            "/api/ingest/report/preview",
            files={"file": (_PDF.name, handle, "application/pdf")},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _by_label(preview: dict[str, object]) -> dict[str, dict[str, object]]:
    """Map the report's own finding labels (D-01, A-01, N-01) to the preview rows."""
    rows: dict[str, dict[str, object]] = {}
    candidates = preview["candidates"]
    ruled = preview["ruled_out"]
    assert isinstance(candidates, list) and isinstance(ruled, list)
    for item in candidates:
        assert isinstance(item, dict)
        match = _LABEL.search(str(item["fields"]["excerpt"]))
        assert match is not None, item["candidate_id"]
        rows[match.group(1)] = item
    for item in ruled:
        assert isinstance(item, dict)
        match = _LABEL.search(str(item["excerpt"]))
        assert match is not None, item["finding_id"]
        rows[match.group(1)] = item
    return rows


def _master(client: TestClient) -> dict[str, object]:
    response = client.get("/api/master-data")
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def _applied_summaries(client: TestClient) -> dict[str, dict[str, object]]:
    rows = _master(client)["applied_reports"]
    assert isinstance(rows, list)
    return {str(item["report_id"]): item for item in rows}


def test_pending_preview_is_stored_but_only_an_applied_report_reaches_the_dmo(local_backlog: None) -> None:
    del local_backlog
    before_completed = _completed_ids()
    before_snapshot = _snapshot_json()
    with TestClient(app_module.app) as client:
        preview = _upload_demo(client)
        report_id = str(preview["report_id"])
        generated = str(preview["summary"])
        provider = preview["provider"]
        assert isinstance(provider, dict)
        assert preview["provider_name"] == "local-text"
        assert provider["scripted"] is False
        assert provider["live"] is False
        assert _SCRIPTED not in generated
        assert "SIMULATED TEST DATA" not in generated

        lines = [line for line in generated.split(". ") if line]
        assert len(lines) == 5
        assert generated.endswith(".")
        rows = _by_label(preview)
        assert set(rows) == {"D-01", "D-02", "D-03", "A-01", "N-01"}
        for label in ("D-01", "D-02"):
            item = rows[label]
            assert item["status"] == "READY_TO_APPLY"
            assert item["missing_fields"] == []
            assert isinstance(item["scheduling_preview"], dict)
            assert str(item["fields"]["excerpt"]) not in generated
        assert rows["D-03"]["status"] == "NEEDS_REVIEW"
        assert rows["D-03"]["missing_fields"] == ["has_tsr"]
        assert rows["D-03"]["scheduling_preview"] is None
        assert rows["D-03"]["fields"]["has_tsr"]["value"] is None
        assert rows["A-01"]["status"] == "NEEDS_REVIEW"
        assert rows["A-01"]["fields"]["uncertainty"] == "high"
        assert rows["A-01"]["scheduling_preview"] is None
        assert rows["N-01"]["status"] == "RULED_OUT"
        assert str(rows["N-01"]["reason"]).startswith("negated_finding")
        assert "TSR unknown" in generated
        assert "No TSR" in generated
        assert "Ruled out" in generated

        stored = load_report(report_id)
        assert stored is not None
        assert stored["status"] == "pending"
        assert stored["summary"] == generated

        assert _open_ids() == []
        assert _completed_ids() == before_completed
        assert _snapshot_json() == before_snapshot
        pending_view = _master(client)
        assert pending_view["defects"] == []
        assert pending_view["applied_reports"] == []

        ready_ids = [str(rows[label]["candidate_id"]) for label in ("D-01", "D-02")]
        applied = _apply(client, report_id, ready_ids)
        assert applied.status_code == 200, applied.text
        body = applied.json()
        assert body["status"] == "APPLIED"
        defect_ids = [str(item["defect_id"]) for item in body["applied_defects"]]
        assert len(defect_ids) == 2
        assert _open_ids() == defect_ids
        assert _completed_ids() == before_completed

        shown = _applied_summaries(client)
        assert list(shown) == [report_id]
        listed = shown[report_id]
        assert listed["summary"] == generated
        assert listed["filename"] == _PDF.name
        assert listed["defect_ids"] == defect_ids
        assert listed["applied_at"] is not None

        after = load_report(report_id)
        assert after is not None
        assert after["status"] == "applied"
        assert after["summary"] == generated

        reopened = client.get(f"/api/ingest/report/{report_id}")
        assert reopened.status_code == 200
        stored_view = reopened.json()
        assert stored_view["summary"] == generated
        assert stored_view["stored_status"] == "applied"
        assert stored_view["applied_candidate_ids"] == ready_ids

        strips = {str(item["task_id"]): item for item in _master(client)["defects"]}
        assert set(strips) == set(defect_ids)
        active = {item.defect_id: item for item in data_ingestion.get_effective_defects()}
        for task_id, strip in strips.items():
            defect = active[task_id]
            assert not any("summary" in key for key in strip)
            assert strip["description"] == defect.defect_type.value.replace("_", " ").title()
            assert strip["priority_score"] == round(
                defects.compute_priority_urgency_score(
                    defect,
                    days_overdue=defect.days_overdue,
                    has_tsr=defect.has_tsr,
                    traffic_density=defects.estimate_traffic_density(defect),
                )
            )
            assert strip["required_duration_min"] == DEFECT_TYPE_SPEC[DefectType(defect.defect_type)][0]
            assert generated not in str(strip)

        evidence = client.get(f"/api/defects/{defect_ids[0]}/evidence")
        assert evidence.status_code == 200
        linked = evidence.json()["evidence"]
        assert len(linked) == 1
        assert linked[0]["excerpt"] == rows["D-01"]["fields"]["excerpt"]
        assert linked[0]["report_id"] == report_id

        repeat = _apply(client, report_id, ready_ids[:1])
        assert repeat.status_code == 200
        assert repeat.json()["status"] == "ALREADY_APPLIED"
        assert _open_ids() == defect_ids
        assert _applied_summaries(client)[report_id]["summary"] == generated

        for label, status_code in (("D-03", 422), ("A-01", 422)):
            refused = _apply(client, report_id, [str(rows[label]["candidate_id"])])
            assert refused.status_code == status_code, label
            assert _open_ids() == defect_ids
        negated = _apply(client, report_id, [str(rows["N-01"]["finding_id"])])
        assert negated.status_code == 400
        assert _open_ids() == defect_ids
        assert _completed_ids() == before_completed


def test_each_applied_report_keeps_its_own_summary_in_the_dmo_payload(local_backlog: None) -> None:
    del local_backlog
    with TestClient(app_module.app) as client:
        demo = _upload_demo(client)
        demo_id = str(demo["report_id"])
        demo_rows = _by_label(demo)
        first = _apply(client, demo_id, [str(demo_rows[label]["candidate_id"]) for label in ("D-01", "D-02")])
        assert first.status_code == 200
        demo_defects = [str(item["defect_id"]) for item in first.json()["applied_defects"]]

        other = client.post(
            "/api/ingest/report/preview",
            files={"file": ("second-weld-inspection.pdf", _text_pdf([_SECOND]), "application/pdf")},
        )
        assert other.status_code == 200, other.text
        second = other.json()
        second_id = str(second["report_id"])
        assert second_id != demo_id
        assert second["provider_name"] == "local-text"
        assert str(second["summary"]) != str(demo["summary"])
        assert _applied_summaries(client).keys() == {demo_id}

        candidate = second["candidates"][0]
        assert candidate["status"] == "READY_TO_APPLY"
        applied = _apply(client, second_id, [str(candidate["candidate_id"])])
        assert applied.status_code == 200, applied.text
        second_defects = [str(item["defect_id"]) for item in applied.json()["applied_defects"]]
        assert _open_ids() == demo_defects + second_defects

        shown = _applied_summaries(client)
        assert shown.keys() == {demo_id, second_id}
        assert shown[demo_id]["summary"] == demo["summary"]
        assert shown[second_id]["summary"] == second["summary"]
        assert shown[demo_id]["defect_ids"] == demo_defects
        assert shown[second_id]["defect_ids"] == second_defects
        assert _SECOND not in str(shown[second_id]["summary"])
        assert "KM 90 DN" in str(shown[second_id]["summary"])


def test_dmo_page_paints_the_stored_summary_from_master_data() -> None:
    page = _PAGE.read_text(encoding="utf-8")
    painter = _source("paintAppliedReports", page)
    assert 'getElementById("appliedReportSummaries")' in painter
    assert "report.summary" in painter
    assert _SCRIPTED in painter
    for forbidden in ("fetch(", "apiGet(", "innerHTML", "excerpt"):
        assert forbidden not in painter
    for loader in ("loadMasterData", "reloadBacklog"):
        source = _source(loader, page)
        assert 'apiGet("/api/master-data")' in source
        assert "paintAppliedReports(data.applied_reports)" in source
    assert "await reloadBacklog();" in _source("reportSend", page)
    assert "reportAcceptedIds" in _source("reportSend", page)
    assert "Report submitted." in page
    assert "Report not submitted." in page
    officer = page.split('id="secOfficer"', 1)[1].split('id="secChief"', 1)[0]
    summary_at = officer.index('id="appliedReportSummaries"')
    table_at = officer.index('<table class="maintenance-strips">')
    assert summary_at < table_at
    assert "summary" not in _source("maintenanceStrip", page)
