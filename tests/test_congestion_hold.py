"""A congestion hold keeps the demand open and does not close a live possession."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import HTTPException

import app as app_module
import sim_clock


def _possession(**overrides: object) -> dict[str, object]:
    plan = sim_clock.date_for_absolute_day(1).isoformat()
    record: dict[str, object] = {
        "bundle_id": "BUNDLE-TAMP-1",
        "track_line": "UP",
        "section_id": "SECTION_4",
        "start_min": 180,
        "end_min": 230,
        "start_hhmm": "03:00",
        "end_hhmm": "03:50",
        "power_sector_id": "PSS-TEST",
        "solver_status": "FEASIBLE",
        "block_type": "TRAFFIC_ONLY",
        "earth_required": False,
        "defect_ids": [],
        "worksite_start_km": 160.0,
        "worksite_end_km": 161.0,
        "closed_start_km": 159.5,
        "closed_end_km": 161.5,
        "protected_lines": ["UP"],
        "approach_km": 1.6,
        "earth_up_km": 159.5,
        "earth_down_km": 161.5,
        "possession_ref": "COA-CR-BLK-HOLD",
        "window_label": "NIGHTLY",
        "window_index": 2,
        "plan_date": plan,
        "plan_day": 1,
        "start_abs": 1440 + 180,
        "end_abs": 1440 + 230,
        "timeline": {
            "start_min": 180.0,
            "end_min": 230.0,
            "arrive_min": 190.0,
            "protection_ready_min": 200.0,
            "work_end_min": 220.0,
            "return_start_min": 220.0,
            "transit_min": 10.0,
        },
        "machines": [
            {
                "machine_type": "TIE_TAMPER",
                "unit_index": 1,
                "depot_km": 150.0,
                "site_km": 160.0,
                "speed_kmh": 20.0,
            }
        ],
        "immediate": False,
        "staff_count": 4,
        "machines_label": "TIE_TAMPER",
    }
    record.update(overrides)
    return record


@pytest.fixture
def isolated_hold(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, list[dict[str, object]]]]:
    saved = {key: app_module.SYSTEM_STATE.get(key) for key in (
        "active_plan",
        "sanctioned",
        "sanctioned_by",
        "emergency_active",
        "dossier_submitted",
        "coa_dossier_id",
        "reject_reason",
        "congestion_hold",
        "possession",
        "tsr",
        "hold_count",
        "paused_work",
    )}
    posted: list[dict[str, object]] = []
    recorded: list[dict[str, object]] = []
    dossiers: list[dict[str, object]] = []

    def _post(
        ref: str,
        kind: str,
        from_role: str,
        to_role: str,
        text: str,
        sequence: int = 0,
    ) -> int:
        posted.append(
            {"ref": ref, "kind": kind, "from": from_role, "to": to_role, "text": text, "sequence": sequence}
        )
        return len(posted)

    def _record(**kwargs: object) -> int:
        recorded.append(kwargs)
        return len(recorded)

    def _dossier(**kwargs: object) -> int:
        dossiers.append(kwargs)
        return len(dossiers)

    monkeypatch.setattr(app_module.sim_clock, "advance", lambda: None)
    monkeypatch.setattr(app_module.sim_clock, "save", lambda: None)
    monkeypatch.setattr(app_module.sim_clock, "save_if_due", lambda: None)
    monkeypatch.setattr(app_module.sim_clock, "minute_of_day", lambda: 1400.0)
    monkeypatch.setattr(app_module.sim_clock, "absolute_minute", lambda: 1400.0)
    monkeypatch.setattr(app_module.sim_clock, "absolute_day", lambda: 0)
    monkeypatch.setattr(app_module.sim_clock, "sim_date", lambda: sim_clock.date_for_absolute_day(0))
    monkeypatch.setattr(app_module.persistence, "post_block_message", _post)
    monkeypatch.setattr(app_module.persistence, "record_possession_event", _record)
    monkeypatch.setattr(app_module.persistence, "record_dossier_submission", _dossier)
    monkeypatch.setattr(app_module.persistence, "record_tsr_order", lambda **_kwargs: 1)
    monkeypatch.setattr(app_module.persistence, "next_order_serial", lambda: 1)
    monkeypatch.setattr(app_module.persistence, "has_block_message", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(app_module.backlog_state, "release_defects", lambda _ids: None)
    monkeypatch.setattr(app_module.backlog_state, "reserve_defects", lambda _ids: None)
    monkeypatch.setattr(app_module.backlog_state, "scheduled_defect_ids", lambda: frozenset())
    monkeypatch.setattr(app_module.data_ingestion, "get_effective_defects", lambda: ())
    app_module.SYSTEM_STATE["sanctioned"] = False
    app_module.SYSTEM_STATE["possession"] = None
    app_module.SYSTEM_STATE["dossier_submitted"] = False
    app_module.SYSTEM_STATE["reject_reason"] = None
    app_module.SYSTEM_STATE["congestion_hold"] = None
    app_module.SYSTEM_STATE["emergency_active"] = False
    app_module.SYSTEM_STATE["tsr"] = None
    app_module.SYSTEM_STATE["hold_count"] = 0
    app_module.SYSTEM_STATE["active_plan"] = "PUNCTUALITY_FIRST"
    try:
        yield {"posted": posted, "recorded": recorded, "dossiers": dossiers}
    finally:
        for key, value in saved.items():
            app_module.SYSTEM_STATE[key] = value


def _hold_messages(bucket: dict[str, list[dict[str, object]]]) -> list[dict[str, object]]:
    return [row for row in bucket["posted"] if row["kind"] == "CONGESTION_HOLD"]


def test_hold_refuses_when_no_dossier_was_sent(
    isolated_hold: dict[str, list[dict[str, object]]],
) -> None:
    with pytest.raises(HTTPException) as refused:
        app_module.chief_reject({"reason": "banana"})
    assert refused.value.status_code == 409
    assert "No dossier has been sent" in str(refused.value.detail)
    assert isolated_hold["posted"] == []
    assert isolated_hold["dossiers"] == []
    assert app_module.SYSTEM_STATE["reject_reason"] is None
    assert app_module.SYSTEM_STATE["congestion_hold"] is None


def test_hold_keeps_a_transmitted_demand_and_offers_the_next_window(
    isolated_hold: dict[str, list[dict[str, object]]],
) -> None:
    app_module.SYSTEM_STATE["dossier_submitted"] = True
    body = app_module.chief_reject({"reason": "banana"})
    assert body["status"] == "HELD"
    assert body["reason"] == app_module._CONGESTION_HOLD_REASON
    assert "banana" not in str(body["message"])
    assert "demand stays open" in str(body["message"])
    alternate = body["alternate_window"]
    assert isinstance(alternate, dict)
    assert alternate["available"] is True
    assert alternate["window_label"]
    assert "–" in f"{alternate['window_start_hhmm']}–{alternate['window_end_hhmm']}" or "-" in str(alternate["window_start_hhmm"])
    assert app_module.SYSTEM_STATE["dossier_submitted"] is True
    assert app_module.SYSTEM_STATE["reject_reason"] is None
    assert app_module.SYSTEM_STATE["sanctioned"] is False
    posted = _hold_messages(isolated_hold)
    assert any(row["to"] == "MAINTENANCE_OFFICER" for row in posted)
    assert any(row["to"] == "STATION_MASTER" for row in posted)
    assert any(row["to"] == "TRACTION_POWER_CONTROLLER" for row in posted)
    assert isolated_hold["dossiers"][0]["status"] == "HELD"
    assert isolated_hold["dossiers"][0]["status"] != "REJECTED"


def test_unstarted_sanction_is_released_and_the_demand_stays_open(
    isolated_hold: dict[str, list[dict[str, object]]],
) -> None:
    record = _possession(block_type="POWER_AND_TRAFFIC", earth_required=True)
    app_module.SYSTEM_STATE["sanctioned"] = True
    app_module.SYSTEM_STATE["dossier_submitted"] = True
    app_module.SYSTEM_STATE["possession"] = record
    body = app_module.chief_reject({})
    assert body["status"] == "HELD"
    assert body["possession"] is None
    assert app_module.SYSTEM_STATE["sanctioned"] is False
    assert app_module.SYSTEM_STATE["dossier_submitted"] is True
    assert app_module.SYSTEM_STATE["tsr"] is None
    assert app_module.SYSTEM_STATE["reject_reason"] is None
    outcomes = [str(row["outcome"]) for row in isolated_hold["recorded"]]
    assert "WITHDRAWN" in outcomes
    assert "REJECTED" not in outcomes
    posted = _hold_messages(isolated_hold)
    assert any("Do not isolate" in str(row["text"]) and row["to"] == "TRACTION_POWER_CONTROLLER" for row in posted)
    assert any("STATION_MASTER" in str(row["to"]) for row in posted)
    assert any(row["to"] == "MAINTENANCE_OFFICER" for row in posted)
    assert any(row["to"] == "SITE_GANG" and "Stand down" in str(row["text"]) for row in posted)


def test_live_possession_uses_withdraw_and_imposes_caution(
    isolated_hold: dict[str, list[dict[str, object]]],
) -> None:
    rolling = _possession(
        plan_date=sim_clock.date_for_absolute_day(0).isoformat(),
        plan_day=0,
        start_min=1350,
        end_min=1550,
        start_abs=1350.0,
        end_abs=1550.0,
        timeline={
            "start_min": 1350.0,
            "end_min": 1550.0,
            "arrive_min": 1450.0,
            "protection_ready_min": 1460.0,
            "work_end_min": 1540.0,
            "return_start_min": 1540.0,
            "transit_min": 100.0,
        },
    )
    app_module.SYSTEM_STATE["sanctioned"] = True
    app_module.SYSTEM_STATE["dossier_submitted"] = True
    app_module.SYSTEM_STATE["possession"] = rolling
    moving = app_module.chief_reject({})
    assert moving["status"] == "HELD"
    assert moving["phase"] == "DEMOBILISING"
    assert rolling["withdraw_pending"] is True
    assert rolling["start_min"] == 1350
    assert app_module.SYSTEM_STATE["sanctioned"] is True
    assert app_module.SYSTEM_STATE["tsr"] is None
    assert any(row["kind"] == "CLEAR_LINE" and row["to"] == "SITE_GANG" for row in isolated_hold["posted"])

    working = _possession(
        plan_date=sim_clock.date_for_absolute_day(0).isoformat(),
        plan_day=0,
        block_type="POWER_AND_TRAFFIC",
        earth_required=True,
        possession_ref="COA-CR-BLK-LIVE",
        start_min=1300,
        end_min=1500,
        start_abs=1300.0,
        end_abs=1500.0,
        timeline={
            "start_min": 1300.0,
            "end_min": 1500.0,
            "arrive_min": 1350.0,
            "protection_ready_min": 1360.0,
            "work_end_min": 1480.0,
            "return_start_min": 1480.0,
            "transit_min": 50.0,
        },
    )
    app_module.SYSTEM_STATE["possession"] = working
    app_module.SYSTEM_STATE["congestion_hold"] = None
    held = app_module.chief_reject({"reason": "free text"})
    assert held["status"] == "HELD"
    assert "Caution 30 km/h" in str(held["message"])
    assert "free text" not in str(held["message"])
    assert working["start_min"] == 1300
    assert app_module.SYSTEM_STATE["sanctioned"] is True
    tsr = app_module.SYSTEM_STATE["tsr"]
    assert isinstance(tsr, dict)
    assert tsr["max_kmh"] == 30.0
    assert any(
        row["to"] == "TRACTION_POWER_CONTROLLER" and "feeder" in str(row["text"])
        for row in _hold_messages(isolated_hold)
    )
    assert any(row["kind"] == "CLEAR_LINE" and "30 km/h" in str(row["text"]) for row in isolated_hold["posted"])


def test_caution_saves_when_no_block_and_the_backlog_is_empty(
    isolated_hold: dict[str, list[dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: list[dict[str, object]] = []

    def _record(**kwargs: object) -> int:
        saved.append(kwargs)
        return 1

    monkeypatch.setattr(app_module.persistence, "record_tsr_order", _record)
    body = app_module.chief_caution({"max_kmh": 30, "center_km": 114})
    assert body["status"] == "CAUTION"
    tsr = body["tsr"]
    assert isinstance(tsr, dict)
    assert tsr["line"] == "CORRIDOR"
    assert tsr["start_km"] == 111.0
    assert tsr["end_km"] == 117.0
    assert tsr["max_kmh"] == 30.0
    assert saved[0]["start_km"] == 111.0
    assert saved[0]["end_km"] == 117.0
    assert isolated_hold["recorded"] == []


def test_caution_uses_the_ghat_centre_when_no_chainage_is_sent(
    isolated_hold: dict[str, list[dict[str, object]]],
) -> None:
    _ = isolated_hold
    body = app_module.chief_caution({"max_kmh": 30})
    tsr = body["tsr"]
    assert isinstance(tsr, dict)
    assert tsr["start_km"] == 111.0
    assert tsr["end_km"] == 117.0


def test_caution_keeps_the_possession_limits_when_a_block_is_on(
    isolated_hold: dict[str, list[dict[str, object]]],
) -> None:
    _ = isolated_hold
    app_module.SYSTEM_STATE["possession"] = {
        "worksite_start_km": 80.0,
        "worksite_end_km": 82.0,
        "track_line": "UP",
    }
    body = app_module.chief_caution({"max_kmh": 30, "center_km": 114})
    tsr = body["tsr"]
    assert isinstance(tsr, dict)
    assert tsr["line"] == "UP"
    assert tsr["start_km"] == 80.0
    assert tsr["end_km"] == 82.0
