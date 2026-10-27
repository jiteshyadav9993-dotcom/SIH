"""Architecture flow: submit a report, accept only ready findings, plan them, transmit the dossier.

The BDMS portal submits. Accepted records pass through the existing Formula 1,
Formula 2, ML forecast and CP-SAT path. The DMO reads that result. The section
controller receives the same dossier transmission as before. Isolated database only.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import app as app_module
import backlog_state
import data_ingestion
import defects
import ml_priority
from defects import DEFECT_TYPE_SPEC, DefectType
from tests.test_ai1_summary_delivery import _applied_summaries, _by_label, _upload_demo, local_backlog
from tests.test_report_apply import _completed_ids, _open_ids, workspace

__all__ = ["local_backlog", "workspace"]


def test_demo_report_reaches_the_planner_and_the_controller(local_backlog: None) -> None:
    del local_backlog
    before_completed = _completed_ids()
    with TestClient(app_module.app) as client:
        preview = _upload_demo(client)
        assert _open_ids() == []
        rows = _by_label(preview)
        ready = [str(rows[label]["candidate_id"]) for label in ("D-01", "D-02")]
        held_back = [str(rows["D-03"]["candidate_id"]), str(rows["A-01"]["candidate_id"]), str(rows["N-01"]["finding_id"])]
        report_id = str(preview["report_id"])

        applied = client.post(
            f"/api/ingest/report/{report_id}/apply",
            json={"operation": "append_report_defects", "candidate_ids": ready},
        )
        assert applied.status_code == 200, applied.text
        body = applied.json()
        assert body["status"] == "APPLIED"
        defect_ids = [str(item["defect_id"]) for item in body["applied_defects"]]
        assert len(defect_ids) == 2
        assert _open_ids() == defect_ids

        for candidate_id in held_back[:2]:
            refused = client.post(
                f"/api/ingest/report/{report_id}/apply",
                json={"operation": "append_report_defects", "candidate_ids": [candidate_id]},
            )
            assert refused.status_code == 422
        ruled = client.post(
            f"/api/ingest/report/{report_id}/apply",
            json={"operation": "append_report_defects", "candidate_ids": [held_back[2]]},
        )
        assert ruled.status_code == 400
        assert _open_ids() == defect_ids

        master = client.get("/api/master-data")
        assert master.status_code == 200
        payload = master.json()
        listed = _applied_summaries(client)
        assert listed[report_id]["summary"] == preview["summary"]
        strips = {str(item["task_id"]): item for item in payload["defects"]}
        assert set(strips) == set(defect_ids)
        active = {item.defect_id: item for item in data_ingestion.get_effective_defects()}
        for task_id, strip in strips.items():
            defect = active[task_id]
            score = defects.compute_priority_urgency_score(
                defect,
                days_overdue=defect.days_overdue,
                has_tsr=defect.has_tsr,
                traffic_density=defects.estimate_traffic_density(defect),
            )
            assert strip["priority_score"] == round(score)
            assert strip["predicted_days_to_critical"] == round(ml_priority.predict_days_to_critical(defect), 1)
            assert strip["required_duration_min"] == DEFECT_TYPE_SPEC[DefectType(defect.defect_type)][0]
            assert strip["description"] == defect.defect_type.value.replace("_", " ").title()
            evidence = client.get(f"/api/defects/{task_id}/evidence")
            assert evidence.status_code == 200
            assert evidence.json()["evidence"][0]["report_id"] == report_id

        bundles = payload["bundles"]
        assert isinstance(bundles, list) and bundles
        planned_ids = {task["task_id"] for bundle in bundles for task in bundle["tasks"]}
        assert set(defect_ids) <= planned_ids

        optimized = client.get("/api/optimize")
        assert optimized.status_code == 200
        plan = optimized.json()
        assert plan["status"] in {"SUCCESS", "INFEASIBLE"}
        target_tasks = [task["task_id"] for task in plan["target_bundle"]["tasks"]]
        assert target_tasks
        assert set(target_tasks) <= set(defect_ids)
        if plan["status"] == "SUCCESS":
            assert plan["candidate_options"]
            assert all(option["solver_status"] in {"OPTIMAL", "FEASIBLE"} for option in plan["candidate_options"])

        transmitted = client.post("/api/officer/submit", json={"plan_type": "PUNCTUALITY_FIRST"})
        assert transmitted.status_code == 200, transmitted.text
        dossier = transmitted.json()
        assert dossier["status"] == "SUBMITTED"
        assert dossier["system_state"]["dossier_submitted"] is True
        assert dossier["system_state"]["sanctioned"] is False
        again = client.post("/api/officer/submit", json={"plan_type": "PUNCTUALITY_FIRST"})
        assert again.json()["status"] == "ALREADY_SUBMITTED"
        assert _open_ids() == defect_ids
        assert _completed_ids() == before_completed

        if plan["status"] == "SUCCESS":
            option = plan["candidate_options"][0]
            sanctioned = client.post(
                "/api/chief/sanction",
                json={
                    "plan_type": "PUNCTUALITY_FIRST",
                    "target_line": plan["target_bundle"]["track_line"],
                    "window_index": option["window_index"],
                },
            )
            assert sanctioned.status_code == 200, sanctioned.text
            order = sanctioned.json()
            assert order["status"] in {"SANCTIONED", "INFEASIBLE"}
            if order["status"] == "SANCTIONED":
                assert order["possession"]["defect_ids"]
                assert set(order["possession"]["defect_ids"]) <= set(defect_ids)
                assert order["system_state"]["sanctioned"] is True


_WELD = "Weld failure at KM 80.0 on the DN line, severity 84, 22 days overdue, no TSR."


def test_schedulable_record_is_sanctioned_and_can_be_withdrawn(local_backlog: None) -> None:
    """A record that fits a night window must travel through the controller and back."""
    del local_backlog
    from tests.test_report_planner_integration import _apply, _candidate, _upload

    before_completed = _completed_ids()
    with TestClient(app_module.app) as client:
        preview = _upload(client, (_WELD,), "single-weld-block.pdf")
        assert _open_ids() == []
        ready = _candidate(preview, _WELD)
        applied = _apply(client, str(preview["report_id"]), [str(ready["candidate_id"])])
        assert applied.status_code == 200, applied.text
        body = applied.json()
        assert body["status"] == "APPLIED"
        defect_id = body["applied_defects"][0]["defect_id"]

        master = client.get("/api/master-data")
        assert master.status_code == 200
        payload = master.json()
        listed = _applied_summaries(client)
        assert listed[str(preview["report_id"])]["summary"] == preview["summary"]
        strips = payload["defects"]
        assert len(strips) == 1
        assert strips[0]["task_id"] == defect_id
        assert "summary" not in strips[0]

        optimized = client.get("/api/optimize")
        assert optimized.status_code == 200, optimized.text
        plan = optimized.json()
        assert plan["status"] == "SUCCESS", plan
        assert plan["candidate_options"]
        assert [task["task_id"] for task in plan["target_bundle"]["tasks"]] == [defect_id]
        option = plan["candidate_options"][0]
        assert option["solver_status"] in {"OPTIMAL", "FEASIBLE"}

        app_module.SYSTEM_STATE["dossier_submitted"] = False
        app_module.SYSTEM_STATE["reject_reason"] = None
        transmitted = client.post("/api/officer/submit", json={"plan_type": option["profile"]})
        assert transmitted.status_code == 200, transmitted.text
        assert transmitted.json()["status"] == "SUBMITTED"
        assert defect_id not in backlog_state.scheduled_defect_ids()

        sanctioned = client.post(
            "/api/chief/sanction",
            json={
                "plan_type": option["profile"],
                "target_line": plan["target_bundle"]["track_line"],
                "window_index": option["window_index"],
            },
        )
        assert sanctioned.status_code == 200, sanctioned.text
        order = sanctioned.json()
        assert order["status"] == "SANCTIONED", order
        assert order["possession"]["defect_ids"] == [defect_id]
        assert order["system_state"]["sanctioned"] is True
        assert defect_id in backlog_state.scheduled_defect_ids()
        assert client.get("/api/master-data").json()["defects"][0]["backlog_status"] == "SCHEDULED"
        assert _completed_ids() == before_completed

        withdrawn = client.post("/api/chief/withdraw")
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json()["status"] == "WITHDRAWN"
        assert withdrawn.json()["system_state"]["sanctioned"] is False
        assert defect_id not in backlog_state.scheduled_defect_ids()
        assert _open_ids() == [defect_id]
        assert _completed_ids() == before_completed
