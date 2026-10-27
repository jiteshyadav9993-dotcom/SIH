"""DMO maintenance strips are built from existing computed outputs. Isolated database only."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import data_ingestion
import defects
import ml_priority
from defects import DEFECT_TYPE_SPEC, DefectType, MachineType
from tests.test_report_apply import _clear_possession_flags, _open_ids, workspace
from tests.test_report_dashboard import _source
from tests.test_report_documents import _text_pdf
from tests.test_report_planner_integration import _apply, _candidate, _install_open, _upload, _use_local_provider

_PAGE = Path(__file__).resolve().parents[1] / "index.html"
_WELD = (
    "Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR, "
    "estimated duration 15 minutes."
)
_FRACTURE = "Rail fracture at KM 40.0 on the UP fast line, severity 92, 18 days overdue, TSR imposed."
_OHE = "OHE sag at KM 150.0 on the UP line, severity 76, 9 days overdue, no TSR."
_INCOMPLETE = "Signal interlock at KM 9.4 on the UP fast line."
_UNCERTAIN = "Possible weld failure at KM 67.5 on the DN line, severity 84, 22 days overdue, no TSR."
_NEGATED = "No rail fracture was observed at KM 12.0 on the UP fast line."
_PAGES = (_WELD, _FRACTURE, _OHE, _INCOMPLETE, _UNCERTAIN, _NEGATED)
_SEVERITY = {"WELD_FAILURE": 84, "RAIL_FRACTURE": 92, "OHE_SAG": 76}
_SENTENCE = {"WELD_FAILURE": _WELD, "RAIL_FRACTURE": _FRACTURE, "OHE_SAG": _OHE}
_STRIP_KEYS = (
    "source",
    "chainage_km",
    "track_line",
    "description",
    "priority_score",
    "predicted_days_to_critical",
    "required_duration_min",
    "crew_headcount",
)


@pytest.fixture
def local_backlog(workspace: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    del workspace
    _use_local_provider(monkeypatch)
    _clear_possession_flags()
    _install_open([])
    data_ingestion.clear_last_ingest()


def test_strip_builder_maps_only_master_data_fields() -> None:
    page = _PAGE.read_text(encoding="utf-8")
    builder = _source("maintenanceStrip", page)
    renderer = _source("renderDefectTable", page)
    officer = page.split('id="secOfficer"', 1)[1].split('id="secChief"', 1)[0]
    for field in (
        "d.task_id",
        "d.source",
        "d.chainage_km",
        "d.track_line",
        "d.description",
        "d.priority_score",
        "d.predicted_days_to_critical",
        "d.required_duration_min",
        "d.machine_id",
        "d.crew_headcount",
    ):
        assert field in builder
    for forbidden in ("severity", "summary", "excerpt", "compute_priority", "predict_days", "fetch("):
        assert forbidden not in builder
    assert "maintenanceStrip(d, clusterState)" in renderer
    assert 'tr.className = "maintenance-strip"' in renderer
    for label in ('"Dept"', '"Location"', '"Defect"', '"F1 urgency"', '"ML forecast (d)"', '"Block"', '"Machine"', '"Crew"', '"Review"'):
        assert label in renderer
    assert 'button.setAttribute("data-review-defect", String(d.task_id))' in renderer
    for forbidden in ("summary", "excerpt", "innerHTML"):
        assert forbidden not in renderer
    assert '<table class="maintenance-strips">' in officer
    assert 'id="appliedReportSummaries"' in officer
    assert ".maintenance-strip td { padding: 4px 8px; white-space: nowrap;" in page
    assert "content: attr(data-label);" in page


def test_each_strip_shows_its_own_computed_values_and_evidence(local_backlog: None) -> None:
    del local_backlog
    with TestClient(app_module.app) as client:
        preview = _upload(client, _PAGES, "dmo-strip-inspection.pdf")
        ready = [_candidate(preview, text) for text in (_WELD, _FRACTURE, _OHE)]
        applied = _apply(client, str(preview["report_id"]), [str(item["candidate_id"]) for item in ready])
        assert applied.status_code == 200
        defect_ids = [item["defect_id"] for item in applied.json()["applied_defects"]]
        assert _open_ids() == defect_ids

        payload = client.get("/api/master-data").json()
        rows = {item["task_id"]: item for item in payload["defects"]}
        assert set(rows) == set(defect_ids)
        active = {item.defect_id: item for item in data_ingestion.get_effective_defects()}
        summary = str(payload["applied_reports"][0]["summary"])
        assert summary

        distinct_from_severity = 0
        for task_id, shown in rows.items():
            defect = active[task_id]
            for key in _STRIP_KEYS:
                assert shown[key] is not None, key
            assert "machine_id" in shown
            assert shown["source"] == app_module._SOURCE_BY_DEFECT_TYPE[defect.defect_type]
            assert shown["chainage_km"] == defect.km_location
            assert shown["track_line"] == defect.track_line
            assert shown["description"] == defect.defect_type.value.replace("_", " ").title()
            formula_1 = defects.compute_priority_urgency_score(
                defect,
                days_overdue=defect.days_overdue,
                has_tsr=defect.has_tsr,
                traffic_density=defects.estimate_traffic_density(defect),
            )
            assert shown["priority_score"] == round(formula_1)
            if shown["priority_score"] != _SEVERITY[defect.defect_type.value]:
                distinct_from_severity += 1
            assert shown["predicted_days_to_critical"] == round(ml_priority.predict_days_to_critical(defect), 1)
            assert shown["required_duration_min"] == DEFECT_TYPE_SPEC[defect.defect_type][0]
            assert shown["required_duration_min"] == int(defect.required_block_min)
            expected_machine = None if defect.required_machine is MachineType.NONE else defect.required_machine.value
            assert shown["machine_id"] == expected_machine
            assert shown["crew_headcount"] == app_module._crew_headcount(defect)
            assert shown["backlog_status"] == "OPEN"
            for key in _STRIP_KEYS:
                assert len(str(shown[key])) <= 40, key
                assert str(shown[key]) != summary

            evidence = client.get(f"/api/defects/{task_id}/evidence")
            assert evidence.status_code == 200
            body = evidence.json()
            assert body["defect_id"] == task_id
            linked = body["evidence"]
            assert linked
            for item in linked:
                assert item["defect_id"] == task_id
                assert item["excerpt"] == _SENTENCE[defect.defect_type.value]
                assert item["severity"] == _SEVERITY[defect.defect_type.value]
                assert item["filename"] == "dmo-strip-inspection.pdf"
                assert item["reference"].startswith("page:")
                assert item["normalized_defect_type"] == defect.defect_type.value
                for other_type, sentence in _SENTENCE.items():
                    if other_type != defect.defect_type.value:
                        assert item["excerpt"] != sentence
        assert distinct_from_severity >= 1

        weld_id = next(task_id for task_id, defect in active.items() if defect.defect_type is DefectType.WELD_FAILURE)
        assert rows[weld_id]["required_duration_min"] == DEFECT_TYPE_SPEC[DefectType.WELD_FAILURE][0]
        assert rows[weld_id]["required_duration_min"] != 15

        shown_descriptions = {item["description"] for item in payload["defects"]}
        assert "Signal Interlock" not in shown_descriptions
        assert len(payload["defects"]) == 3
        for text in (_INCOMPLETE, _UNCERTAIN, _NEGATED):
            assert all(text != item["excerpt"] for row_id in rows for item in client.get(f"/api/defects/{row_id}/evidence").json()["evidence"])


def test_infeasible_plan_is_labelled_no_window() -> None:
    page = _PAGE.read_text(encoding="utf-8")
    painted = _source("paintPlanMetrics", page)
    assert 'sharedState.lastOptimizeStatus === "INFEASIBLE"' in painted
    assert "NO WINDOW" in painted
    assert "No safe gap in the next week" in painted
    applied = _source("applyOptimizeResult", page)
    assert "lastOptimizeStatus" in applied
