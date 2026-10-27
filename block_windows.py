"""
block_windows.py
====================================================================
Choose the next window and bundle for the Officer and Chief console.

Windows take turns: the night block, then the daytime off-peak block,
then the next day's night block, and so on. The first bundle the
solver can place is the one to show.

A window is already past when it ended before the simulated now
(the simulated day and the simulated minute). A window on today's
simulated date is also unavailable unless it starts at least
PLANNING_LEAD_MIN minutes from now. The laptop clock is not consulted.
====================================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Sequence

import calendar_plan
import sim_clock
import solver
from calendar_plan import PossessionWindow
from defects import MaintenanceBundle
from timetable import TrainTrajectory

MAX_WINDOWS_AHEAD: int = 14
SOLVE_LIMIT_SEC: float = 1.0
PLANNING_LEAD_MIN: int = 30


def _bundle_is_reserved(bundle: MaintenanceBundle) -> bool:
    """True when a sanctioned block already holds any defect in this bundle."""
    import backlog_state

    reserved = backlog_state.scheduled_defect_ids()
    if not reserved:
        return False
    return any(item.defect_id in reserved for item in bundle.defects)


@dataclass(frozen=True, slots=True)
class Slot:
    """One bundle placed in one possession window."""

    bundle: MaintenanceBundle
    window_index: int
    plan_date: date
    window: PossessionWindow
    start_min: int


def window_for_index(base_date: date, index: int) -> tuple[date, PossessionWindow]:
    """The date and possession window for this window number.

    Even numbers are the night window, which is the Sunday mega-block
    when that date is a Sunday. Odd numbers are the daytime off-peak
    window on the same date.
    """
    plan_date = base_date + timedelta(days=index // 2)
    if index % 2 == 0:
        return plan_date, calendar_plan.get_window_for_date(plan_date)
    return plan_date, PossessionWindow(
        calendar_plan.DAY_OFFPEAK_START_MIN,
        calendar_plan.DAY_OFFPEAK_END_MIN,
        calendar_plan.LABEL_DAY_OFFPEAK,
    )


def window_is_past(plan_date: date, window: PossessionWindow) -> bool:
    """True when this window cannot be booked on the simulated clock.

    A window that already ended is past. A window on today's simulated
    date is also unavailable unless its start is at least
    PLANNING_LEAD_MIN minutes from the current simulated minute.
    """
    today = sim_clock.sim_date()
    if plan_date < today:
        return True
    if plan_date > today:
        return False
    now = sim_clock.minute_of_day()
    if float(window.end_min) <= now:
        return True
    return float(window.start_min) < now + PLANNING_LEAD_MIN


def find_next_slot(
    bundles: Sequence[MaintenanceBundle],
    trains: Sequence[TrainTrajectory],
    first_index: int,
    base_date: date,
) -> Slot | None:
    """The next window that can take one bundle, or nothing if none fit.

    Mandatory bundles are tried first, then the highest urgency. Each
    window is searched fully before the next window is tried.
    """
    ranked = sorted(
        bundles,
        key=lambda bundle: (not bundle.is_mandatory, -bundle.urgency_score),
    )
    last_index = first_index + MAX_WINDOWS_AHEAD - 1
    for window_index in range(first_index, last_index + 1):
        plan_date, window = window_for_index(base_date, window_index)
        if window_is_past(plan_date, window):
            continue
        day_trains = calendar_plan._trains_for_window(trains, window)
        for bundle in ranked:
            if _bundle_is_reserved(bundle):
                continue
            if solver.calculate_transit_envelope(bundle) > window.capacity_min:
                continue
            result = solver.solve_corridor_schedule(
                [bundle],
                day_trains,
                time_limit_sec=SOLVE_LIMIT_SEC,
                earliest_start_min=window.start_min,
                latest_end_min=window.end_min,
            )
            if result.status not in ("OPTIMAL", "FEASIBLE"):
                continue
            placed = next(
                (block for block in result.blocks if block.bundle_id == bundle.bundle_id),
                None,
            )
            if placed is None:
                continue
            return Slot(
                bundle=bundle,
                window_index=window_index,
                plan_date=plan_date,
                window=window,
                start_min=placed.start_min,
            )
    return None


def find_option_slots(
    bundle: MaintenanceBundle,
    trains: Sequence[TrainTrajectory],
    first_index: int,
    base_date: date,
    used: Iterable[int] = (),
    count: int = 3,
) -> list[Slot]:
    """Up to `count` feasible windows for this one bundle, in window order.

    Searches at most MAX_WINDOWS_AHEAD indexes from first_index. Indexes in
    `used` are skipped. A window is kept only when the bundle's transit
    envelope fits it and CP-SAT places the bundle inside that window.
    """
    if count <= 0 or _bundle_is_reserved(bundle):
        return []
    used_indexes = {int(index) for index in used}
    last_index = first_index + MAX_WINDOWS_AHEAD - 1
    found: list[Slot] = []
    envelope = solver.calculate_transit_envelope(bundle)
    for window_index in range(first_index, last_index + 1):
        if window_index in used_indexes:
            continue
        plan_date, window = window_for_index(base_date, window_index)
        if window_is_past(plan_date, window):
            continue
        if envelope > window.capacity_min:
            continue
        day_trains = calendar_plan._trains_for_window(trains, window)
        result = solver.solve_corridor_schedule(
            [bundle],
            day_trains,
            time_limit_sec=SOLVE_LIMIT_SEC,
            earliest_start_min=window.start_min,
            latest_end_min=window.end_min,
        )
        if result.status not in ("OPTIMAL", "FEASIBLE"):
            continue
        placed = next(
            (block for block in result.blocks if block.bundle_id == bundle.bundle_id),
            None,
        )
        if placed is None:
            continue
        found.append(
            Slot(
                bundle=bundle,
                window_index=window_index,
                plan_date=plan_date,
                window=window,
                start_min=placed.start_min,
            )
        )
        if len(found) >= count:
            break
    return found
