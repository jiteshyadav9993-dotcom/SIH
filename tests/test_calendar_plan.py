"""Horizon rollover checks for calendar_plan.build_horizon_plan.

Hand-built bundles only. calendar_plan.py and solver.py are not modified.
build_horizon_plan loads the working timetable itself; these cases stay
small enough that each day's solve finishes inside the time limit.
"""

from __future__ import annotations

from datetime import date

from defects import (
    DIVISION_MACHINERY_LIMITS,
    DefectType,
    MachineType,
    MaintenanceBundle,
    TrackDefect,
    _make_defect,
    cluster_defects_into_bundles,
    compute_priority_urgency_score,
)
from solver import calculate_transit_envelope
from topology import LINE_DN

import calendar_plan
from calendar_plan import (
    EXTENDED_END_MIN,
    EXTENDED_START_MIN,
    LABEL_EXTENDED,
    LABEL_MEGA_BLOCK,
    LABEL_NIGHTLY,
    MEGA_BLOCK_END_MIN,
    MEGA_BLOCK_START_MIN,
    NIGHTLY_END_MIN,
    NIGHTLY_START_MIN,
    build_horizon_plan,
    decayed_urgency,
)

_START = date(2026, 9, 21)  # Monday. The following Sunday is inside a 7-day horizon.
_TIME_LIMIT_SEC: float = 5.0
_NIGHTLY_CAPACITY: int = NIGHTLY_END_MIN - NIGHTLY_START_MIN
_MEGA_CAPACITY: int = MEGA_BLOCK_END_MIN - MEGA_BLOCK_START_MIN
_EXTENDED_CAPACITY: int = EXTENDED_END_MIN - EXTENDED_START_MIN


def _bcm_bundle(bundle_id: str, km: float, days_overdue: float) -> MaintenanceBundle:
    defect = _make_defect(
        f"{bundle_id}_D",
        km,
        LINE_DN,
        DefectType.DEEP_SCREENING,
        50,
        days_overdue=days_overdue,
    )
    return MaintenanceBundle(
        bundle_id=bundle_id,
        track_line=LINE_DN,
        section_id=defect.section_id,
        start_km=round(km - 0.5, 3),
        end_km=round(km + 0.5, 3),
        defects=(defect,),
        required_duration_min=40.0,
        required_machine=MachineType.BCM_MACHINE,
        block_type=defect.block_type,
        power_sector_id=defect.power_sector_id,
        cumulative_priority=50.0,
        urgency_score=40.0,
        is_mandatory=False,
    )


def _confirmed_ids(plan: calendar_plan.HorizonPlan) -> set[str]:
    confirmed: set[str] = set()
    for day in plan.days:
        if day.result is None or day.result.status not in ("OPTIMAL", "FEASIBLE"):
            continue
        confirmed.update(block.bundle_id for block in day.result.blocks)
    return confirmed


def test_weekly_rollover_keeps_machine_contended_bundle() -> None:
    assert _START.weekday() == 0
    assert DIVISION_MACHINERY_LIMITS[MachineType.BCM_MACHINE] == 1
    urgent = _bcm_bundle("BCM_URGENT", 60.0, days_overdue=12.0)
    deferred = _bcm_bundle("BCM_DEFERRED", 62.0, days_overdue=0.0)
    assert calculate_transit_envelope(urgent) + calculate_transit_envelope(deferred) <= _NIGHTLY_CAPACITY
    assert decayed_urgency(urgent, 0) > decayed_urgency(deferred, 0)

    plan = build_horizon_plan((urgent, deferred), _START, 2, time_limit_sec=_TIME_LIMIT_SEC)

    assert len(plan.days) == 2
    assert plan.days[0].assigned_bundle_ids == (urgent.bundle_id,)
    assert plan.days[1].assigned_bundle_ids == (deferred.bundle_id,)
    assert plan.days[0].result is not None and plan.days[0].result.status in ("OPTIMAL", "FEASIBLE")
    assert plan.days[1].result is not None and plan.days[1].result.status in ("OPTIMAL", "FEASIBLE")
    assert {block.bundle_id for block in plan.days[1].result.blocks} == {deferred.bundle_id}
    assert deferred.bundle_id not in plan.unscheduled_bundle_ids
    assert plan.unscheduled_bundle_ids == ()


def test_sunday_uses_mega_block_and_other_days_stay_nightly() -> None:
    assert _START.weekday() == 0
    plan = build_horizon_plan((), _START, 7, time_limit_sec=_TIME_LIMIT_SEC)

    sundays = [day for day in plan.days if day.calendar_date.weekday() == 6]
    assert len(sundays) == 1
    sunday = sundays[0]
    assert sunday.calendar_date == date(2026, 9, 27)
    assert sunday.window.label == LABEL_MEGA_BLOCK
    assert (sunday.window.start_min, sunday.window.end_min) == (MEGA_BLOCK_START_MIN, MEGA_BLOCK_END_MIN)

    weekdays = [day for day in plan.days if day.calendar_date.weekday() != 6]
    assert len(weekdays) == 6
    for day in weekdays:
        assert day.window.label == LABEL_NIGHTLY
        assert (day.window.start_min, day.window.end_min) == (NIGHTLY_START_MIN, NIGHTLY_END_MIN)


def test_oversized_mandatory_bundle_is_split_and_sunday_opens_extended() -> None:
    assert _START.weekday() == 0
    lead = _make_defect("OS_LEAD", 70.0, LINE_DN, DefectType.RAIL_FRACTURE, 90, days_overdue=5.0)
    tail = _make_defect("OS_TAIL", 73.0, LINE_DN, DefectType.WELD_FAILURE, 85, days_overdue=1.0)
    parent = MaintenanceBundle(
        bundle_id="OVERSIZED_PARENT",
        track_line=LINE_DN,
        section_id=lead.section_id,
        start_km=69.5,
        end_km=73.5,
        defects=(lead, tail),
        required_duration_min=500.0,
        required_machine=MachineType.NONE,
        block_type=lead.block_type,
        power_sector_id=lead.power_sector_id,
        cumulative_priority=175.0,
        urgency_score=80.0,
        is_mandatory=True,
    )
    parent_envelope = calculate_transit_envelope(parent)
    assert parent.is_mandatory and len(parent.defects) > 1
    assert _MEGA_CAPACITY < parent_envelope <= _EXTENDED_CAPACITY
    assert parent_envelope > _NIGHTLY_CAPACITY

    pieces = cluster_defects_into_bundles(parent.defects, max_spatial_gap_km=0)
    assert len(pieces) == 2
    assert all(len(piece.defects) == 1 for piece in pieces)
    assert {piece.bundle_id for piece in pieces}.isdisjoint({parent.bundle_id})
    assert sum(calculate_transit_envelope(piece) for piece in pieces) <= _NIGHTLY_CAPACITY

    plan = build_horizon_plan((parent,), _START, 7, time_limit_sec=_TIME_LIMIT_SEC)

    sunday = next(day for day in plan.days if day.calendar_date.weekday() == 6)
    assert sunday.window.label == LABEL_EXTENDED
    assert (sunday.window.start_min, sunday.window.end_min) == (EXTENDED_START_MIN, EXTENDED_END_MIN)
    for day in plan.days:
        if day.calendar_date.weekday() != 6:
            assert day.window.label == LABEL_NIGHTLY

    scheduled = _confirmed_ids(plan)
    assert scheduled == {piece.bundle_id for piece in pieces}
    assert parent.bundle_id not in scheduled
    assert parent.bundle_id not in plan.unscheduled_bundle_ids
    for day in plan.days:
        assert parent.bundle_id not in day.assigned_bundle_ids


def test_short_horizon_reports_exactly_the_unscheduled_bundles() -> None:
    urgent = _bcm_bundle("BCM_FIT", 60.0, days_overdue=12.0)
    second = _bcm_bundle("BCM_WAIT", 62.0, days_overdue=6.0)
    third = _bcm_bundle("BCM_LEFT", 64.0, days_overdue=0.0)
    bundles = (urgent, second, third)
    assert DIVISION_MACHINERY_LIMITS[MachineType.BCM_MACHINE] == 1
    # All three envelopes fit inside one nightly window together. Day 1 still
    # takes only the most urgent bundle, because the BCM pool has a single
    # machine. A one-day horizon has no later night to give the other two.
    assert sum(calculate_transit_envelope(bundle) for bundle in bundles) <= _NIGHTLY_CAPACITY

    plan = build_horizon_plan(bundles, _START, 1, time_limit_sec=_TIME_LIMIT_SEC)

    assert len(plan.days) == 1
    scheduled = _confirmed_ids(plan)
    assert scheduled == {urgent.bundle_id}
    assert set(plan.unscheduled_bundle_ids) == {second.bundle_id, third.bundle_id}
    assert set(plan.unscheduled_bundle_ids) == {bundle.bundle_id for bundle in bundles} - scheduled


def test_decayed_urgency_grows_with_overdue_until_formula1_cap() -> None:
    fresh_defect: TrackDefect = _make_defect(
        "DECAY_FRESH",
        60.0,
        LINE_DN,
        DefectType.WELD_FAILURE,
        40,
        days_overdue=0.0,
        has_tsr=False,
    )
    aging_defect: TrackDefect = _make_defect(
        "DECAY_AGING",
        62.0,
        LINE_DN,
        DefectType.WELD_FAILURE,
        40,
        days_overdue=25.0,
        has_tsr=False,
    )

    def _as_bundle(defect: TrackDefect) -> MaintenanceBundle:
        return MaintenanceBundle(
            bundle_id=f"BUNDLE_{defect.defect_id}",
            track_line=defect.track_line,
            section_id=defect.section_id,
            start_km=round(defect.km_location - 0.5, 3),
            end_km=round(defect.km_location + 0.5, 3),
            defects=(defect,),
            required_duration_min=defect.required_block_min,
            required_machine=defect.required_machine,
            block_type=defect.block_type,
            power_sector_id=defect.power_sector_id,
            cumulative_priority=float(defect.severity),
            urgency_score=0.0,
            is_mandatory=False,
        )

    fresh = _as_bundle(fresh_defect)
    aging = _as_bundle(aging_defect)

    assert decayed_urgency(fresh, 0) < decayed_urgency(fresh, 10)
    assert decayed_urgency(fresh, 10) < decayed_urgency(fresh, 30)
    assert decayed_urgency(fresh, 30) == decayed_urgency(fresh, 60)

    assert decayed_urgency(aging, 0) < decayed_urgency(aging, 5)
    assert decayed_urgency(aging, 5) == decayed_urgency(aging, 20)

    for bundle, extra_days in (
        (fresh, 0),
        (fresh, 10),
        (fresh, 30),
        (fresh, 60),
        (aging, 0),
        (aging, 5),
        (aging, 20),
    ):
        defect = bundle.defects[0]
        expected = compute_priority_urgency_score(
            defect,
            days_overdue=defect.days_overdue + extra_days,
            has_tsr=defect.has_tsr,
            clip=False,
        )
        assert decayed_urgency(bundle, extra_days) == expected
