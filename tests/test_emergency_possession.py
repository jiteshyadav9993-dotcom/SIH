"""Emergency possessions protect a chosen site and hold approaching trains."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import HTTPException

import app as app_module
import timetable


def test_stop_approaching_holds_short_of_a_ghat_line() -> None:
    hit: tuple[timetable.TrainTrajectory, float] | None = None
    for trajectory in timetable.ALL_TRAJECTORIES:
        if not trajectory.waypoints:
            continue
        intervals = timetable._occupancy_intervals(trajectory, 110.0, 112.0, frozenset({"MID"}))
        if intervals:
            hit = (trajectory, intervals[0][0])
            break
    assert hit is not None
    trajectory, enters = hit
    common: dict[str, object] = {
        "possession_id": "EMG",
        "protected_lines": ("MID",),
        "start_km": 110.0,
        "end_km": 112.0,
        "start_min": 0.0,
        "end_min": 1440.0,
        "approach_km": 1.2,
    }
    open_block = timetable.LinePossession(**common, stop_approaching=False)  # type: ignore[arg-type]
    stop_block = timetable.LinePossession(**common, stop_approaching=True)  # type: ignore[arg-type]
    clock = timetable.wrap_minute(enters + 1.0)
    moving = timetable.get_regulated_positions(clock, [open_block])
    stopped = timetable.get_regulated_positions(clock, [stop_block])
    moving_row = next(row for row in moving if row["train_id"] == trajectory.train_id)
    stopped_row = next(row for row in stopped if row["train_id"] == trajectory.train_id)
    assert moving_row["status"] == timetable.STATUS_DIVERTED
    assert stopped_row["status"] == timetable.STATUS_HELD
    assert stopped_row["speed_kmh"] == 0.0
    assert stopped_row["current_line"] == "MID"
    km = float(stopped_row["current_km"])
    assert km <= 110.0 - 1.2 + 0.05 or km >= 112.0 + 1.2 - 0.05
    assert not any(row.get("diverted") for row in stopped)


@pytest.fixture
def isolated_emergency(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, list[dict[str, object]]]]:
    saved = {
        key: app_module.SYSTEM_STATE.get(key)
        for key in (
            "active_plan",
            "sanctioned",
            "sanctioned_by",
            "freight_held",
            "emergency_active",
            "dossier_submitted",
            "coa_dossier_id",
            "reject_reason",
            "possession",
            "paused_work",
            "tsr",
        )
    }
    posted: list[dict[str, object]] = []
    recorded: list[dict[str, object]] = []
    released: list[str] = []

    def _post(
        ref: str,
        kind: str,
        from_role: str,
        to_role: str,
        text: str,
        sequence: int = 0,
    ) -> int:
        posted.append(
            {
                "ref": ref,
                "kind": kind,
                "from": from_role,
                "to": to_role,
                "text": text,
                "sequence": sequence,
            }
        )
        return len(posted)

    def _record(**kwargs: object) -> int:
        recorded.append(kwargs)
        return len(recorded)

    def _release(ids: object) -> None:
        released.extend(str(item) for item in ids)  # type: ignore[arg-type]

    monkeypatch.setattr(app_module.sim_clock, "advance", lambda: None)
    monkeypatch.setattr(app_module.sim_clock, "save", lambda: None)
    monkeypatch.setattr(app_module.sim_clock, "save_if_due", lambda: None)
    monkeypatch.setattr(app_module.sim_clock, "minute_of_day", lambda: 1400.0)
    monkeypatch.setattr(app_module.sim_clock, "absolute_minute", lambda: 1400.0)
    monkeypatch.setattr(app_module.sim_clock, "absolute_day", lambda: 0)
    monkeypatch.setattr(app_module.persistence, "post_block_message", _post)
    monkeypatch.setattr(app_module.persistence, "record_possession_event", _record)
    monkeypatch.setattr(app_module.persistence, "next_order_serial", lambda: 1)
    monkeypatch.setattr(app_module.persistence, "has_block_message", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(app_module.backlog_state, "release_defects", _release)
    monkeypatch.setattr(app_module.backlog_state, "reserve_defects", lambda _ids: None)
    app_module.SYSTEM_STATE["sanctioned"] = False
    app_module.SYSTEM_STATE["sanctioned_by"] = None
    app_module.SYSTEM_STATE["possession"] = None
    app_module.SYSTEM_STATE["emergency_active"] = False
    app_module.SYSTEM_STATE["paused_work"] = None
    app_module.SYSTEM_STATE["coa_dossier_id"] = None
    app_module.SYSTEM_STATE["dossier_submitted"] = False
    try:
        yield {"posted": posted, "recorded": recorded, "released": released}  # type: ignore[dict-item]
    finally:
        for key, value in saved.items():
            app_module.SYSTEM_STATE[key] = value


def _messages(bucket: dict[str, list[dict[str, object]]]) -> list[dict[str, object]]:
    return bucket["posted"]


def test_late_fracture_uses_the_chosen_km_and_tells_the_station_master(
    isolated_emergency: dict[str, list[dict[str, object]]],
) -> None:
    body = app_module.simulate_emergency(
        {"emergency_type": "RAIL_FRACTURE", "chainage_km": 87.2, "target_line": "UP"}
    )
    assert body["status"] == "SANCTIONED"
    possession = body["possession"]
    assert isinstance(possession, dict)
    assert possession["emergency_km"] == 87.2
    assert possession["track_line"] == "UP"
    assert possession["protected_lines"] == ["UP"]
    assert possession["stop_approaching"] is True
    assert possession["approach_km"] == 1.2
    assert possession["defect_ids"] == []
    assert str(possession["bundle_id"]).startswith("EMG-RAIL_FRACTURE-87.2")
    assert possession["earth_required"] is False
    assert possession["start_min"] == 1400
    assert possession["end_min"] == 55
    assert app_module.SYSTEM_STATE["emergency_active"] is True
    execution = body["execution"]
    assert isinstance(execution, dict)
    assert execution["live"] is True
    assert execution["phase"] == "PROTECTION"
    labels = [str(step["label"]) for step in execution["steps"]]  # type: ignore[index]
    assert "Emergency repair in progress" in labels
    assert "Approaching trains stopped short of the site" in labels
    posted = _messages(isolated_emergency)
    station = [row for row in posted if "STATION_MASTER" in str(row["to"]) and row["kind"] == "EMERGENCY_ORDER"]
    assert station
    assert all("Stop approaching trains" in str(row["text"]) for row in station)
    assert any("87.2" in str(row["text"]) for row in station)
    assert any(row["to"] == "TRACTION_POWER_CONTROLLER" and "Stand by" in str(row["text"]) for row in posted)
    assert any(row["to"] == "SITE_GANG" and "immediately" in str(row["text"]) for row in posted)
    rows = timetable.get_regulated_positions(1400.0, app_module._active_possessions())
    for row in rows:
        if row["current_line"] == "UP" and 87.1 < float(row["current_km"]) < 87.3:
            raise AssertionError(row)
        if row["train_id"] in body["held_trains"]:
            assert row["status"] == "HELD"
            assert row["speed_kmh"] == 0.0


def test_landslide_outside_the_ghat_does_not_sanction(
    isolated_emergency: dict[str, list[dict[str, object]]],
) -> None:
    del isolated_emergency
    with pytest.raises(HTTPException) as refused:
        app_module.simulate_emergency(
            {"emergency_type": "LANDSLIDE", "chainage_km": 40, "target_line": "DN"}
        )
    assert refused.value.status_code == 400
    assert app_module.SYSTEM_STATE["sanctioned"] is False
    assert app_module.SYSTEM_STATE["emergency_active"] is False


def test_fracture_rejects_a_line_that_is_not_there(
    isolated_emergency: dict[str, list[dict[str, object]]],
) -> None:
    del isolated_emergency
    with pytest.raises(HTTPException) as refused:
        app_module.simulate_emergency(
            {"emergency_type": "RAIL_FRACTURE", "chainage_km": 20, "target_line": "MID"}
        )
    assert refused.value.status_code == 400
    assert "not at KM 20.0" in str(refused.value.detail)
    assert app_module.SYSTEM_STATE["sanctioned"] is False


def test_landslide_protects_every_ghat_line_and_ohe_asks_for_isolation(
    isolated_emergency: dict[str, list[dict[str, object]]],
) -> None:
    slip = app_module.simulate_emergency(
        {"emergency_type": "LANDSLIDE", "chainage_km": 114, "target_line": "MID"}
    )
    slip_possession = slip["possession"]
    assert isinstance(slip_possession, dict)
    assert set(slip_possession["protected_lines"]) == {"UP", "MID", "DN"}
    assert slip_possession["section_id"] == "SECTION_3"
    assert slip_possession["earth_required"] is False
    wire = app_module.simulate_emergency(
        {"emergency_type": "OHE_BREAKDOWN", "chainage_km": 30, "target_line": "DN_FAST"}
    )
    wire_possession = wire["possession"]
    assert isinstance(wire_possession, dict)
    assert wire_possession["earth_required"] is True
    assert "DN_FAST" in wire_possession["protected_lines"]
    assert wire_possession["supersedes"]
    posted = _messages(isolated_emergency)
    assert any(
        row["to"] == "TRACTION_POWER_CONTROLLER" and "Isolate and earth" in str(row["text"])
        for row in posted
    )
    assert any(row["kind"] == "SUPERSEDED" for row in posted)


def test_emergency_pauses_open_work_and_blocks_other_orders(
    isolated_emergency: dict[str, list[dict[str, object]]],
) -> None:
    app_module.SYSTEM_STATE["sanctioned"] = True
    app_module.SYSTEM_STATE["possession"] = {
        "bundle_id": "BUNDLE-TAMP-1",
        "track_line": "UP",
        "section_id": "SECTION_2",
        "start_min": 600,
        "end_min": 700,
        "power_sector_id": "PSS-TEST",
        "solver_status": "FEASIBLE",
        "defect_ids": ["D-1"],
        "worksite_start_km": 80.0,
        "worksite_end_km": 81.0,
        "closed_start_km": 79.5,
        "closed_end_km": 81.5,
        "protected_lines": ["UP"],
        "approach_km": 1.6,
        "start_hhmm": "10:00",
        "end_hhmm": "11:40",
        "possession_ref": "COA-CR-BLK-TEST",
        "timeline": {
            "arrive_min": 610.0,
            "protection_ready_min": 620.0,
            "work_end_min": 690.0,
            "return_start_min": 690.0,
        },
        "machines": [],
        "immediate": False,
        "plan_day": 0,
        "start_abs": 600.0,
        "end_abs": 700.0,
    }
    body = app_module.simulate_emergency(
        {"emergency_type": "RAIL_FRACTURE", "chainage_km": 64.5, "target_line": "DN"}
    )
    paused = body["paused_work"]
    assert isinstance(paused, dict)
    assert paused["bundle_id"] == "BUNDLE-TAMP-1"
    assert paused["possession_ref"] == "COA-CR-BLK-TEST"
    possession = body["possession"]
    assert isinstance(possession, dict)
    assert possession["defect_ids"] == []
    assert possession["paused_work"] == paused
    posted = _messages(isolated_emergency)
    assert any(row["kind"] == "WORK_PAUSED" and row["to"] == "SITE_GANG" for row in posted)
    assert any(row["kind"] == "WORK_PAUSED" and "STATION_MASTER" in str(row["to"]) for row in posted)
    outcomes = [str(row["outcome"]) for row in isolated_emergency["recorded"]]
    assert "PAUSED" in outcomes
    assert "SANCTIONED" in outcomes
    assert "D-1" in isolated_emergency["released"]
    with pytest.raises(HTTPException) as rejected:
        app_module.chief_reject({"reason": "congestion"})
    assert rejected.value.status_code == 409
    with pytest.raises(HTTPException) as shifted:
        app_module.chief_shift({"delta_min": 30})
    assert shifted.value.status_code == 409
    with pytest.raises(HTTPException) as withdrawn:
        app_module.chief_withdraw()
    assert withdrawn.value.status_code == 409
    with pytest.raises(HTTPException) as cleared:
        app_module.chief_clear()
    assert cleared.value.status_code == 409
    assert "emergency repair" in str(cleared.value.detail)
    assert app_module.SYSTEM_STATE["emergency_active"] is True
    assert app_module.SYSTEM_STATE["sanctioned"] is True
    live = app_module.SYSTEM_STATE["possession"]
    assert isinstance(live, dict)
    assert live["emergency_km"] == 64.5
