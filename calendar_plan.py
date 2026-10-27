"""
calendar_plan.py
====================================================================
Multi-day possession planning layer for Central Railway corridor
maintenance.

This module does NOT contain any CP-SAT logic. It decides which
maintenance bundles compete for which calendar day's possession
window, then hands each day's bundle set to
solver.solve_corridor_schedule() unchanged. solver.py remains the
sole owner of cp_model logic.

Concepts
--------
PossessionWindow  — the block of clock-minutes available on a given
                     day (nightly, Sunday mega-block, extended block, or
                     daytime off-peak).
DayPlan           — one day's outcome: which bundles were assigned,
                     and the real CP-SAT SolverResult for that day.
HorizonPlan       — a run of consecutive DayPlans (7 days for a
                     weekly view, 28 for a monthly view).
====================================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final, Sequence

from defects import (
    DIVISION_MACHINERY_LIMITS,
    MachineType,
    MaintenanceBundle,
    cluster_defects_into_bundles,
    compute_priority_urgency_score,
)
from solver import SolverResult, calculate_transit_envelope, solve_corridor_schedule
from timetable import TYPE_PREMIUM, TYPE_SUPERFAST, TrainTrajectory, get_all_trajectories

# ---------------------------------------------------------------------------
# Possession window calendar
# ---------------------------------------------------------------------------

NIGHTLY_START_MIN: Final[int] = 0       # 00:00
NIGHTLY_END_MIN: Final[int] = 240       # 04:00 — routine weeknight window
MEGA_BLOCK_START_MIN: Final[int] = 0    # 00:00
MEGA_BLOCK_END_MIN: Final[int] = 420    # 07:00 — Sunday wide window
EXTENDED_START_MIN: Final[int] = 0      # 00:00
EXTENDED_END_MIN: Final[int] = 720      # 12:00 — total traffic block for an oversized job
DAY_OFFPEAK_START_MIN: Final[int] = 690  # 11:30
DAY_OFFPEAK_END_MIN: Final[int] = 870    # 14:30 — daytime off-peak window

LABEL_NIGHTLY: Final[str] = "NIGHTLY"
LABEL_MEGA_BLOCK: Final[str] = "MEGA_BLOCK"
LABEL_EXTENDED: Final[str] = "EXTENDED_BLOCK"
LABEL_DAY_OFFPEAK: Final[str] = "DAY_OFFPEAK"

SUNDAY_WEEKDAY: Final[int] = 6  # date.weekday(): Monday=0 ... Sunday=6


@dataclass(frozen=True, slots=True)
class PossessionWindow:
    """The clock-minute span available for maintenance on one calendar day."""

    start_min: int
    end_min: int
    label: str

    @property
    def capacity_min(self) -> float:
        return float(self.end_min - self.start_min)


def get_window_for_date(day: date, force_extended: bool = False) -> PossessionWindow:
    """Sunday gets the wide mega-block window; every other day gets the nightly window.

    ``force_extended=True`` opens a 12-hour total traffic block on that date
    regardless of weekday, for a job that cannot fit inside a mega-block.
    """
    if force_extended:
        return PossessionWindow(EXTENDED_START_MIN, EXTENDED_END_MIN, LABEL_EXTENDED)
    if day.weekday() == SUNDAY_WEEKDAY:
        return PossessionWindow(MEGA_BLOCK_START_MIN, MEGA_BLOCK_END_MIN, LABEL_MEGA_BLOCK)
    return PossessionWindow(NIGHTLY_START_MIN, NIGHTLY_END_MIN, LABEL_NIGHTLY)


# ---------------------------------------------------------------------------
# Urgency decay
# ---------------------------------------------------------------------------


def decayed_urgency(bundle: MaintenanceBundle, extra_days: int) -> float:
    """
    Formula 1, re-evaluated as if every member defect has sat `extra_days`
    longer without rectification. Unclipped (clip=False), matching how
    solver.py weights the CP-SAT objective — a bundle must not lose
    ranking resolution to the [0, 100] display cap while planning ahead.
    """
    return max(
        compute_priority_urgency_score(
            defect,
            days_overdue=defect.days_overdue + extra_days,
            has_tsr=defect.has_tsr,
            clip=False,
        )
        for defect in bundle.defects
    )


# ---------------------------------------------------------------------------
# Per-day bundle selection (greedy bin-pack by envelope + machine pool)
# ---------------------------------------------------------------------------


def _fits_machine_pool(
    bundle: MaintenanceBundle,
    committed: dict[MachineType, int],
) -> dict[MachineType, int] | None:
    """
    Return the machine-count map AFTER adding `bundle`, or None if adding it
    would exceed DIVISION_MACHINERY_LIMITS for any machine type it needs.
    """
    projected = dict(committed)
    for machine in bundle.machines_required():
        projected[machine] = projected.get(machine, 0) + 1
        if projected[machine] > DIVISION_MACHINERY_LIMITS.get(machine, 0):
            return None
    return projected


def _release_oversized_mandatory(
    remaining: Sequence[MaintenanceBundle],
    window: PossessionWindow,
    current_date: date,
) -> list[MaintenanceBundle]:
    """
    A safety-critical defect clustered with lighter work can inherit an
    envelope larger than the night window, so the greedy packer never
    even offers it to the solver. Split those bundles into one bundle
    per defect before ranking, and keep the pieces in the horizon pool.
    """
    released: list[MaintenanceBundle] = []
    for bundle in remaining:
        oversized = (
            bundle.is_mandatory
            and len(bundle.defects) > 1
            and calculate_transit_envelope(bundle) > window.capacity_min
        )
        if not oversized:
            released.append(bundle)
            continue
        pieces = list(
            cluster_defects_into_bundles(bundle.defects, max_spatial_gap_km=0)
        )
        print(
            f"[horizon] {current_date.isoformat()}: {bundle.bundle_id} "
            f"envelope exceeds {window.label} ({window.capacity_min:.0f} min); "
            f"splitting into {', '.join(piece.bundle_id for piece in pieces)}"
        )
        released.extend(pieces)
    return released


def _select_bundles_for_day(
    candidates: Sequence[MaintenanceBundle],
    window: PossessionWindow,
    extra_days: int,
    already_used_min: float = 0.0,
    machine_committed: dict[MachineType, int] | None = None,
) -> list[MaintenanceBundle]:
    """
    Greedy fit: rank remaining bundles mandatory-first, then by decayed
    urgency (most urgent first) within each group, then pack as many as
    fit inside this day's window capacity and machinery pool. Anything
    that doesn't fit rolls to the next day automatically — it simply
    isn't picked here.

    ``already_used_min`` and ``machine_committed`` account for an oversized
    bundle already reserved on an EXTENDED_BLOCK day, so the rest of the
    day's candidates are packed into whatever capacity remains.
    """
    ranked = sorted(
        candidates,
        key=lambda bundle: (not bundle.is_mandatory, -decayed_urgency(bundle, extra_days)),
    )
    picked: list[MaintenanceBundle] = []
    used_min = float(already_used_min)
    machine_committed = dict(machine_committed or {})

    for bundle in ranked:
        envelope = calculate_transit_envelope(bundle)
        if used_min + envelope > window.capacity_min:
            continue
        projected = _fits_machine_pool(bundle, machine_committed)
        if projected is None:
            continue
        picked.append(bundle)
        used_min += envelope
        machine_committed = projected

    return picked


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DayPlan:
    """One calendar day's outcome: which bundles were attempted, and the
    real CP-SAT SolverResult for that day (None if nothing was assigned)."""

    calendar_date: date
    window: PossessionWindow
    assigned_bundle_ids: tuple[str, ...]
    result: SolverResult | None
    trains_in_play: int


@dataclass(frozen=True, slots=True)
class HorizonPlan:
    """A run of consecutive DayPlans, plus any bundle that never found a
    day within the horizon (needs a longer horizon or urgent manual review)."""

    start_date: date
    days: tuple[DayPlan, ...]
    unscheduled_bundle_ids: tuple[str, ...]


# ---------------------------------------------------------------------------
# Day-of-week train filter (timetable itself is a fixed 24-hour set)
# ---------------------------------------------------------------------------


def _trains_for_window(
    trains: Sequence[TrainTrajectory],
    window: PossessionWindow,
) -> tuple[TrainTrajectory, ...]:
    """
    On a MEGA_BLOCK or EXTENDED_BLOCK day, zero-patience premier services
    (Premium, Superfast) are assumed suspended or diverted, matching
    real-world practice for a wide corridor block. Every other train type
    (Express, Local, Freight, Banker) still runs and is still fully
    protected by the solver's constraints. NIGHTLY days are
    unaffected — no trains are filtered.
    """
    if window.label not in (LABEL_MEGA_BLOCK, LABEL_EXTENDED):
        return tuple(trains)
    return tuple(
        t for t in trains
        if t.train_type not in (TYPE_PREMIUM, TYPE_SUPERFAST)
    )


def _book_one_job_at_a_time(
    candidates: Sequence[MaintenanceBundle],
    window: PossessionWindow,
    trains: Sequence[TrainTrajectory],
    extra_days: int,
    time_limit_sec: float,
) -> tuple[list[MaintenanceBundle], SolverResult | None]:
    """Book whatever really fits a window, one job at a time.

    Mandatory bundles are tried first, then the highest decayed urgency.
    A candidate is skipped when its transit envelope would push the booked
    minutes over the window, or when it would exceed the machine pool.
    Each remaining candidate is solved together with the jobs already
    accepted. It stays only when that solve is OPTIMAL or FEASIBLE.
    """
    ranked = sorted(
        candidates,
        key=lambda bundle: (not bundle.is_mandatory, -decayed_urgency(bundle, extra_days)),
    )
    accepted: list[MaintenanceBundle] = []
    used_min = 0.0
    machine_committed: dict[MachineType, int] = {}
    last_result: SolverResult | None = None

    for bundle in ranked:
        envelope = calculate_transit_envelope(bundle)
        if used_min + envelope > window.capacity_min:
            continue
        projected = _fits_machine_pool(bundle, machine_committed)
        if projected is None:
            continue
        trial = solve_corridor_schedule(
            [*accepted, bundle],
            trains,
            time_limit_sec=time_limit_sec,
            earliest_start_min=window.start_min,
            latest_end_min=window.end_min,
        )
        if trial.status not in ("OPTIMAL", "FEASIBLE"):
            continue
        accepted.append(bundle)
        used_min += envelope
        machine_committed = projected
        last_result = trial

    return accepted, last_result


def _book_offpeak_day(
    candidates: Sequence[MaintenanceBundle],
    trains: Sequence[TrainTrajectory],
    current_date: date,
    extra_days: int,
) -> tuple[DayPlan, set[str]] | None:
    """Book the 11:30-14:30 off-peak window against every train.

    Returns nothing when no job fits, so a quiet afternoon adds no row.
    """
    window = PossessionWindow(
        DAY_OFFPEAK_START_MIN,
        DAY_OFFPEAK_END_MIN,
        LABEL_DAY_OFFPEAK,
    )
    booked, result = _book_one_job_at_a_time(
        candidates,
        window,
        trains,
        extra_days,
        1.0,
    )
    if not booked or result is None:
        return None
    booked_ids = {bundle.bundle_id for bundle in booked}
    plan = DayPlan(
        calendar_date=current_date,
        window=window,
        assigned_bundle_ids=tuple(bundle.bundle_id for bundle in booked),
        result=result,
        trains_in_play=len(trains),
    )
    return plan, booked_ids


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _sundays_for_oversized_bundles(
    bundles: Sequence[MaintenanceBundle],
    start_date: date,
    num_days: int,
) -> dict[date, tuple[str, ...]]:
    """
    Sundays that must open as EXTENDED_BLOCK.

    A bundle whose transit envelope is larger than the 420-minute mega-block
    cannot be offered on a normal Sunday. Each such bundle reserves the first
    Sunday in the horizon that still has room inside the 720-minute extended
    window. Other work that day is still packed against that wider window.
    """
    mega_capacity = float(MEGA_BLOCK_END_MIN - MEGA_BLOCK_START_MIN)
    extended_capacity = float(EXTENDED_END_MIN - EXTENDED_START_MIN)
    sundays = [
        start_date + timedelta(days=offset)
        for offset in range(max(0, num_days))
        if (start_date + timedelta(days=offset)).weekday() == SUNDAY_WEEKDAY
    ]
    committed: dict[date, float] = {sunday: 0.0 for sunday in sundays}
    assigned: dict[date, list[str]] = {sunday: [] for sunday in sundays}
    for bundle in bundles:
        envelope = float(calculate_transit_envelope(bundle))
        if envelope <= mega_capacity:
            continue
        for sunday in sundays:
            if committed[sunday] + envelope <= extended_capacity:
                committed[sunday] += envelope
                assigned[sunday].append(bundle.bundle_id)
                break
    return {day: tuple(ids) for day, ids in assigned.items() if ids}


def build_horizon_plan(
    bundles: Sequence[MaintenanceBundle],
    start_date: date,
    num_days: int,
    time_limit_sec: float = 3.0,
) -> HorizonPlan:
    """
    Roll forward day by day. A bundle whose envelope exceeds the 420-minute
    mega-block reserves the first Sunday that can hold it, and that Sunday
    opens as a 720-minute EXTENDED_BLOCK. Each day: pick mandatory bundles first, then
    the highest decayed-urgency bundles that fit the day's window and
    machine pool, hand exactly that day's set to the existing CP-SAT
    solver unchanged, and keep whatever the solver actually confirms as
    OPTIMAL/FEASIBLE. A mandatory bundle whose envelope is larger than
    the day's window is split into single-defect bundles before packing,
    so the safety-critical defect is not skipped with the cluster. If a
    solve fails and the day includes a safety-critical bundle, retry
    once with each mandatory bundle split the same way. Anything the
    solver rejects, or anything
    that didn't fit the day's capacity at all, stays in the pool and gets
    re-ranked (with higher decayed urgency) on the next day. If the day's
    joint solve is still not OPTIMAL or FEASIBLE, that window is booked
    one job at a time. Bundles still waiting after the night plan can take
    the same date's daytime off-peak window, except on an extended block.
    """
    remaining: list[MaintenanceBundle] = list(bundles)
    trains: tuple[TrainTrajectory, ...] = get_all_trajectories()
    days: list[DayPlan] = []
    extended_by_date = _sundays_for_oversized_bundles(bundles, start_date, num_days)

    for offset in range(num_days):
        current_date = start_date + timedelta(days=offset)
        reserved_ids = extended_by_date.get(current_date, ())
        window = get_window_for_date(
            current_date,
            force_extended=current_date in extended_by_date,
        )
        remaining = _release_oversized_mandatory(remaining, window, current_date)

        reserved = [bundle for bundle in remaining if bundle.bundle_id in reserved_ids]
        reserved_minutes = sum(calculate_transit_envelope(bundle) for bundle in reserved)
        reserved_machines: dict[MachineType, int] = {}
        for bundle in reserved:
            projected = _fits_machine_pool(bundle, reserved_machines)
            if projected is not None:
                reserved_machines = projected
        others = _select_bundles_for_day(
            [bundle for bundle in remaining if bundle.bundle_id not in reserved_ids],
            window,
            extra_days=offset,
            already_used_min=reserved_minutes,
            machine_committed=reserved_machines,
        )
        day_candidates = [*reserved, *others]
        day_trains = _trains_for_window(trains, window)
        solved_candidates = day_candidates

        if day_candidates:
            result = solve_corridor_schedule(
                day_candidates,
                day_trains,
                time_limit_sec=time_limit_sec,
                earliest_start_min=window.start_min,
                latest_end_min=window.end_min,
            )
            split_succeeded = False
            if result.status not in ("OPTIMAL", "FEASIBLE") and any(
                bundle.is_mandatory for bundle in day_candidates
            ):
                mandatory_ids = [
                    bundle.bundle_id for bundle in day_candidates if bundle.is_mandatory
                ]
                print(
                    f"[horizon] {current_date.isoformat()}: status {result.status} "
                    f"with mandatory bundle(s) {', '.join(mandatory_ids)}; "
                    "retrying as single-defect bundles"
                )
                adjusted: list[MaintenanceBundle] = []
                for bundle in day_candidates:
                    if bundle.is_mandatory:
                        adjusted.extend(
                            cluster_defects_into_bundles(
                                bundle.defects,
                                max_spatial_gap_km=0,
                            )
                        )
                    else:
                        adjusted.append(bundle)
                retry_result = solve_corridor_schedule(
                    adjusted,
                    day_trains,
                    time_limit_sec=time_limit_sec,
                    earliest_start_min=window.start_min,
                    latest_end_min=window.end_min,
                )
                print(
                    f"[horizon] {current_date.isoformat()}: single-defect retry "
                    f"status {retry_result.status}"
                )
                if retry_result.status in ("OPTIMAL", "FEASIBLE"):
                    result = retry_result
                    solved_candidates = adjusted
                    split_succeeded = True
            if (
                window.label == LABEL_EXTENDED
                and reserved
                and result.status not in ("OPTIMAL", "FEASIBLE")
            ):
                held = ", ".join(bundle.bundle_id for bundle in reserved)
                print(
                    f"[horizon] {current_date.isoformat()}: {held} "
                    f"cannot be placed in the extended block ({result.status}); "
                    "packing the rest of the day without it"
                )
                fallback_candidates = _select_bundles_for_day(
                    [bundle for bundle in remaining if bundle.bundle_id not in reserved_ids],
                    window,
                    extra_days=offset,
                )
                if fallback_candidates:
                    fallback_result = solve_corridor_schedule(
                        fallback_candidates,
                        day_trains,
                        time_limit_sec=time_limit_sec,
                        earliest_start_min=window.start_min,
                        latest_end_min=window.end_min,
                    )
                    print(
                        f"[horizon] {current_date.isoformat()}: extended-day fallback "
                        f"status {fallback_result.status}"
                    )
                    if fallback_result.status in ("OPTIMAL", "FEASIBLE"):
                        result = fallback_result
                        solved_candidates = fallback_candidates
                        split_succeeded = False
            if result.status not in ("OPTIMAL", "FEASIBLE"):
                booked, booked_result = _book_one_job_at_a_time(
                    remaining,
                    window,
                    day_trains,
                    offset,
                    time_limit_sec,
                )
                if booked and booked_result is not None:
                    result = booked_result
                    solved_candidates = booked
                    split_succeeded = False
                    print(
                        f"[horizon] {current_date.isoformat()}: {window.label} "
                        f"booked one job at a time: "
                        f"{', '.join(bundle.bundle_id for bundle in booked)}"
                    )
            if split_succeeded:
                # The parent bundle ids are what the horizon pool still holds.
                # A successful split must retire those parents even when the
                # single-defect bundles were given fresh ids.
                confirmed_ids = {bundle.bundle_id for bundle in day_candidates}
            else:
                confirmed_ids = (
                    {block.bundle_id for block in result.blocks}
                    if result.status in ("OPTIMAL", "FEASIBLE")
                    else set()
                )
        else:
            result = None
            confirmed_ids = set()

        days.append(
            DayPlan(
                calendar_date=current_date,
                window=window,
                assigned_bundle_ids=tuple(bundle.bundle_id for bundle in solved_candidates),
                result=result,
                trains_in_play=len(day_trains),
            )
        )

        remaining = [b for b in remaining if b.bundle_id not in confirmed_ids]
        if remaining and window.label != LABEL_EXTENDED:
            offpeak = _book_offpeak_day(remaining, trains, current_date, offset)
            if offpeak is not None:
                offpeak_plan, booked_ids = offpeak
                days.append(offpeak_plan)
                remaining = [b for b in remaining if b.bundle_id not in booked_ids]

    return HorizonPlan(
        start_date=start_date,
        days=tuple(days),
        unscheduled_bundle_ids=tuple(b.bundle_id for b in remaining),
    )


__all__ = [
    "DayPlan",
    "HorizonPlan",
    "DAY_OFFPEAK_END_MIN",
    "DAY_OFFPEAK_START_MIN",
    "EXTENDED_END_MIN",
    "EXTENDED_START_MIN",
    "LABEL_DAY_OFFPEAK",
    "LABEL_EXTENDED",
    "LABEL_MEGA_BLOCK",
    "LABEL_NIGHTLY",
    "PossessionWindow",
    "build_horizon_plan",
    "decayed_urgency",
    "get_window_for_date",
]
