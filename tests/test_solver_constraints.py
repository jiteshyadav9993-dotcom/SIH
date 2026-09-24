"""Constraint checks for solver.solve_corridor_schedule.

Hand-built bundles and trains only. These tests do not call
defects.get_default_defects() and do not modify solver.py.
"""

from __future__ import annotations

import math

import pytest

from defects import (
    BlockType,
    DefectType,
    MachineType,
    MaintenanceBundle,
    TrackDefect,
    _make_defect,
    get_power_subsector,
)
from solver import (
    MAX_PATIENCE_MIN,
    SolverResult,
    _block_entry_exit_minutes,
    _trajectory_uses_line_in_range,
    calculate_transit_envelope,
    protection_zone,
    solve_corridor_schedule,
)
from timetable import (
    DIR_DN,
    DIR_UP,
    TYPE_EXPRESS,
    TYPE_LOCAL,
    TYPE_PREMIUM,
    TYPE_SUPERFAST,
    TimeWindow,
    TrainTrajectory,
    Waypoint,
)
from topology import LINE_DN, LINE_DN_FAST, LINE_UP_FAST

_TIME_LIMIT_SEC: float = 5.0


def _train(
    train_id: str,
    train_type: str,
    direction: str,
    line: str,
    km_start: float,
    km_end: float,
    minute_start: float,
    minute_end: float,
) -> TrainTrajectory:
    return TrainTrajectory(
        train_id=train_id,
        service_name=train_id,
        train_type=train_type,
        direction=direction,
        cycle_id="SYN",
        origin_code="ORG",
        dest_code="DST",
        origin_km=km_start,
        dest_km=km_end,
        stop_calls=(),
        waypoints=(
            Waypoint(minute=minute_start, km=km_start, status="RUNNING", line=line),
            Waypoint(minute=minute_end, km=km_end, status="RUNNING", line=line),
        ),
        window=TimeWindow(start_min=minute_start, end_min=minute_end),
        uses_mid_ghat=False,
        assigned_mission="",
    )


def _bundle(
    bundle_id: str,
    km: float,
    line: str,
    defect_type: DefectType,
    severity: int,
    duration_min: float,
    *,
    block_type: BlockType | None = None,
    machine: MachineType | None = None,
    power_sector_id: str | None = None,
    urgency_score: float = 50.0,
) -> MaintenanceBundle:
    defect: TrackDefect = _make_defect(f"{bundle_id}_D", km, line, defect_type, severity)
    chosen_block = defect.block_type if block_type is None else block_type
    chosen_machine = defect.required_machine if machine is None else machine
    chosen_sector = defect.power_sector_id if power_sector_id is None else power_sector_id
    return MaintenanceBundle(
        bundle_id=bundle_id,
        track_line=line,
        section_id=defect.section_id,
        start_km=round(km - 0.5, 3),
        end_km=round(km + 0.5, 3),
        defects=(defect,),
        required_duration_min=duration_min,
        required_machine=chosen_machine,
        block_type=chosen_block,
        power_sector_id=chosen_sector,
        cumulative_priority=float(severity),
        urgency_score=urgency_score,
        is_mandatory=severity >= 80,
    )


def _zone_occupancy(trajectory: TrainTrajectory, bundle: MaintenanceBundle) -> tuple[int, int]:
    block_lo, block_hi, _line = protection_zone(bundle)
    occupancy = _block_entry_exit_minutes(trajectory, block_lo, block_hi)
    assert occupancy is not None
    return occupancy


def _delay_min(result: SolverResult, train_id: str) -> int:
    matches = [item.delay_min for item in result.delayed_trains if item.train_id == train_id]
    assert len(matches) <= 1
    return matches[0] if matches else 0


def _shares_minute(block_start: int, block_end: int, train_in: int, train_out: int) -> bool:
    """True when any minute is inside both half-open intervals.

    The no-crash disjunction allows the train to clear on the same minute
    the possession opens (exit <= start), so the shared boundary is not
    a simultaneous occupation.
    """
    return any(train_in <= minute < train_out for minute in range(block_start, block_end))


def _solve(
    bundles: list[MaintenanceBundle] | tuple[MaintenanceBundle, ...],
    trains: list[TrainTrajectory] | tuple[TrainTrajectory, ...],
    *,
    earliest_start_min: int = 0,
    latest_end_min: int | None = None,
) -> SolverResult:
    return solve_corridor_schedule(
        bundles,
        trains,
        time_limit_sec=_TIME_LIMIT_SEC,
        earliest_start_min=earliest_start_min,
        latest_end_min=latest_end_min,
    )


def test_no_crash_law_block_and_train_never_share_a_minute() -> None:
    bundle = _bundle("CRASH", 70.0, LINE_DN, DefectType.WELD_FAILURE, 40, 40.0)
    train = _train("EXP_CRASH", TYPE_EXPRESS, DIR_DN, LINE_DN, 69.0, 72.0, 0.0, 60.0)
    train_in, train_out = _zone_occupancy(train, bundle)
    envelope = calculate_transit_envelope(bundle)
    assert _shares_minute(0, envelope, train_in, train_out)

    result = _solve([bundle], [train])
    assert result.status in ("OPTIMAL", "FEASIBLE")
    assert math.isfinite(result.chaos_score)
    assert len(result.blocks) == 1
    block = result.blocks[0]
    delay = _delay_min(result, train.train_id)
    assert not _shares_minute(block.start_min, block.end_min, train_in + delay, train_out + delay)
    assert block.end_min - block.start_min == envelope


@pytest.mark.parametrize("train_type", [TYPE_PREMIUM, TYPE_SUPERFAST])
def test_vip_protection_keeps_delay_at_zero_and_moves_the_block(train_type: str) -> None:
    assert MAX_PATIENCE_MIN[train_type] == 0
    bundle = _bundle("VIP", 70.0, LINE_DN, DefectType.WELD_FAILURE, 40, 40.0)
    train = _train("VIP_TRAIN", train_type, DIR_DN, LINE_DN, 69.0, 72.0, 0.0, 60.0)
    train_in, train_out = _zone_occupancy(train, bundle)
    envelope = calculate_transit_envelope(bundle)
    assert envelope > train_in
    assert _shares_minute(0, envelope, train_in, train_out)

    result = _solve([bundle], [train])
    assert result.status in ("OPTIMAL", "FEASIBLE")
    assert _delay_min(result, train.train_id) == 0
    block = result.blocks[0]
    assert block.start_min == train_out
    assert not _shares_minute(block.start_min, block.end_min, train_in, train_out)


@pytest.mark.parametrize("train_type", [TYPE_EXPRESS, TYPE_LOCAL])
def test_max_patience_caps_displaced_passenger_delay(train_type: str) -> None:
    patience = MAX_PATIENCE_MIN[train_type]
    assert patience == 15
    bundle = _bundle("PATIENCE", 70.5, LINE_DN, DefectType.WELD_FAILURE, 40, 25.0)
    # Both waypoints sit inside the 70.0–71.0 worksite, so occupancy is exactly
    # [115, 120]. The 30-minute envelope cannot finish before minute 115, and
    # the latest legal start (115) is still before the train clears at 120,
    # so the only feasible branch is to hold the train for the full 15 minutes.
    train = _train("HELD", train_type, DIR_DN, LINE_DN, 70.2, 70.8, 115.0, 120.0)
    envelope = calculate_transit_envelope(bundle)
    assert envelope == 30
    train_in, train_out = _zone_occupancy(train, bundle)
    assert (train_in, train_out) == (115, 120)

    result = _solve([bundle], [train], earliest_start_min=100, latest_end_min=145)
    assert result.status in ("OPTIMAL", "FEASIBLE")
    delay = _delay_min(result, train.train_id)
    assert 0 < delay <= patience
    assert delay == patience
    block = result.blocks[0]
    assert (block.start_min, block.end_min) == (100, 130)
    assert not _shares_minute(block.start_min, block.end_min, train_in + delay, train_out + delay)


def test_machinery_pool_serialises_single_capacity_bcm() -> None:
    first = _bundle("BCM_A", 60.0, LINE_DN, DefectType.DEEP_SCREENING, 50, 40.0)
    second = _bundle("BCM_B", 80.0, LINE_DN, DefectType.DEEP_SCREENING, 50, 40.0)
    assert first.required_machine is MachineType.BCM_MACHINE
    assert second.required_machine is MachineType.BCM_MACHINE

    alone_first = _solve([first], [])
    alone_second = _solve([second], [])
    assert alone_first.status == "OPTIMAL"
    assert alone_second.status == "OPTIMAL"
    assert _shares_minute(
        alone_first.blocks[0].start_min,
        alone_first.blocks[0].end_min,
        alone_second.blocks[0].start_min,
        alone_second.blocks[0].end_min,
    )

    joint = _solve([first, second], [])
    assert joint.status == "OPTIMAL"
    assert math.isfinite(joint.chaos_score)
    by_id = {block.bundle_id: block for block in joint.blocks}
    left = by_id[first.bundle_id]
    right = by_id[second.bundle_id]
    assert left.end_min <= right.start_min or right.end_min <= left.start_min
    assert left.machine_type == MachineType.BCM_MACHINE.value
    assert right.machine_type == MachineType.BCM_MACHINE.value


def test_power_sector_isolation_constrains_feeder_line_not_parallel_track() -> None:
    feeder_sector = "PSS_CSMT_DR_DN_FAST"
    bundle = _bundle(
        "POWER",
        5.0,
        LINE_UP_FAST,
        DefectType.WELD_FAILURE,
        40,
        30.0,
        block_type=BlockType.POWER_AND_TRAFFIC,
        machine=MachineType.NONE,
        power_sector_id=feeder_sector,
    )
    _lo, _hi, isolated_line = protection_zone(bundle)
    assert isolated_line == get_power_subsector(feeder_sector).isolated_line == LINE_DN_FAST
    assert bundle.track_line == LINE_UP_FAST
    assert isolated_line != bundle.track_line

    feeder = _train("FEEDER", TYPE_PREMIUM, DIR_DN, LINE_DN_FAST, 1.0, 8.0, 0.0, 40.0)
    direct = _train("DIRECT", TYPE_PREMIUM, DIR_UP, LINE_UP_FAST, 1.0, 8.0, 0.0, 40.0)
    block_lo, block_hi, _line = protection_zone(bundle)
    assert _trajectory_uses_line_in_range(feeder, isolated_line, block_lo, block_hi)
    assert not _trajectory_uses_line_in_range(direct, isolated_line, block_lo, block_hi)
    assert _trajectory_uses_line_in_range(direct, bundle.track_line, block_lo, block_hi)

    feeder_in, feeder_out = _zone_occupancy(feeder, bundle)
    direct_in, direct_out = _zone_occupancy(direct, bundle)
    envelope = calculate_transit_envelope(bundle)
    assert _shares_minute(0, envelope, feeder_in, feeder_out)
    assert _shares_minute(0, envelope, direct_in, direct_out)

    isolated = _solve([bundle], [feeder])
    assert isolated.status == "OPTIMAL"
    assert _delay_min(isolated, feeder.train_id) == 0
    feeder_block = isolated.blocks[0]
    assert feeder_block.start_min == feeder_out
    assert not _shares_minute(feeder_block.start_min, feeder_block.end_min, feeder_in, feeder_out)

    parallel = _solve([bundle], [direct])
    assert parallel.status == "OPTIMAL"
    assert _delay_min(parallel, direct.train_id) == 0
    direct_block = parallel.blocks[0]
    assert direct_block.start_min == 0
    assert _shares_minute(direct_block.start_min, direct_block.end_min, direct_in, direct_out)


def test_infeasible_solver_fallback_uses_earliest_theoretical_slot() -> None:
    """A clock window exists, but a zero-patience train covers all of it.

    solve_corridor_schedule's docstring assigns INFEASIBLE / UNKNOWN results
    to [0, envelope] with an infinite chaos score. The pre-solve guard that
    rejects a numeric window (earliest + envelope past latest_end) returns
    before that fallback and leaves blocks empty, so this case keeps a legal
    minute range and lets the CP-SAT model itself come back infeasible.
    """
    bundle = _bundle("NO_WINDOW", 70.0, LINE_DN, DefectType.WELD_FAILURE, 40, 35.0)
    envelope = calculate_transit_envelope(bundle)
    earliest = 100
    latest_end = earliest + envelope + 20
    assert earliest + envelope <= latest_end

    wall = _train("WALL", TYPE_PREMIUM, DIR_DN, LINE_DN, 70.0, 70.0, 0.0, 400.0)
    train_in, train_out = _zone_occupancy(wall, bundle)
    max_start = latest_end - envelope
    assert train_out > max_start
    assert train_in < earliest + envelope
    assert MAX_PATIENCE_MIN[TYPE_PREMIUM] == 0

    result = _solve([bundle], [wall], earliest_start_min=earliest, latest_end_min=latest_end)
    assert result.status == "INFEASIBLE"
    assert math.isinf(result.chaos_score)
    assert math.isinf(result.best_bound)
    assert result.delayed_trains == ()
    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.bundle_id == bundle.bundle_id
    assert (block.start_min, block.end_min) == (0, envelope)
    assert block.machine_type == bundle.required_machine.value
    assert block.power_sector_id == bundle.power_sector_id
