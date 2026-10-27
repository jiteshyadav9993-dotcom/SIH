"""Report defects reach the existing planner only after an explicit apply.

Preview does not plan. Apply writes TrackDefect rows. /api/master-data
reads them and calculates Formula 1 and the ML forecast. /api/optimize and
/api/horizon-plan bundle the unreserved rows and call the existing solver.
A solver result does not reserve a possession.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app as app_module
import backlog_state
import data_ingestion
import defects
import ml_priority
import solver
from defects import DEFECT_TYPE_SPEC, DefectType
from report_service import evidence_for_report
from solver import SolverResult
from tests.test_report_apply import _clear_possession_flags, _completed_ids, _open_ids, workspace
from tests.test_report_documents import _text_pdf

_WELD = "Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR."
_FRACTURE = "Rail fracture at KM 40.0 on the UP fast line, severity 92, 18 days overdue, TSR imposed."
_OHE = "OHE sag at KM 150.0 on the UP line, severity 76, 9 days overdue, no TSR."
_INCOMPLETE = "Signal interlock at KM 9.4 on the UP fast line."
_UNCERTAIN = "Possible weld failure at KM 67.5 on the DN line, severity 84, 22 days overdue, no TSR."
_NEGATED = "No rail fracture was observed at KM 12.0 on the UP fast line."
_NARRATIVE = "The inspection team reached the site at 09:30."
_NEARBY = "Weld failure at KM 81.0 on the DN line, severity 70, 4 days overdue, no TSR."
_PAGES = (_WELD, _FRACTURE, _OHE, _INCOMPLETE, _UNCERTAIN, _NEGATED, _NARRATIVE)


def _install_open(items: list[object]) -> None:
    backlog_state._ensure_loaded()
    with backlog_state._LOCK:
        backlog_state._ACTIVE = list(items)  # type: ignore[arg-type]
        backlog_state._save_snapshot()


def _use_local_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_AI_PROVIDER", "mock")
    monkeypatch.delenv("REPORT_AI_MOCK_FIXTURE", raising=False)
    monkeypatch.delenv("REPORT_AI_API_KEY", raising=False)


def _reset_backlog() -> None:
    _clear_possession_flags()
    _install_open([])
    data_ingestion.clear_last_ingest()


def _upload(client: TestClient, pages: tuple[str, ...] | list[str], filename: str) -> dict[str, object]:
    response = client.post(
        "/api/ingest/report/preview",
        files={"file": (filename, _text_pdf(list(pages)), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _apply(client: TestClient, report_id: str, candidate_ids: list[str]) -> object:
    return client.post(
        f"/api/ingest/report/{report_id}/apply",
        json={"operation": "append_report_defects", "candidate_ids": candidate_ids},
    )


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


def _ready_fields(item: dict[str, object], defect_type: str, line: str, chainage: float, severity: int, overdue: float, has_tsr: bool) -> None:
    evidence = item["evidence"]
    scheduling = item["scheduling_preview"]
    assert isinstance(evidence, dict) and isinstance(scheduling, dict)
    assert item["status"] == "READY_TO_APPLY"
    assert item["missing_fields"] == []
    assert evidence["normalized_defect_type"] == defect_type
    assert evidence["track_line"] == line
    assert evidence["chainage_km"] == chainage
    assert evidence["severity"] == severity
    assert evidence["days_overdue"] == overdue
    assert evidence["has_tsr"] is has_tsr
    assert str(scheduling["defect_id"]).startswith("AI1-")
    assert scheduling["required_block_min"] == DEFECT_TYPE_SPEC[DefectType(defect_type)][0]


@pytest.fixture
def local_backlog(workspace: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    del workspace
    _use_local_provider(monkeypatch)
    _reset_backlog()


def test_unfamiliar_report_separates_ready_review_and_ruled_out_findings(local_backlog: None) -> None:
    del local_backlog
    before_completed = _completed_ids()
    with TestClient(app_module.app) as client:
        empty = client.get("/api/optimize")
        assert empty.status_code == 200
        assert empty.json()["status"] == "NO_DEFECTS"
        assert empty.json()["target_bundle"] is None
        assert empty.json()["candidate_options"] == []

        preview = _upload(client, _PAGES, "unfamiliar-section-inspection.pdf")
        assert preview["provider_name"] == "local-text"
        provider = preview["provider"]
        assert isinstance(provider, dict)
        assert provider["scripted"] is False
        assert provider["live"] is False
        assert provider["local_text"] is True
        summary = str(preview["summary"])
        assert "SCRIPTED MOCK" not in summary
        assert "Weld failure · KM 80 DN · Severity 84 · 22 days overdue · No TSR." in summary
        assert "Negative rail fracture observation · Ruled out; not a maintenance job." in summary
        assert _WELD not in summary
        assert _NEGATED not in summary
        assert _NARRATIVE not in summary
        assert "09:30" not in summary

        weld = _candidate(preview, _WELD)
        fracture = _candidate(preview, _FRACTURE)
        ohe = _candidate(preview, _OHE)
        incomplete = _candidate(preview, _INCOMPLETE)
        uncertain = _candidate(preview, _UNCERTAIN)
        _ready_fields(weld, "WELD_FAILURE", "DN", 80.0, 84, 22.0, False)
        _ready_fields(fracture, "RAIL_FRACTURE", "UP_FAST", 40.0, 92, 18.0, True)
        _ready_fields(ohe, "OHE_SAG", "UP", 150.0, 76, 9.0, False)
        assert weld["evidence"]["references"] == ["page:1"]
        assert fracture["evidence"]["references"] == ["page:2"]
        assert ohe["evidence"]["references"] == ["page:3"]
        assert incomplete["status"] == "NEEDS_REVIEW"
        assert incomplete["scheduling_preview"] is None
        assert incomplete["missing_fields"] == ["severity", "days_overdue", "has_tsr"]
        assert uncertain["status"] == "NEEDS_REVIEW"
        assert uncertain["scheduling_preview"] is None
        assert uncertain["evidence"]["normalized_defect_type"] == "WELD_FAILURE"
        assert any("ambiguous" in str(reason) for reason in uncertain["reasons"])
        ruled = preview["ruled_out"]
        assert isinstance(ruled, list) and len(ruled) == 1
        assert ruled[0]["excerpt"] == _NEGATED
        assert ruled[0]["status"] == "RULED_OUT"
        assert _open_ids() == []

        report_id = str(preview["report_id"])
        for candidate_id, status_code in (
            (str(incomplete["candidate_id"]), 422),
            (str(uncertain["candidate_id"]), 422),
            (str(ruled[0]["finding_id"]), 400),
        ):
            refused = _apply(client, report_id, [candidate_id])
            assert refused.status_code == status_code
            assert _open_ids() == []
        assert _completed_ids() == before_completed


def test_three_approved_defects_enter_the_backlog_once_with_linked_evidence(local_backlog: None) -> None:
    del local_backlog
    before_completed = _completed_ids()
    catalog = {item.defect_id for item in defects.get_default_defects()}
    with TestClient(app_module.app) as client:
        preview = _upload(client, _PAGES, "three-defect-inspection.pdf")
        ready = [_candidate(preview, text) for text in (_WELD, _FRACTURE, _OHE)]
        expected_ids = [str(item["scheduling_preview"]["defect_id"]) for item in ready]
        assert len(set(expected_ids)) == 3
        applied = _apply(client, str(preview["report_id"]), [str(item["candidate_id"]) for item in ready])
        assert applied.status_code == 200
        body = applied.json()
        assert body["status"] == "APPLIED"
        assert body["completed_history_preserved"] is True
        assert [item["defect_id"] for item in body["applied_defects"]] == expected_ids
        assert _open_ids() == expected_ids
        assert catalog.isdisjoint(_open_ids())
        assert _completed_ids() == before_completed

        stored = {str(item["source_finding_id"]): item for item in evidence_for_report(str(preview["report_id"]))}
        for candidate, defect_id, excerpt in zip(ready, expected_ids, (_WELD, _FRACTURE, _OHE), strict=True):
            row = stored[str(candidate["candidate_id"])]
            assert row["defect_id"] == defect_id
            assert row["excerpt"] == excerpt
        assert stored[str(_candidate(preview, _INCOMPLETE)["candidate_id"])]["defect_id"] is None
        assert stored[str(_candidate(preview, _UNCERTAIN)["candidate_id"])]["defect_id"] is None

        repeat = _apply(client, str(preview["report_id"]), [str(item["candidate_id"]) for item in ready])
        assert repeat.status_code == 200
        assert repeat.json()["status"] == "ALREADY_APPLIED"
        assert _open_ids() == expected_ids
        assert _completed_ids() == before_completed

        nearby = _upload(client, (_NEARBY,), "nearby-weld.pdf")
        duplicate = _candidate(nearby, _NEARBY)
        assert duplicate["status"] == "POSSIBLE_DUPLICATE"
        assert expected_ids[0] in duplicate["duplicate_of"]
        assert duplicate["scheduling_preview"]["defect_id"] not in _open_ids()
        assert _open_ids() == expected_ids


def test_approved_defects_reach_formula_1_ml_formula_2_and_the_solver(local_backlog: None, monkeypatch: pytest.MonkeyPatch) -> None:
    del local_backlog
    with TestClient(app_module.app) as client:
        preview = _upload(client, _PAGES, "planner-inspection.pdf")
        ready = [_candidate(preview, text) for text in (_WELD, _FRACTURE, _OHE)]
        applied = _apply(client, str(preview["report_id"]), [str(item["candidate_id"]) for item in ready])
        assert applied.status_code == 200
        defect_ids = [item["defect_id"] for item in applied.json()["applied_defects"]]

        master = client.get("/api/master-data")
        assert master.status_code == 200
        payload = master.json()
        active = list(data_ingestion.get_effective_defects())
        assert [item.defect_id for item in active] == defect_ids
        rows = {item["task_id"]: item for item in payload["defects"]}
        assert set(rows) == set(defect_ids)
        for defect in active:
            shown = rows[defect.defect_id]
            density = defects.estimate_traffic_density(defect)
            score = defects.compute_priority_urgency_score(
                defect,
                days_overdue=defect.days_overdue,
                has_tsr=defect.has_tsr,
                traffic_density=density,
            )
            assert shown["priority_score"] == round(score)
            assert shown["predicted_days_to_critical"] == round(ml_priority.predict_days_to_critical(defect), 1)
            assert shown["chainage_km"] == defect.km_location
            assert shown["track_line"] == defect.track_line
            assert shown["required_duration_min"] == int(defect.required_block_min)
            assert shown["backlog_status"] == "OPEN"
            assert defect.defect_id not in backlog_state.scheduled_defect_ids()

        expected_bundles = list(defects.cluster_defects_into_bundles(active))
        assert len(expected_bundles) == 3
        shown_groups = [tuple(task["task_id"] for task in bundle["tasks"]) for bundle in payload["bundles"]]
        assert shown_groups == [tuple(item.defect_id for item in bundle.defects) for bundle in expected_bundles]
        target = max(expected_bundles, key=lambda bundle: bundle.urgency_score)
        target_ids = {item.defect_id for item in target.defects}
        other_ids = set(defect_ids) - target_ids
        assert other_ids

        optimize_calls: list[tuple[str, ...]] = []
        real_solve = solver.solve_corridor_schedule

        def _record_optimize(bundles: object, trains: object, **kwargs: object) -> SolverResult:
            assert isinstance(bundles, list)
            optimize_calls.append(tuple(item.defect_id for bundle in bundles for item in bundle.defects))
            return real_solve(bundles, trains, **kwargs)

        monkeypatch.setattr(solver, "solve_corridor_schedule", _record_optimize)
        optimized = client.get("/api/optimize")
        assert optimized.status_code == 200
        plan = optimized.json()
        assert plan["status"] in {"SUCCESS", "INFEASIBLE"}
        assert plan["target_bundle"]["bundle_id"] == target.bundle_id
        assert [task["task_id"] for task in plan["target_bundle"]["tasks"]] == [item.defect_id for item in target.defects]
        assert optimize_calls
        assert all(set(call) == target_ids for call in optimize_calls)
        assert all(other_id not in call for call in optimize_calls for other_id in other_ids)
        assert backlog_state.scheduled_defect_ids().isdisjoint(defect_ids)
        refreshed = {item["task_id"]: item for item in client.get("/api/master-data").json()["defects"]}
        assert all(refreshed[defect_id]["backlog_status"] == "OPEN" for defect_id in defect_ids)

        horizon_calls: list[tuple[str, ...]] = []

        def _record_horizon(bundles: object, trains: object, **kwargs: object) -> SolverResult:
            assert isinstance(bundles, list)
            horizon_calls.append(tuple(item.defect_id for bundle in bundles for item in bundle.defects))
            return real_solve(bundles, trains, **kwargs)

        import calendar_plan

        monkeypatch.setattr(calendar_plan, "solve_corridor_schedule", _record_horizon)
        horizon = client.get("/api/horizon-plan?days=1")
        assert horizon.status_code == 200
        horizon_body = horizon.json()
        assert horizon_body["total_bundles"] == 3
        assert horizon_body["scheduled_bundles"] + len(horizon_body["unscheduled_bundle_ids"]) == 3
        known_bundles = {bundle.bundle_id: tuple(item.defect_id for item in bundle.defects) for bundle in expected_bundles}
        assert set(horizon_body["unscheduled_bundle_ids"]) <= set(known_bundles)
        for day in horizon_body["days"]:
            assert set(day["assigned_bundle_ids"]) <= set(known_bundles)
        reached = {defect_id for call in horizon_calls for defect_id in call}
        for bundle_id in horizon_body["unscheduled_bundle_ids"]:
            reached.update(known_bundles[bundle_id])
        assert reached == set(defect_ids)
        assert backlog_state.scheduled_defect_ids().isdisjoint(defect_ids)


def test_reserved_and_infeasible_results_do_not_become_new_schedules(local_backlog: None, monkeypatch: pytest.MonkeyPatch) -> None:
    del local_backlog
    with TestClient(app_module.app) as client:
        preview = _upload(client, (_WELD, _INCOMPLETE), "single-weld-inspection.pdf")
        weld = _candidate(preview, _WELD)
        incomplete = _candidate(preview, _INCOMPLETE)
        applied = _apply(client, str(preview["report_id"]), [str(weld["candidate_id"])])
        assert applied.status_code == 200
        defect_id = applied.json()["applied_defects"][0]["defect_id"]
        assert client.get("/api/master-data").json()["defects"][0]["backlog_status"] == "OPEN"

        backlog_state.reserve_defects([defect_id])
        reserved = client.get("/api/master-data")
        assert reserved.json()["defects"][0]["backlog_status"] == "SCHEDULED"
        blocked = client.get("/api/optimize")
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == "Every open defect is already scheduled."
        assert _open_ids() == [defect_id]
        assert defect_id in backlog_state.scheduled_defect_ids()
        refused = _apply(client, str(preview["report_id"]), [str(incomplete["candidate_id"])])
        assert refused.status_code == 422
        assert _open_ids() == [defect_id]
        again = _apply(client, str(preview["report_id"]), [str(weld["candidate_id"])])
        assert again.status_code == 200
        assert again.json()["status"] == "ALREADY_APPLIED"
        assert _open_ids() == [defect_id]

        backlog_state.release_defects([defect_id])
        calls: list[tuple[str, ...]] = []

        def _infeasible(bundles: object, trains: object, **kwargs: object) -> SolverResult:
            del trains, kwargs
            assert isinstance(bundles, list)
            calls.append(tuple(item.defect_id for bundle in bundles for item in bundle.defects))
            return SolverResult("INFEASIBLE", 0.0, (), (), float("inf"), float("inf"))

        monkeypatch.setattr(solver, "solve_corridor_schedule", _infeasible)
        infeasible = client.get("/api/optimize")
        assert infeasible.status_code == 200
        body = infeasible.json()
        assert body["status"] == "INFEASIBLE"
        assert body["candidate_options"] == []
        assert body["target_bundle"]["tasks"] == [{"task_id": defect_id}]
        assert calls
        assert all(call == (defect_id,) for call in calls)
        assert defect_id not in backlog_state.scheduled_defect_ids()
        assert client.get("/api/master-data").json()["defects"][0]["backlog_status"] == "OPEN"
        assert incomplete["scheduling_preview"] is None
        assert _open_ids() == [defect_id]
