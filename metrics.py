"""
metrics.py
====================================================================
Central Railway CP-SAT schedule telemetry engine for the CRIS VDU
dashboard.

Evaluates the mathematical ROI of the AI solver output:
  • Spatial Bundling Efficiency  (Formula 2 gain %)
  • Machine utilisation hours per plant type
  • Passenger vs freight delay impact analysis
  • Chaos Score pass-through and dashboard report formatting

Receives structured inputs from defects.py and solver.py only.
Contains zero solver math; all CP-SAT logic lives in solver.py.
====================================================================
"""

from __future__ import annotations

from dataclasses import dataclass

import defects
from defects import MachineType, MaintenanceBundle, TrackDefect  # noqa: F401 — TrackDefect re-exported
from solver import DelayedTrain, ScheduledBlock, SolverResult
from timetable import (
    ALL_TRAJECTORIES,
    TYPE_EXPRESS,
    TYPE_LOCAL,
    TYPE_PREMIUM,
    TYPE_SUPERFAST,
)

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_PASSENGER_TYPES: frozenset[str] = frozenset(
    {TYPE_PREMIUM, TYPE_SUPERFAST, TYPE_EXPRESS, TYPE_LOCAL}
)

# Flat train_id → train_type map built once from the full 24-hour WTT.
# Covers every cycle suffix (e.g. "12123_C1", "BNK1_C3").
_TRAIN_TYPE_LOOKUP: dict[str, str] = {
    trajectory.train_id: trajectory.train_type
    for trajectory in ALL_TRAJECTORIES
}


# ---------------------------------------------------------------------------
# Analytics output dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScheduleMetrics:
    """
    Frozen analytics payload produced after a single CP-SAT solve run.

    All integer minute fields represent track-possession clock time so they
    can be directly compared to WTT durations without unit conversion.
    """

    total_defects_cleared: int
    standalone_downtime_min: int     # Sum of individual defect block durations
    bundled_downtime_min: int        # Sum of bundle required durations (excl. transit)
    efficiency_gain_pct: float       # Spatial Bundling Formula 2 ROI %
    passenger_trains_delayed: int
    freight_trains_delayed: int
    total_delay_minutes: int
    machine_utilization_hours: dict[str, float]  # Machine name → active hours deployed
    chaos_score: float
    cascade_impact_score: float      # Post-hoc knock-on cost; not part of the CP-SAT objective


# ---------------------------------------------------------------------------
# Spatial Bundling Efficiency Engine
# ---------------------------------------------------------------------------


def calculate_bundling_efficiency(
    scheduled_bundle_ids: set[str],
    bundles: tuple[MaintenanceBundle, ...],
) -> tuple[int, int, float]:
    """
    Compute the spatial bundling gain % ROI for the set of bundles that
    the CP-SAT solver actually scheduled.

    Parameters
    ----------
    scheduled_bundle_ids
        Set of ``bundle_id`` strings present in ``SolverResult.blocks``.
    bundles
        Full catalogue of ``MaintenanceBundle`` objects from ``defects.py``.

    Returns
    -------
    (standalone_downtime_min, bundled_downtime_min, gain_pct)

    standalone_downtime_min
        Sum of ``defect.required_block_min`` across every defect inside
        the scheduled bundles — the theoretical track-out time if each
        defect were addressed in its own uncoordinated possession.
    bundled_downtime_min
        Sum of ``bundle.required_duration_min`` for the scheduled bundles,
        excluding machine transit time which is absorbed by the solver
        envelope in ``solver.calculate_transit_envelope()``.
    gain_pct
        Formula 2 ROI: ``(1 − bundled / standalone) × 100.0``, rounded
        to 2 decimal places.  Returns ``0.0`` when no defects are scheduled.
    """
    active_bundles: list[MaintenanceBundle] = [
        bundle for bundle in bundles if bundle.bundle_id in scheduled_bundle_ids
    ]

    standalone_downtime_min: int = round(
        sum(
            defect.required_block_min
            for bundle in active_bundles
            for defect in bundle.defects
        )
    )
    bundled_downtime_min: int = round(
        sum(bundle.required_duration_min for bundle in active_bundles)
    )

    if standalone_downtime_min == 0:
        gain_pct: float = 0.0
    else:
        gain_pct = round(
            (1.0 - bundled_downtime_min / standalone_downtime_min) * 100.0,
            2,
        )

    return standalone_downtime_min, bundled_downtime_min, gain_pct


def calculate_spatial_bundling_proof(
    bundles: tuple[MaintenanceBundle, ...] | list[MaintenanceBundle],
) -> tuple[int, int, float, int, int]:
    """Formula 2 on clusters that actually share a possession (two or more jobs).

    Solo jobs always look 15 minutes worse than working them alone, because
    mobilisation sits on the bundle clock. The dashboard proof therefore uses
    only the multi-defect clusters: nearby jobs on the same line and feeder.
    """
    clustered: list[MaintenanceBundle] = [
        bundle for bundle in bundles if len(bundle.defects) >= 2
    ]
    standalone_downtime_min: int = round(
        sum(
            defect.required_block_min
            for bundle in clustered
            for defect in bundle.defects
        )
    )
    bundled_downtime_min: int = round(
        sum(bundle.required_duration_min for bundle in clustered)
    )
    if standalone_downtime_min == 0:
        gain_pct: float = 0.0
    else:
        gain_pct = round(
            (1.0 - bundled_downtime_min / standalone_downtime_min) * 100.0,
            2,
        )
    defect_count: int = sum(len(bundle.defects) for bundle in clustered)
    return standalone_downtime_min, bundled_downtime_min, gain_pct, len(clustered), defect_count


# ---------------------------------------------------------------------------
# Machine Utilisation Tracker
# ---------------------------------------------------------------------------


def calculate_machine_utilization(
    blocks: tuple[ScheduledBlock, ...],
) -> dict[str, float]:
    """
    Accumulate active deployment hours per division machine type from the
    solved possession blocks.

    Blocks with ``machine_type == "NONE"`` represent gang-only possessions
    that require no trackside plant and are excluded from the tally.

    Parameters
    ----------
    blocks
        Tuple of ``ScheduledBlock`` objects from ``SolverResult.blocks``.

    Returns
    -------
    dict[str, float]
        Mapping of machine-type name string (e.g. ``"TIE_TAMPER"``) to total
        hours actively deployed during the scheduled corridor day, rounded
        to 2 decimal places.
    """
    hours: dict[str, float] = {}
    for block in blocks:
        if block.machine_type == MachineType.NONE.value:
            continue
        block_hours: float = (block.end_min - block.start_min) / 60.0
        hours[block.machine_type] = hours.get(block.machine_type, 0.0) + block_hours

    return {machine: round(total_h, 2) for machine, total_h in hours.items()}


# ---------------------------------------------------------------------------
# Primary Metrics Generator
# ---------------------------------------------------------------------------


def generate_metrics(
    bundles: tuple[MaintenanceBundle, ...],
    solver_result: SolverResult,
) -> ScheduleMetrics:
    """
    Compute the full ``ScheduleMetrics`` analytics payload from a CP-SAT
    solve result.

    Aggregation steps
    -----------------
    1. Extract the set of scheduled bundle IDs from ``solver_result.blocks``.
    2. Delegate efficiency calculation to ``calculate_bundling_efficiency()``.
    3. Count cleared defects by iterating over the active bundles.
    4. Categorise delayed trains into passenger vs freight using the WTT
       type lookup; accumulate total delay minutes.
    5. Score post-hoc downstream cascade impact for each delayed train.
    6. Delegate machine utilisation to ``calculate_machine_utilization()``.
    7. Return a frozen ``ScheduleMetrics`` instance.

    Parameters
    ----------
    bundles
        All spatial maintenance clusters that were passed to the solver.
    solver_result
        Frozen output of ``solver.solve_corridor_schedule()``.

    Returns
    -------
    ScheduleMetrics
        Frozen analytics dataclass ready for CRIS VDU dashboard rendering.
    """
    scheduled_bundle_ids: set[str] = {
        block.bundle_id for block in solver_result.blocks
    }

    standalone_min, bundled_min, gain_pct = calculate_bundling_efficiency(
        scheduled_bundle_ids, bundles
    )

    active_bundles: tuple[MaintenanceBundle, ...] = tuple(
        bundle for bundle in bundles if bundle.bundle_id in scheduled_bundle_ids
    )
    total_defects_cleared: int = sum(
        len(bundle.defects) for bundle in active_bundles
    )

    passenger_delayed: int = 0
    freight_delayed: int = 0
    total_delay_minutes: int = 0

    for delayed_train in solver_result.delayed_trains:
        total_delay_minutes += delayed_train.delay_min
        train_type: str = _TRAIN_TYPE_LOOKUP.get(delayed_train.train_id, "")
        if train_type in _PASSENGER_TYPES:
            passenger_delayed += 1
        else:
            freight_delayed += 1

    # Simplification: two following services are assumed affected by each
    # primary delay. The real knock-on count depends on the timetable behind
    # that train; this figure is reporting-only and is not a solver penalty.
    total_cascade_impact: float = 0.0
    for delayed_train in solver_result.delayed_trains:
        train_type: str = _TRAIN_TYPE_LOOKUP.get(delayed_train.train_id, "")
        total_cascade_impact += defects.downstream_cascade_penalty(
            delayed_train.delay_min,
            train_type,
            downstream_count=2,
        )

    machine_hours: dict[str, float] = calculate_machine_utilization(solver_result.blocks)

    return ScheduleMetrics(
        total_defects_cleared=total_defects_cleared,
        standalone_downtime_min=standalone_min,
        bundled_downtime_min=bundled_min,
        efficiency_gain_pct=gain_pct,
        passenger_trains_delayed=passenger_delayed,
        freight_trains_delayed=freight_delayed,
        total_delay_minutes=total_delay_minutes,
        machine_utilization_hours=machine_hours,
        chaos_score=solver_result.chaos_score,
        cascade_impact_score=total_cascade_impact,
    )


# ---------------------------------------------------------------------------
# Dashboard Report Formatter
# ---------------------------------------------------------------------------


def tsr_time_loss_min(distance_km: float, normal_kmh: float, capped_kmh: float) -> float:
    """Extra running minutes caused by capping speed over a chainage length."""
    distance = max(0.0, float(distance_km))
    normal = float(normal_kmh)
    capped = float(capped_kmh)
    if distance <= 0.0 or normal <= 0.0 or capped <= 0.0 or capped >= normal:
        return 0.0
    normal_min = distance / normal * 60.0
    capped_min = distance / capped * 60.0
    return round(capped_min - normal_min, 2)


def get_metrics_report(metrics: ScheduleMetrics) -> dict[str, str]:
    """
    Return a strictly formatted key→value dictionary for the CRIS VDU
    frontend.

    All values are human-readable strings intended for direct rendering on
    the dark-console dashboard without further transformation by the caller.

    Keys
    ----
    ``"Network ROI (Gain %)"``
        Spatial bundling efficiency expressed as a percentage string.
    ``"Defects Resolved"``
        Total number of track defects cleared in the scheduled window.
    ``"Track Time Saved (Mins)"``
        Difference between standalone and bundled possession durations.
    ``"Passenger Delays"``
        Count of affected passenger services and aggregate delay minutes.
    ``"Downstream Cascade Impact"``
        Post-hoc knock-on cost of each delayed train plus two following services.
    ``"Machine Deployment"``
        Pipe-separated per-machine active hours, or a nil-deployment label.
    """
    track_time_saved: int = metrics.standalone_downtime_min - metrics.bundled_downtime_min

    if metrics.machine_utilization_hours:
        machine_parts: list[str] = [
            f"{machine_name}: {hours:.2f} h"
            for machine_name, hours in sorted(metrics.machine_utilization_hours.items())
        ]
        machine_summary: str = " | ".join(machine_parts)
    else:
        machine_summary = "No plant deployed"

    return {
        "Network ROI (Gain %)": f"{metrics.efficiency_gain_pct:.2f} %",
        "Defects Resolved": str(metrics.total_defects_cleared),
        "Track Time Saved (Mins)": str(track_time_saved),
        "Passenger Delays": (
            f"{metrics.passenger_trains_delayed} trains / {metrics.total_delay_minutes} min total"
        ),
        "Downstream Cascade Impact": f"{metrics.cascade_impact_score:.1f}",
        "Machine Deployment": machine_summary,
    }


__all__ = [
    "ScheduleMetrics",
    "calculate_bundling_efficiency",
    "calculate_machine_utilization",
    "generate_metrics",
    "get_metrics_report",
    "tsr_time_loss_min",
]
