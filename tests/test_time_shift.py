"""A time shift asks engineering and leaves a live possession where it is."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import HTTPException

import app as app_module
import sim_clock
import solver


class _PathTrain:
    def __init__(self, train_id: str, train_type: str) -> None:
        self.train_id = train_id
        self.train_type = train_type


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
        "possession_ref": "COA-CR-BLK-SHIFT",
        "window_label": "NIGHTLY",
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
        "shift_count": 0,
        "staff_count": 4,
        "machines_label": "TIE_TAMPER",
    }
    record.update(overrides)
    return record


def _install(record: dict[str, object] | None) -> None:
    app_module.SYSTEM_STATE["sanctioned"] = record is not None
    app_module.SYSTEM_STATE["possession"] = record
    app_module.SYSTEM_STATE["active_plan"] = "PUNCTUALITY_FIRST"
    app_module.SYSTEM_STATE["coa_dossier_id"] = None if record is None else record["possession_ref"]
    app_module.SYSTEM_STATE["earliest_start_min"] = 44
    app_module.SYSTEM_STATE["emergency_active"] = False
    app_module.SYSTEM_STATE["sanctioned_by"] = "SECTION_CONTROLLER"


@pytest.fixture
def isolated_shift(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, list[dict[str, object]]]]:
    saved = {key: app_module.SYSTEM_STATE.get(key) for key in (
        "active_plan",
        "sanctioned",
        "sanctioned_by",
        "emergency_active",
        "coa_dossier_id",
        "possession",
        "earliest_start_min",
        "paused_work",
    )}
    posted: list[dict[str, object]] = []
    recorded: list[dict[str, object]] = []

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

    def _no_trains(*_args: object, **_kwargs: object) -> list[object]:
        return []

    def _solver_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a time shift must not ask the solver for a blind later start")

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
    monkeypatch.setattr(app_module.backlog_state, "release_defects", lambda _ids: None)
    monkeypatch.setattr(app_module.backlog_state, "reserve_defects", lambda _ids: None)
    monkeypatch.setattr(app_module.timetable, "get_trains_in_block", _no_trains)
    monkeypatch.setattr(solver, "solve_corridor_schedule", _solver_must_not_run)
    _install(None)
    try:
        yield {"posted": posted, "recorded": recorded}
    finally:
        for key, value in saved.items():
            app_module.SYSTEM_STATE[key] = value


def test_unsanctioned_shift_does_not_pretend_the_window_moved(
    isolated_shift: dict[str, list[dict[str, object]]],
) -> None:
    with pytest.raises(HTTPException) as refused:
        app_module.chief_shift({"delta_min": 30})
    assert refused.value.status_code == 409
    assert "Nothing is sanctioned" in str(refused.value.detail)
    assert app_module.SYSTEM_STATE["earliest_start_min"] == 44
    assert isolated_shift["posted"] == []


def test_live_possession_with_machines_or_an_isolated_feeder_stays_put(
    isolated_shift: dict[str, list[dict[str, object]]],
) -> None:
    machines = _possession(
        plan_date=sim_clock.date_for_absolute_day(0).isoformat(),
        plan_day=0,
        start_min=1300,
        end_min=1500,
        start_abs=1300.0,
        end_abs=1500.0,
        start_hhmm="21:40",
        end_hhmm="01:00",
        timeline={
            "arrive_min": 1350.0,
            "protection_ready_min": 1360.0,
            "work_end_min": 1480.0,
            "return_start_min": 1480.0,
        },
    )
    _install(machines)
    with pytest.raises(HTTPException) as on_line:
        app_module.chief_shift({"delta_min": 30})
    assert on_line.value.status_code == 409
    assert "Machines are already on the line" in str(on_line.value.detail)
    assert machines["start_min"] == 1300

    feeder = _possession(
        plan_date=sim_clock.date_for_absolute_day(0).isoformat(),
        plan_day=0,
        block_type="POWER_AND_TRAFFIC",
        earth_required=True,
        start_min=1300,
        end_min=1500,
        start_abs=1300.0,
        end_abs=1500.0,
        timeline={
            "arrive_min": 1350.0,
            "protection_ready_min": 1360.0,
            "work_end_min": 1480.0,
            "return_start_min": 1480.0,
        },
    )
    _install(feeder)
    with pytest.raises(HTTPException) as isolated:
        app_module.chief_shift({})
    assert "feeder is already isolated" in str(isolated.value.detail)
    assert feeder["start_min"] == 1300

    rolling = _possession(
        plan_date=sim_clock.date_for_absolute_day(0).isoformat(),
        plan_day=0,
        start_min=1350,
        end_min=20,
        start_abs=1350.0,
        end_abs=1440.0 + 20,
        timeline={
            "arrive_min": 1410.0,
            "protection_ready_min": 1420.0,
            "work_end_min": 10.0,
            "return_start_min": 10.0,
        },
    )
    _install(rolling)
    with pytest.raises(HTTPException) as taken:
        app_module.chief_shift({})
    assert "line is already taken" in str(taken.value.detail)
    assert app_module.SYSTEM_STATE["earliest_start_min"] == 44
    assert isolated_shift["posted"] == []


def test_shift_asks_engineering_and_does_not_add_thirty_minutes(
    isolated_shift: dict[str, list[dict[str, object]]],
) -> None:
    record = _possession()
    _install(record)
    body = app_module.chief_shift({"delta_min": 30})
    assert body["status"] == "ASKED"
    proposal = body["proposal"]
    assert isinstance(proposal, dict)
    assert proposal["start_min"] == 185
    assert proposal["end_min"] == 235
    assert record["start_min"] == 180
    assert record["shift_request"] == proposal
    message = str(body["message"])
    assert "Asked engineering" in message
    assert "still stands at 03:00" in message
    assert "Mail and express" in message
    assert "suburban peak" in message
    assert "agreed power block" not in message
    assert "gang duty" in message
    posted = isolated_shift["posted"]
    assert any(row["kind"] == "SHIFT_REQUEST" and row["to"] == "MAINTENANCE_OFFICER" for row in posted)
    assert any(row["kind"] == "SHIFT_REQUEST" and row["to"] == "SITE_GANG" for row in posted)
    assert app_module.SYSTEM_STATE["earliest_start_min"] == 44


def test_mail_express_and_suburban_peak_and_power_block_and_duty_are_checked(
    isolated_shift: dict[str, list[dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _express_until_100(
        _lo: float,
        _hi: float,
        t_start: float,
        _t_end: float,
        _line: str,
    ) -> list[_PathTrain]:
        if t_start < 100:
            return [_PathTrain("12127", "EXPRESS")]
        return []

    monkeypatch.setattr(app_module.timetable, "get_trains_in_block", _express_until_100)
    mail = _possession(start_min=60, end_min=90, start_hhmm="01:00", end_hhmm="01:30", start_abs=1440 + 60, end_abs=1440 + 90)
    _install(mail)
    asked = app_module.chief_shift({})
    proposal = asked["proposal"]
    assert isinstance(proposal, dict)
    assert proposal["start_min"] == 100
    assert mail["start_min"] == 60

    peak = _possession(
        section_id="SECTION_1",
        track_line="UP_FAST",
        protected_lines=["UP_FAST"],
        window_label="BLOCK",
        worksite_start_km=20.0,
        worksite_end_km=21.0,
        closed_start_km=19.5,
        closed_end_km=21.5,
        start_min=500,
        end_min=540,
        start_hhmm="08:20",
        end_hhmm="09:00",
        start_abs=1440 + 500,
        end_abs=1440 + 540,
    )
    monkeypatch.setattr(app_module.timetable, "get_trains_in_block", lambda *_args, **_kwargs: [])
    _install(peak)
    moved = app_module.chief_shift({})
    peak_proposal = moved["proposal"]
    assert isinstance(peak_proposal, dict)
    assert peak_proposal["start_min"] == 690
    assert peak_proposal["window_label"] == "DAY_OFFPEAK"
    assert "suburban peak" in str(moved["message"])
    assert "Engineering has to accept that roster" in str(moved["message"])
    assert peak["start_min"] == 500

    power = _possession(
        block_type="POWER_AND_TRAFFIC",
        earth_required=True,
        start_min=200,
        end_min=250,
        start_hhmm="03:20",
        end_hhmm="04:10",
        start_abs=1440 + 200,
        end_abs=1440 + 250,
    )
    _install(power)
    with pytest.raises(HTTPException) as refused:
        app_module.chief_shift({})
    assert refused.value.status_code == 409
    assert "traction power controller" in str(refused.value.detail)
    assert power["start_min"] == 200
    assert "shift_request" not in power


def test_engineering_agreement_moves_the_block_and_a_decline_leaves_it(
    isolated_shift: dict[str, list[dict[str, object]]],
) -> None:
    record = _possession(block_type="POWER_AND_TRAFFIC", earth_required=True)
    _install(record)
    with pytest.raises(HTTPException) as early:
        app_module.chief_shift({"confirm": True})
    assert "Ask engineering" in str(early.value.detail)

    asked = app_module.chief_shift({})
    proposal = asked["proposal"]
    assert isinstance(proposal, dict)
    assert proposal["start_min"] == 185
    assert any(row["to"] == "TRACTION_POWER_CONTROLLER" for row in isolated_shift["posted"])
    agreed = app_module.chief_shift({"confirm": True})
    assert agreed["status"] == "SHIFTED"
    moved = agreed["possession"]
    assert isinstance(moved, dict)
    assert moved["start_min"] == 185
    assert moved["end_min"] == 235
    assert moved["start_hhmm"] == "03:05"
    timeline = moved["timeline"]
    assert isinstance(timeline, dict)
    assert timeline["arrive_min"] == 195.0
    assert "shift_request" not in moved
    assert "Engineering agreed" in str(agreed["message"])
    assert any(row["kind"] == "ENGINEERING_AGREED" for row in isolated_shift["posted"])
    assert app_module.SYSTEM_STATE["earliest_start_min"] == 44

    again = _possession()
    _install(again)
    app_module.chief_shift({})
    declined = app_module.chief_shift({"decline": True})
    assert declined["status"] == "DECLINED"
    assert again["start_min"] == 180
    assert "shift_request" not in again
    assert any(row["kind"] == "ENGINEERING_DECLINED" for row in isolated_shift["posted"])
