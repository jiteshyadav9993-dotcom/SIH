"""
Temporary diagnosis: why BUNDLE_S2_UP_01 is INFEASIBLE when solved alone.

Does not change solver.py. Power-sector isolation is skipped by a subclass
(the same test as commenting out _add_power_sector_isolation_constraints)
because the production model does not register CP-SAT assumption literals.
"""

from __future__ import annotations

import math

from ortools.sat.python import cp_model

import defects
import timetable
from solver import (
    CorridorScheduleModel,
    _block_entry_exit_minutes,
    _trajectory_uses_line_in_range,
    calculate_transit_envelope,
    protection_zone,
)

STATUS_NAME = {
    cp_model.OPTIMAL: "OPTIMAL",
    cp_model.FEASIBLE: "FEASIBLE",
    cp_model.INFEASIBLE: "INFEASIBLE",
    cp_model.UNKNOWN: "UNKNOWN",
    cp_model.MODEL_INVALID: "MODEL_INVALID",
}


class AssumedCorridorModel(CorridorScheduleModel):
    """Same model, but every no-crash disjunction is gated by an assumption."""

    def __init__(self) -> None:
        super().__init__()
        self.assumption_by_index: dict[int, str] = {}

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
        label = f"{bundle_id} :: {train_id}  t_in={t_orig_in} t_out={t_orig_out}"
        assumption = self._model.NewBoolVar(f"assume__{bundle_id}__{train_id}")
        self._model.AddAssumption(assumption)
        self.assumption_by_index[assumption.Index()] = label

        train_before = self._model.NewBoolVar(f"before__{bundle_id}__{train_id}")
        self._model.Add(delay_var + t_orig_out <= b_start).OnlyEnforceIf(
            [train_before, assumption]
        )
        self._model.Add(delay_var + t_orig_in >= b_end).OnlyEnforceIf(
            [train_before.Not(), assumption]
        )


class NoPowerIsolationModel(CorridorScheduleModel):
    """Identical build, with Constraint 3 commented out."""

    def _add_power_sector_isolation_constraints(self, bundles, trains) -> None:
        return None


def _solve(model: CorridorScheduleModel, seconds: float = 10.0) -> tuple[cp_model.CpSolver, int]:
    cp_solver = cp_model.CpSolver()
    cp_solver.parameters.max_time_in_seconds = seconds
    cp_solver.parameters.log_search_progress = False
    status = cp_solver.Solve(model.get_model())
    return cp_solver, status


def _print_status(label: str, status: int) -> None:
    print(f"{label}: {status} ({STATUS_NAME.get(status, 'UNKNOWN')})")


def _conflicting_trains(bundle, trains) -> list[dict[str, object]]:
    """Trains Rule A (and, separately, power isolation) would fence off."""
    envelope = calculate_transit_envelope(bundle)
    block_lo, block_hi, protected_line = protection_zone(bundle)
    rows: list[dict[str, object]] = []
    seen: set[str] = set()

    def consider(trajectory, source: str) -> None:
        if trajectory.train_id in seen and source == "power_isolation":
            return
        if not _trajectory_uses_line_in_range(trajectory, protected_line, block_lo, block_hi):
            return
        entry_exit = _block_entry_exit_minutes(trajectory, block_lo, block_hi)
        if entry_exit is None:
            return
        t_in, t_out = entry_exit
        from solver import MAX_PATIENCE_MIN

        patience = MAX_PATIENCE_MIN.get(trajectory.train_type, 15)
        # Feasible starts: S <= t_in + patience - envelope  OR  S >= t_out
        branch2_max = t_in + patience - envelope
        rows.append(
            {
                "source": source if trajectory.train_id not in seen else "already_in_rule_a",
                "train_id": trajectory.train_id,
                "train_type": trajectory.train_type,
                "t_in": t_in,
                "t_out": t_out,
                "patience": patience,
                "branch2_max_start": branch2_max,
                "forbidden_open": (branch2_max, t_out) if branch2_max < t_out else None,
            }
        )
        seen.add(trajectory.train_id)

    for trajectory in trains:
        consider(trajectory, "rule_a")
    return rows


def main() -> None:
    all_bundles = defects.cluster_defects_into_bundles(defects.get_default_defects())
    target = next(b for b in all_bundles if b.bundle_id == "BUNDLE_S2_UP_01")
    trains = timetable.get_all_trajectories()

    envelope = calculate_transit_envelope(target)
    zone = protection_zone(target)
    print(f"Bundle: {target.bundle_id}")
    print(f"Block type: {target.block_type}")
    print(f"Track line: {target.track_line}")
    print(f"Power sector: {target.power_sector_id}")
    print(f"Required machine: {target.required_machine}")
    print(f"Envelope min: {envelope}")
    print(f"Protection zone km/line: {zone}")
    print(f"Defects: {[d.defect_id for d in target.defects]}")
    print(f"Trains in model: {len(trains)}")
    print()

    print("--- full CorridorScheduleModel (production constraints) ---")
    full = CorridorScheduleModel()
    full.build([target], trains, earliest_start_min=0)
    full_solver, full_status = _solve(full)
    _print_status("Status", full_status)
    print(f"Constrained pairs: {len(full._constrained_pairs)}")
    for pair in sorted(full._constrained_pairs):
        print(f"  {pair[0]} x {pair[1]}")
    print()

    print("--- sufficient-assumptions diagnosis ---")
    assumed = AssumedCorridorModel()
    assumed.build([target], trains, earliest_start_min=0)
    assumed_solver, assumed_status = _solve(assumed)
    _print_status("Assumed-model status", assumed_status)
    if assumed_status == cp_model.INFEASIBLE:
        indices = list(assumed_solver.SufficientAssumptionsForInfeasibility())
        print(f"Sufficient assumptions ({len(indices)}):")
        if not indices:
            print("  (empty — model had no conflicting assumption literals)")
        for index in indices:
            print(f"  {assumed.assumption_by_index.get(index, f'literal {index}')}")
    print()

    print("--- Constraint 3 removed (_add_power_sector_isolation_constraints) ---")
    stripped = NoPowerIsolationModel()
    stripped.build([target], trains, earliest_start_min=0)
    _stripped_solver, stripped_status = _solve(stripped)
    _print_status("Status without power isolation", stripped_status)
    print(f"Constrained pairs without power isolation: {len(stripped._constrained_pairs)}")
    only_power = full._constrained_pairs - stripped._constrained_pairs
    print(f"Pairs added only by power isolation: {len(only_power)}")
    for pair in sorted(only_power):
        print(f"  {pair[0]} x {pair[1]}")
    print()

    print("--- start windows forbidden by each constrained train ---")
    max_start = max(0, 1440 - envelope)
    print(f"Legal start domain before trains: [0, {max_start}]")
    rows = _conflicting_trains(target, trains)
    covered = [True] * (max_start + 1)
    for row in rows:
        forbid = row["forbidden_open"]
        print(
            f"  {row['train_id']:16} {row['train_type']:10} "
            f"in={row['t_in']:4} out={row['t_out']:4} patience={row['patience']:3} "
            f"must S<={row['branch2_max_start']} or S>={row['t_out']}"
        )
        if forbid is None:
            continue
        lo = math.floor(forbid[0]) + 1
        hi = math.ceil(forbid[1]) - 1
        for minute in range(max(0, lo), min(max_start, hi) + 1):
            covered[minute] = False
    free = [minute for minute, ok in enumerate(covered) if ok]
    print(f"Starts still legal after every Rule-A train: {len(free)}")
    if free:
        print(f"  first legal start: {free[0]}  last: {free[-1]}")
    else:
        print("  none: the train disjunctions alone cover the whole start domain")


if __name__ == "__main__":
    main()
