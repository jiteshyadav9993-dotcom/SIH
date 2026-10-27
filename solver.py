"""
solver.py
====================================================================
Central Railway CP-SAT corridor maintenance scheduling engine.

Google OR-Tools CP-SAT formulation.  Receives structured inputs from
topology.py, timetable.py, and defects.py and produces time-slotted
possession windows that minimise the Chaos Score:

    Z = Σ(W_type · delay_t) + Σ(W_overdue · bundle_start_time)

Hard constraints enforced
─────────────────────────
  Rule A – No Crash Law   : a block zone is never simultaneously
            occupied by a live train and a maintenance gang.
  Rule B – VIP Protection : Premium / Superfast trains carry zero
            delay tolerance (max_patience = 0).
  Rule C – Max Patience   : per-type ceiling on acceptable induced delay.
  Machinery Pooling       : division plant cannot be double-booked beyond
            DIVISION_MACHINERY_LIMITS fleet capacity.
  Power Sector Isolation  : POWER_AND_TRAFFIC blocks close the worksite
            on the isolated feeder line and require earth at both ends
            before work. Parallel running lines stay open.

This file is the sole owner of cp_model logic.  No solver math is placed
in any other module.
====================================================================
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Final, Sequence

from ortools.sat.python import cp_model

from defects import (
    BlockType,
    DIVISION_MACHINERY_LIMITS,
    MaintenanceBundle,
    MachineType,
    get_power_subsector,
)
from timetable import (
    TYPE_BANKER,
    TYPE_EXPRESS,
    TYPE_FREIGHT,
    TYPE_LOCAL,
    TYPE_PREMIUM,
    TYPE_SUPERFAST,
    TrainTrajectory,
)

# ---------------------------------------------------------------------------
# Machine depot geography and mobilisation physics
# ---------------------------------------------------------------------------

_DEPOT_KM: Final[dict[MachineType, float]] = {
    MachineType.TIE_TAMPER: 100.0,   # Karjat Yard
    MachineType.BCM_MACHINE: 54.0,   # Kalyan Yard
    MachineType.TOWER_WAGON: 128.0,  # Lonavala Depot
}

_DEPOT_SPEED_KMPH: Final[dict[MachineType, float]] = {
    MachineType.TIE_TAMPER: 35.0,
    MachineType.BCM_MACHINE: 30.0,
    MachineType.TOWER_WAGON: 40.0,
}

# OHE isolation setup / restoration clearance buffers (minutes)
_CLEARANCE_POWER_AND_TRAFFIC: Final[int] = 15
_CLEARANCE_TRAFFIC_ONLY: Final[int] = 5

# ---------------------------------------------------------------------------
# Objective-function penalty weights
# ---------------------------------------------------------------------------

# W_type — per-minute delay penalty applied once per train in the objective
W_PASSENGER_DELAY: Final[int] = 300   # Premium, Superfast, Express, Local
W_FREIGHT_DELAY: Final[int] = 2       # Freight and Banker (support vehicle)

# W_overdue — forces the solver to prefer early block windows rather than
# packing everything into the late-night trough.
W_POSTPONEMENT: Final[int] = 1

# ---------------------------------------------------------------------------
# Rule C — Max patience per train type (minutes)
# ---------------------------------------------------------------------------

MAX_PATIENCE_MIN: Final[dict[str, int]] = {
    TYPE_PREMIUM:   0,    # Rule B – VIP Protection, zero tolerance
    TYPE_SUPERFAST: 0,    # Rule B – VIP Protection, zero tolerance
    TYPE_EXPRESS:  15,
    TYPE_LOCAL:    15,
    TYPE_FREIGHT: 120,
    TYPE_BANKER:  120,
}

# ---------------------------------------------------------------------------
# Output dataclasses (all frozen — safe to cache and return over REST)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScheduledBlock:
    """A solved maintenance possession window assigned to a bundle."""

    bundle_id: str
    start_min: int
    end_min: int
    machine_type: str
    power_sector_id: str


@dataclass(frozen=True, slots=True)
class DelayedTrain:
    """A WTT service whose departure was displaced to accommodate a block."""

    train_id: str
    original_min: int
    new_min: int
    delay_min: int


@dataclass(frozen=True, slots=True)
class SolverResult:
    """Complete output of one CP-SAT solve run for the corridor."""

    status: str
    solve_time_sec: float
    blocks: tuple[ScheduledBlock, ...]
    delayed_trains: tuple[DelayedTrain, ...]
    chaos_score: float
    best_bound: float


# ---------------------------------------------------------------------------
# Machine transit and block-envelope physics
# ---------------------------------------------------------------------------


def machine_transits(bundle: MaintenanceBundle) -> tuple[dict[str, float | str], ...]:
    """Depot, speed, and one-way run time for every plant the bundle actually needs."""
    centre_km: float = (bundle.start_km + bundle.end_km) / 2.0
    rows: list[dict[str, float | str]] = []
    for machine in bundle.machines_required():
        depot_km: float = _DEPOT_KM[machine]
        speed_kmph: float = _DEPOT_SPEED_KMPH[machine]
        transit_min: float = 60.0 * abs(centre_km - depot_km) / speed_kmph
        rows.append(
            {
                "machine_type": machine.value,
                "depot_km": depot_km,
                "speed_kmh": speed_kmph,
                "transit_min": round(transit_min, 3),
                "site_km": round(centre_km, 3),
            }
        )
    return tuple(rows)


def longest_transit_min(bundle: MaintenanceBundle) -> float:
    """Slowest required machine. The possession cannot start work before it arrives."""
    transits = machine_transits(bundle)
    if not transits:
        return 0.0
    return max(float(row["transit_min"]) for row in transits)


def clearance_min(bundle: MaintenanceBundle) -> int:
    """Minutes after the machines arrive before work may start."""
    if bundle.block_type is BlockType.POWER_AND_TRAFFIC:
        return _CLEARANCE_POWER_AND_TRAFFIC
    return _CLEARANCE_TRAFFIC_ONLY


def calculate_transit_envelope(bundle: MaintenanceBundle) -> int:
    """
    Compute the total block-possession window required for one bundle.

        W_required = 2 · t_transit + T_bundle + B_clearance

    where
        t_transit  = longest one-way depot run of the machines the bundle needs
        T_bundle   = bundle.required_duration_min  (already includes
                     the 15-min mobilisation buffer from defects.py)
        B_clearance = 15 min  (POWER_AND_TRAFFIC — isolate, earth both ends,
                               then restore)
                    =  5 min  (TRAFFIC_ONLY — protection placed on both sides)

    Returns the ceiling integer minute count.  If the bundle requires no
    machine (MachineType.NONE), t_transit = 0 and the envelope collapses
    to T_bundle + B_clearance.
    """
    t_transit: float = longest_transit_min(bundle)
    w_required: float = 2.0 * t_transit + bundle.required_duration_min + clearance_min(bundle)
    return math.ceil(w_required)


def possession_timeline(
    bundle: MaintenanceBundle,
    start_min: int,
    end_min: int,
) -> dict[str, object]:
    """
    Step order inside a locked possession. Work is not reachable until the
    machines are on site and, for an OHE block, the feeder is isolated and
    earthed at both ends.
    """
    transit: float = longest_transit_min(bundle)
    setup: int = clearance_min(bundle)
    work: float = float(bundle.required_duration_min)
    start: float = float(start_min)
    arrive: float = start + transit
    ready: float = arrive + setup
    work_end: float = ready + work
    return {
        "start_min": start,
        "end_min": float(end_min),
        "transit_min": round(transit, 3),
        "clearance_min": setup,
        "work_min": work,
        "arrive_min": round(arrive, 3),
        "protection_ready_min": round(ready, 3),
        "work_start_min": round(ready, 3),
        "work_end_min": round(work_end, 3),
        "return_start_min": round(work_end, 3),
        "power_block": bundle.block_type is BlockType.POWER_AND_TRAFFIC,
        "machines": [dict(row) for row in machine_transits(bundle)],
        "worksite_start_km": bundle.start_km,
        "worksite_end_km": bundle.end_km,
        "track_line": bundle.track_line,
        "power_sector_id": bundle.power_sector_id,
        "block_type": bundle.block_type.value,
    }


# ---------------------------------------------------------------------------
# Trajectory spatial analysis
# ---------------------------------------------------------------------------


def protection_zone(bundle: MaintenanceBundle) -> tuple[float, float, str]:
    """
    Chainage and running line that must be empty while the possession is open.

    The closed stretch is the worksite plus its safety clearance. On a power
    block that same stretch is the isolated section, earthed at both ends.
    Parallel lines keep their own feeders and stay open.
    """
    block_lo, block_hi = bundle.block_boundaries()
    protected_line = bundle.track_line
    if bundle.block_type is BlockType.POWER_AND_TRAFFIC:
        protected_line = get_power_subsector(bundle.power_sector_id).isolated_line
    return block_lo, block_hi, protected_line


def _trajectory_km_extent(trajectory: TrainTrajectory) -> tuple[float, float]:
    """
    Return the true (lo, hi) chainage extent of a trajectory derived from
    its waypoints when available.

    Waypoint-based extent is critical for round-trip services whose
    origin_km == dest_km but whose physical range differs (e.g. Bhor Ghat
    bankers: KJT→LNL→KJT store dest_km = lnl.km = 128, but the extent
    from the waypoints captures the full [100, 128] range explicitly).
    Falls back to origin_km / dest_km when no waypoints exist.
    """
    if trajectory.waypoints:
        lo = min(wp.km for wp in trajectory.waypoints)
        hi = max(wp.km for wp in trajectory.waypoints)
        return lo, hi
    return (
        min(trajectory.origin_km, trajectory.dest_km),
        max(trajectory.origin_km, trajectory.dest_km),
    )


def _trajectory_uses_line_in_range(
    trajectory: TrainTrajectory,
    track_line: str,
    block_lo: float,
    block_hi: float,
) -> bool:
    """
    Return True iff the trajectory physically runs on *track_line* within
    the chainage zone [block_lo, block_hi].

    Used by Constraint 2 (No Crash Law) to restrict enforcement to trains
    on the SAME running line as the bundle.  A train on UP_SLOW has zero
    physical interaction with a DN_FAST block zone and must be excluded;
    over-constraining with parallel-track services makes feasible problems
    appear infeasible.

    Adjacent-track exclusions for POWER_AND_TRAFFIC bundles are handled
    separately by Constraint 3 which intentionally omits this line filter.

    Algorithm
    ---------
    1. Fast reject via `_trajectory_km_extent` (same guard as the entry/
       exit function to ensure consistency).
    2. Scan waypoints that fall inside the zone; any whose `.line` matches
       is conclusive.
    3. For non-stopping pass-through trains (no waypoints in zone), sample
       `trajectory.line_at_km` at the zone centre km.
    """
    traj_lo, traj_hi = _trajectory_km_extent(trajectory)
    if traj_hi < block_lo - 1e-6 or traj_lo > block_hi + 1e-6:
        return False

    # Waypoints inside the zone give a definitive answer
    for wp in trajectory.waypoints:
        if block_lo - 1e-6 <= wp.km <= block_hi + 1e-6:
            if wp.line == track_line:
                return True

    # For pass-through trains without zone waypoints, resolve at centre km
    mid_km: float = (block_lo + block_hi) / 2.0
    clamped: float = max(traj_lo, min(traj_hi, mid_km))
    if abs(clamped - mid_km) < 1e-3:
        return trajectory.line_at_km(mid_km) == track_line

    return False


def _block_entry_exit_minutes(
    trajectory: TrainTrajectory,
    block_lo: float,
    block_hi: float,
) -> tuple[int, int] | None:
    """
    Return (T_orig_in, T_orig_out) as integer minutes for the interval
    during which *trajectory* physically occupies [block_lo, block_hi].

    Algorithm
    ---------
    1. Fast-reject: trajectory km range does not intersect block zone.
    2. Walk consecutive waypoint segments; for each overlapping segment,
       collect:
         • endpoint minutes if the endpoint km lies inside the zone, and
         • precise interpolated crossing minutes at block_lo / block_hi
           using the parametric formula  t = t_left + ratio · dt.
    3. Clamp raw float results to [0, 1440] and return
       (floor(min), ceil(max)).

    Note on bankers: bankers traverse the ghat on both a push leg (KJT→LNL)
    and a return leg (LNL→KJT). Both legs cross any intermediate block zone.
    The returned window spans first-entry to last-exit conservatively; any
    intra-window gap (banker parked at Lonavala) is included for safety.

    Returns None when the trajectory does not enter the block zone.
    """
    traj_lo: float
    traj_hi: float
    traj_lo, traj_hi = _trajectory_km_extent(trajectory)

    # Fast reject — trajectory km extent does not reach block zone
    if traj_hi < block_lo - 1e-6 or traj_lo > block_hi + 1e-6:
        return None

    waypoints = trajectory.waypoints
    if not waypoints:
        return None

    times_in_zone: list[float] = []

    for idx in range(len(waypoints) - 1):
        left = waypoints[idx]
        right = waypoints[idx + 1]

        seg_lo: float = min(left.km, right.km)
        seg_hi: float = max(left.km, right.km)

        # Skip segments entirely outside the zone
        if seg_hi < block_lo - 1e-6 or seg_lo > block_hi + 1e-6:
            continue

        # Endpoint minutes that already fall inside the zone
        if block_lo - 1e-6 <= left.km <= block_hi + 1e-6:
            times_in_zone.append(left.minute)
        if block_lo - 1e-6 <= right.km <= block_hi + 1e-6:
            times_in_zone.append(right.minute)

        # Interpolate precise km-boundary crossing times
        dkm: float = right.km - left.km
        dt: float = right.minute - left.minute
        if abs(dkm) > 1e-9:
            if seg_lo < block_lo < seg_hi:
                ratio: float = (block_lo - left.km) / dkm
                if 0.0 <= ratio <= 1.0:
                    times_in_zone.append(left.minute + ratio * dt)
            if seg_lo < block_hi < seg_hi:
                ratio = (block_hi - left.km) / dkm
                if 0.0 <= ratio <= 1.0:
                    times_in_zone.append(left.minute + ratio * dt)

    # Check the terminal waypoint (loop runs to len-1)
    last_wp = waypoints[-1]
    if block_lo - 1e-6 <= last_wp.km <= block_hi + 1e-6:
        times_in_zone.append(last_wp.minute)

    if not times_in_zone:
        return None

    t_in: int = max(0, min(1440, math.floor(min(times_in_zone))))
    t_out: int = max(0, min(1440, math.ceil(max(times_in_zone))))

    # Guard: ensure non-degenerate window
    if t_out <= t_in:
        t_out = t_in + 1

    return t_in, t_out


# ---------------------------------------------------------------------------
# CP-SAT corridor scheduling model
# ---------------------------------------------------------------------------


class CorridorScheduleModel:
    """
    Encapsulates the full CP-SAT model for Central Railway maintenance
    block scheduling.  Accepts structured domain objects from defects.py
    and timetable.py and builds the optimisation problem in-memory.

    Decision variables
    ------------------
    start_time[b]  IntVar  ∈ [0, 1440 − envelope]
    end_time[b]    IntVar  = start_time[b] + envelope  (enforced by IntervalVar)
    interval[b]    IntervalVar  linking start / fixed-size / end
    delay_t[t]     IntVar  ∈ [0, MAX_PATIENCE_MIN[train_type]]

    Constraints
    -----------
    1. Machinery Pooling  — AddCumulative per machine type with fleet
                            capacity from DIVISION_MACHINERY_LIMITS.
    2. No Crash Law       — BoolVar `train_before` disjunction per
                            (bundle, train) pair that shares a block zone.
    3. Power Sector Iso.  — Same disjunction extended to trains on adjacent
                            tracks within the OHE feeder sub-sector.

    Objective
    ---------
    Minimise  Σ_t(W_type · delay_t)  +  Σ_b(W_postponement · start_time_b)
    """

    def __init__(self) -> None:
        self._model: cp_model.CpModel = cp_model.CpModel()
        self._bundle_start: dict[str, cp_model.IntVar] = {}
        self._bundle_end: dict[str, cp_model.IntVar] = {}
        self._bundle_interval: dict[str, cp_model.IntervalVar] = {}
        self._bundle_envelope: dict[str, int] = {}
        self._bundle_urgency: dict[str, float] = {}
        self._train_delay: dict[str, cp_model.IntVar] = {}
        self._train_type_weight: dict[str, int] = {}
        self._earliest_start: int = 0
        self._latest_end: int | None = None
        self._passenger_weight: int = W_PASSENGER_DELAY
        self._freight_weight: int = W_FREIGHT_DELAY
        self._postponement_weight: int = W_POSTPONEMENT
        # Track (bundle_id, train_id) pairs that already have a no-crash
        # constraint; prevents duplicate BoolVar creation.
        self._constrained_pairs: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------
    # Public build interface
    # ------------------------------------------------------------------

    def build(
        self,
        bundles: Sequence[MaintenanceBundle],
        trains: Sequence[TrainTrajectory],
        earliest_start_min: int = 0,
        latest_end_min: int | None = None,
        passenger_weight: int | None = None,
        freight_weight: int | None = None,
        postponement_weight: int | None = None,
    ) -> None:
        """
        Construct all decision variables, constraints, and the objective
        function.  Call once before handing the model to CpSolver.
        """
        self._earliest_start = max(0, int(earliest_start_min))
        self._latest_end = None if latest_end_min is None else int(latest_end_min)
        if passenger_weight is not None:
            self._passenger_weight = max(1, int(passenger_weight))
        if freight_weight is not None:
            self._freight_weight = max(1, int(freight_weight))
        if postponement_weight is not None:
            self._postponement_weight = max(1, int(postponement_weight))
        self._add_bundle_variables(bundles)
        self._add_train_delay_variables(trains)
        self._add_machinery_pooling_constraints(bundles)
        self._add_train_block_safety_constraints(bundles, trains)
        self._add_power_sector_isolation_constraints(bundles, trains)
        self._build_objective(trains)

    def get_model(self) -> cp_model.CpModel:
        """Return the fully-built CpModel for external solve calls."""
        return self._model

    # ------------------------------------------------------------------
    # Variable construction
    # ------------------------------------------------------------------

    def _add_bundle_variables(self, bundles: Sequence[MaintenanceBundle]) -> None:
        for bundle in bundles:
            envelope: int = calculate_transit_envelope(bundle)
            self._bundle_envelope[bundle.bundle_id] = envelope
            if self._latest_end is not None:
                max_start: int = max(0, min(1440 - envelope, self._latest_end - envelope))
            else:
                max_start = max(0, 1440 - envelope)
            lower: int = min(self._earliest_start, max_start)

            start: cp_model.IntVar = self._model.NewIntVar(
                lower, max_start, f"start_{bundle.bundle_id}"
            )
            end: cp_model.IntVar = self._model.NewIntVar(
                envelope, 1440, f"end_{bundle.bundle_id}"
            )
            # NewIntervalVar enforces: start + envelope == end
            interval: cp_model.IntervalVar = self._model.NewIntervalVar(
                start, envelope, end, f"interval_{bundle.bundle_id}"
            )

            self._bundle_start[bundle.bundle_id] = start
            self._bundle_end[bundle.bundle_id] = end
            self._bundle_interval[bundle.bundle_id] = interval
            self._bundle_urgency[bundle.bundle_id] = bundle.urgency_score

    def _add_train_delay_variables(
        self, trains: Sequence[TrainTrajectory]
    ) -> None:
        for trajectory in trains:
            patience: int = MAX_PATIENCE_MIN.get(trajectory.train_type, 15)
            delay: cp_model.IntVar = self._model.NewIntVar(
                0, patience, f"delay_{trajectory.train_id}"
            )
            self._train_delay[trajectory.train_id] = delay
            if trajectory.train_type in (TYPE_PREMIUM, TYPE_SUPERFAST, TYPE_EXPRESS, TYPE_LOCAL):
                weight = self._passenger_weight
            else:
                weight = self._freight_weight
            self._train_type_weight[trajectory.train_id] = weight

    # ------------------------------------------------------------------
    # Constraint 1 — Machinery Pooling (AddCumulative)
    # ------------------------------------------------------------------

    def _add_machinery_pooling_constraints(
        self, bundles: Sequence[MaintenanceBundle]
    ) -> None:
        """
        Group bundle IntervalVars by required machine type and enforce a
        cumulative capacity constraint equal to the division fleet limit.

        A single BCM machine (capacity 1) uses AddCumulative with cap=1,
        which is equivalent to AddNoOverlap.  Tie Tamper and Tower Wagon
        (capacity 2 each) allow two simultaneous deployments.
        """
        machine_intervals: dict[MachineType, list[cp_model.IntervalVar]] = {}
        for bundle in bundles:
            machine: MachineType = bundle.required_machine
            if machine is MachineType.NONE:
                continue
            machine_intervals.setdefault(machine, []).append(
                self._bundle_interval[bundle.bundle_id]
            )

        for machine, intervals in machine_intervals.items():
            fleet_capacity: int = DIVISION_MACHINERY_LIMITS.get(machine, 1)
            demands: list[int] = [1] * len(intervals)
            self._model.AddCumulative(intervals, demands, fleet_capacity)

    # ------------------------------------------------------------------
    # Constraint 2 — No Crash Law / Rule A (train vs block zone)
    # ------------------------------------------------------------------

    def _add_train_block_safety_constraints(
        self,
        bundles: Sequence[MaintenanceBundle],
        trains: Sequence[TrainTrajectory],
    ) -> None:
        """
        For every (bundle, train) pair where the trajectory spatially
        overlaps the bundle's block zone, compute T_orig_in / T_orig_out
        and enforce the binary safe-passage disjunction:

            train_before = 1  ⟹  T_orig_out + delay_t ≤ start_time
            train_before = 0  ⟹  T_orig_in  + delay_t ≥ end_time

        The solver selects the cheapest binary assignment.  Premium and
        Superfast trains have delay_t ∈ [0, 0] (Rule B), so the block
        must flex around their fixed timetable rather than the reverse.
        """
        for bundle in bundles:
            b_start: cp_model.IntVar = self._bundle_start[bundle.bundle_id]
            b_end: cp_model.IntVar = self._bundle_end[bundle.bundle_id]
            block_lo, block_hi, protected_line = protection_zone(bundle)

            for trajectory in trains:
                # Rule A applies only to trains on the protected running line.
                # A train on UP has no physical conflict with a MID possession.
                if not _trajectory_uses_line_in_range(
                    trajectory, protected_line, block_lo, block_hi
                ):
                    continue

                entry_exit = _block_entry_exit_minutes(
                    trajectory, block_lo, block_hi
                )
                if entry_exit is None:
                    continue

                t_in: int
                t_out: int
                t_in, t_out = entry_exit
                delay_var: cp_model.IntVar = self._train_delay[trajectory.train_id]

                self._enforce_no_crash(
                    bundle.bundle_id,
                    trajectory.train_id,
                    b_start,
                    b_end,
                    delay_var,
                    t_in,
                    t_out,
                )
                self._constrained_pairs.add((bundle.bundle_id, trajectory.train_id))

    # ------------------------------------------------------------------
    # Constraint 3 — Power Sector Isolation (adjacent-track trains)
    # ------------------------------------------------------------------

    def _add_power_sector_isolation_constraints(
        self,
        bundles: Sequence[MaintenanceBundle],
        trains: Sequence[TrainTrajectory],
    ) -> None:
        """
        Power isolation uses the same worksite and the same isolated line as
        Rule A. Trains already constrained are skipped. Parallel roads are
        not added here.
        """
        for bundle in bundles:
            if bundle.block_type is not BlockType.POWER_AND_TRAFFIC:
                continue

            sector_lo, sector_hi, isolated_line = protection_zone(bundle)

            b_start: cp_model.IntVar = self._bundle_start[bundle.bundle_id]
            b_end: cp_model.IntVar = self._bundle_end[bundle.bundle_id]

            for trajectory in trains:
                pair: tuple[str, str] = (bundle.bundle_id, trajectory.train_id)
                if pair in self._constrained_pairs:
                    continue
                if not _trajectory_uses_line_in_range(
                    trajectory, isolated_line, sector_lo, sector_hi
                ):
                    continue

                entry_exit = _block_entry_exit_minutes(
                    trajectory, sector_lo, sector_hi
                )
                if entry_exit is None:
                    continue

                t_in: int
                t_out: int
                t_in, t_out = entry_exit
                delay_var: cp_model.IntVar = self._train_delay[trajectory.train_id]

                self._enforce_no_crash(
                    bundle.bundle_id,
                    trajectory.train_id,
                    b_start,
                    b_end,
                    delay_var,
                    t_in,
                    t_out,
                )
                self._constrained_pairs.add(pair)

    # ------------------------------------------------------------------
    # Shared disjunction enforcement
    # ------------------------------------------------------------------

    def _enforce_no_crash(
        self,
        bundle_id: str,
        train_id: str,
        b_start: cp_model.IntVar,
        b_end: cp_model.IntVar,
        delay_var: cp_model.IntVar,
        t_orig_in: int,
        t_orig_out: int,
    ) -> None:
        """
        Create BoolVar `train_before` and encode the safe-passage
        disjunction using conditional linear constraints:

            train_before      ⟹  actual_exit  ≤ block_start
            ¬train_before     ⟹  actual_entry ≥ block_end

        where
            actual_exit  = t_orig_out + delay_var   (integer arithmetic)
            actual_entry = t_orig_in  + delay_var

        OnlyEnforceIf activates each implication only when the BoolVar
        takes its designated value.  The solver selects whichever branch
        minimises the objective.
        """
        var_name: str = f"before__{bundle_id}__{train_id}"
        train_before = self._model.NewBoolVar(var_name)

        # Branch 1: train clears the zone before the block opens
        self._model.Add(
            delay_var + t_orig_out <= b_start
        ).OnlyEnforceIf(train_before)

        # Branch 2: train enters the zone only after the block closes
        self._model.Add(
            delay_var + t_orig_in >= b_end
        ).OnlyEnforceIf(train_before.Not())

    # ------------------------------------------------------------------
    # Chaos Score objective
    # ------------------------------------------------------------------

    def _build_objective(self, trains: Sequence[TrainTrajectory]) -> None:
        """
        Minimise Z = Σ_b(urgency_weight_b · start_b) + Σ_t(W_type · delay_t).

        The postponement penalty for each bundle is scaled by its Formula-1
        urgency score so the solver pulls high-urgency bundles to the earliest
        feasible slot while low-urgency bundles can slip later.  The floor of
        max(1, round(...)) ensures CP-SAT always receives a positive integer
        coefficient — a zero coefficient would make the solver indifferent to
        when that bundle starts, defeating the purpose entirely.

        The train-delay loop is unchanged: passenger services keep their heavy
        per-minute weight independent of bundle urgency.
        """
        all_vars: list[cp_model.IntVar] = []
        all_weights: list[int] = []

        # urgency-weighted postponement — bundles with a higher Formula-1 score
        # carry a proportionally heavier penalty for deferring their start slot.
        for bundle_id, start_var in self._bundle_start.items():
            urgency = self._bundle_urgency.get(bundle_id, 0.0)
            weight = max(1, round(self._postponement_weight * urgency))
            all_vars.append(start_var)
            all_weights.append(weight)

        # W_type · delay_t  — heavier weight for passenger services
        for train_id, delay_var in self._train_delay.items():
            all_vars.append(delay_var)
            all_weights.append(
                self._train_type_weight.get(train_id, W_PASSENGER_DELAY)
            )

        if all_vars:
            self._model.Minimize(
                cp_model.LinearExpr.WeightedSum(all_vars, all_weights)
            )

    # ------------------------------------------------------------------
    # Solution extraction
    # ------------------------------------------------------------------

    def extract_solution(
        self,
        solver: cp_model.CpSolver,
        bundles: Sequence[MaintenanceBundle],
        trains: Sequence[TrainTrajectory],
    ) -> tuple[tuple[ScheduledBlock, ...], tuple[DelayedTrain, ...], float]:
        """
        Read solved variable values and map them to frozen domain objects.

        Only trains with delay_t > 0 appear in delayed_trains; trains whose
        timetable was unaffected by any block are omitted for a lean payload.

        Returns (blocks, delayed_trains, chaos_score).
        """
        blocks: list[ScheduledBlock] = []
        for bundle in bundles:
            start_val: int = solver.Value(self._bundle_start[bundle.bundle_id])
            end_val: int = solver.Value(self._bundle_end[bundle.bundle_id])
            blocks.append(
                ScheduledBlock(
                    bundle_id=bundle.bundle_id,
                    start_min=start_val,
                    end_min=end_val,
                    machine_type=bundle.required_machine.value,
                    power_sector_id=bundle.power_sector_id,
                )
            )

        delayed_trains: list[DelayedTrain] = []
        for trajectory in trains:
            delay_val: int = solver.Value(self._train_delay[trajectory.train_id])
            if delay_val > 0:
                orig_min: int = int(math.floor(trajectory.window.start_min))
                delayed_trains.append(
                    DelayedTrain(
                        train_id=trajectory.train_id,
                        original_min=orig_min,
                        new_min=orig_min + delay_val,
                        delay_min=delay_val,
                    )
                )

        chaos_score: float = float(solver.ObjectiveValue())
        return tuple(blocks), tuple(delayed_trains), chaos_score


# ---------------------------------------------------------------------------
# Solver status map (module-level constant avoids repeated dict literals)
# ---------------------------------------------------------------------------

_STATUS_MAP: Final[dict[int, str]] = {
    cp_model.OPTIMAL:       "OPTIMAL",
    cp_model.FEASIBLE:      "FEASIBLE",
    cp_model.INFEASIBLE:    "INFEASIBLE",
    cp_model.UNKNOWN:       "UNKNOWN",
    cp_model.MODEL_INVALID: "MODEL_INVALID",
}

# ---------------------------------------------------------------------------
# Public execution interface
# ---------------------------------------------------------------------------


def solve_corridor_schedule(
    bundles: Sequence[MaintenanceBundle],
    trains: Sequence[TrainTrajectory],
    time_limit_sec: float = 15.0,
    earliest_start_min: int = 0,
    latest_end_min: int | None = None,
    passenger_weight: int | None = None,
    freight_weight: int | None = None,
    postponement_weight: int | None = None,
) -> SolverResult:
    """
    Build and solve the Central Railway corridor maintenance scheduling
    problem with Google OR-Tools CP-SAT.

    Parameters
    ----------
    bundles
        Spatial maintenance clusters produced by
        ``defects.cluster_defects_into_bundles()``.
    trains
        Full 24-hour WTT trajectory set from
        ``timetable.build_timetable()`` or a filtered subset.
    time_limit_sec
        Wall-clock budget passed to ``solver.parameters.max_time_in_seconds``.
        The solver returns the best feasible solution found within this limit.

    Returns
    -------
    SolverResult
        Frozen dataclass with status, solve_time_sec, scheduled blocks,
        delayed trains (delay > 0 only), the minimised chaos score, and the
        solver's best objective bound.

    Fallback behaviour
    ------------------
    On INFEASIBLE or UNKNOWN, blocks are assigned slot [0, envelope] (the
    earliest theoretical window) and both chaos_score and best_bound are set
    to ``float('inf')`` so callers can distinguish a solved from an unsolved
    instance without inspecting the status string.
    """
    earliest: int = max(0, int(earliest_start_min))
    latest_end: int | None = None if latest_end_min is None else int(latest_end_min)
    for bundle in bundles:
        envelope: int = calculate_transit_envelope(bundle)
        room: int = max(0, 1440 - envelope)
        if earliest > room or (
            latest_end is not None and earliest + envelope > latest_end
        ):
            return SolverResult(
                status="INFEASIBLE",
                solve_time_sec=0.0,
                blocks=(),
                delayed_trains=(),
                chaos_score=float("inf"),
                best_bound=float("inf"),
            )

    schedule_model = CorridorScheduleModel()
    schedule_model.build(
        bundles,
        trains,
        earliest_start_min=earliest,
        latest_end_min=latest_end,
        passenger_weight=passenger_weight,
        freight_weight=freight_weight,
        postponement_weight=postponement_weight,
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_sec)
    solver.parameters.num_search_workers = 4
    solver.parameters.log_search_progress = False

    wall_t0: float = time.monotonic()
    raw_status: int = solver.Solve(schedule_model.get_model())
    elapsed: float = time.monotonic() - wall_t0
    best_bound: float = float(solver.BestObjectiveBound())

    status_str: str = _STATUS_MAP.get(raw_status, "UNKNOWN")

    if raw_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        blocks, delayed_trains, chaos = schedule_model.extract_solution(
            solver, bundles, trains
        )
    else:
        # Fallback: earliest possible slot, no delays extracted.
        # best_bound matches chaos_score: no proven objective exists.
        blocks = tuple(
            ScheduledBlock(
                bundle_id=bundle.bundle_id,
                start_min=0,
                end_min=calculate_transit_envelope(bundle),
                machine_type=bundle.required_machine.value,
                power_sector_id=bundle.power_sector_id,
            )
            for bundle in bundles
        )
        delayed_trains = ()
        chaos = float("inf")
        best_bound = float("inf")

    return SolverResult(
        status=status_str,
        solve_time_sec=round(elapsed, 3),
        blocks=blocks,
        delayed_trains=delayed_trains,
        chaos_score=chaos,
        best_bound=best_bound,
    )


__all__ = [
    "CorridorScheduleModel",
    "DelayedTrain",
    "MAX_PATIENCE_MIN",
    "ScheduledBlock",
    "SolverResult",
    "W_FREIGHT_DELAY",
    "W_PASSENGER_DELAY",
    "W_POSTPONEMENT",
    "calculate_transit_envelope",
    "clearance_min",
    "longest_transit_min",
    "machine_transits",
    "possession_timeline",
    "protection_zone",
    "solve_corridor_schedule",
    "_trajectory_km_extent",
    "_trajectory_uses_line_in_range",
]
