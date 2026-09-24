"""
verify_numbers.py
====================================================================
One-off verification script (NOT part of the SIH-26027 DSS application).
Recomputes every number that the PPT rebuild script quotes, straight
from the real domain modules (defects.py, timetable.py, ml_priority.py,
solver.py) so the slide content can cite figures that are provably
correct rather than hand-calculated.
====================================================================
"""

from __future__ import annotations

import io
import sys
import time
from contextlib import redirect_stdout

sys.path.insert(0, r"C:\Users\JITESH\Downloads\PythonProject5")

import defects
import timetable
import solver
import ml_priority


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def main() -> None:
    section("1. BUNDLES — Formula 2 spatial bundling (from real DEFAULT_DEFECTS)")
    bundles = defects.get_active_maintenance_bundles()
    total_standalone = 0
    total_bundled = 0
    multi_defect_standalone = 0
    multi_defect_bundled = 0
    multi_count = 0
    for b in bundles:
        standalone = sum(d.required_block_min for d in b.defects)
        bundled = b.required_duration_min
        gain = (1 - bundled / standalone) * 100 if standalone else 0.0
        total_standalone += standalone
        total_bundled += bundled
        tag = "MULTI" if len(b.defects) > 1 else "solo"
        if len(b.defects) > 1:
            multi_defect_standalone += standalone
            multi_defect_bundled += bundled
            multi_count += 1
        print(
            f"  {b.bundle_id:26s} defects={len(b.defects)} [{tag:5s}] "
            f"standalone={standalone:6.1f} bundled={bundled:6.1f} "
            f"gain={gain:6.2f}%  machine={b.required_machine.value:12s} "
            f"urgency={b.urgency_score:6.2f} mandatory={b.is_mandatory}"
        )
    print(f"\n  TOTAL defects: {sum(len(b.defects) for b in bundles)}  TOTAL bundles: {len(bundles)}")
    print(f"  TOTAL standalone min: {total_standalone}  TOTAL bundled min: {total_bundled}  "
          f"NET gain%: {(1 - total_bundled / total_standalone) * 100:.2f}%")
    print(f"  MULTI-defect clusters only ({multi_count} clusters): "
          f"standalone={multi_defect_standalone} bundled={multi_defect_bundled} "
          f"saved={multi_defect_standalone - multi_defect_bundled} min "
          f"gain={(1 - multi_defect_bundled / multi_defect_standalone) * 100:.2f}%")

    section("2. TIMETABLE — real WTT trajectory counts")
    all_traj = timetable.get_all_trajectories()
    by_type: dict[str, int] = {}
    for t in all_traj:
        by_type[t.train_type] = by_type.get(t.train_type, 0) + 1
    print(f"  TOTAL trajectories (24h WTT): {len(all_traj)}")
    for k, v in sorted(by_type.items()):
        print(f"    {k:10s}: {v}")

    section("3. ML PRIORITY — GradientBoostingRegressor, freshly trained")
    buf = io.StringIO()
    t0 = time.monotonic()
    with redirect_stdout(buf):
        metrics_out = ml_priority.train_and_save(model_path=ml_priority.MODEL_PATH.with_name("ml_verify_tmp.joblib"))
    elapsed = time.monotonic() - t0
    print(buf.getvalue())
    print(f"  Training wall time: {elapsed:.2f}s")
    print(f"  Returned dict: {metrics_out}")

    section("4. SOLVER — one real CP-SAT solve on the full default backlog")
    trains = timetable.get_all_trajectories()
    t0 = time.monotonic()
    result = solver.solve_corridor_schedule(list(bundles), trains, time_limit_sec=15.0)
    elapsed = time.monotonic() - t0
    print(f"  status={result.status}  solve_time_sec(reported)={result.solve_time_sec}  "
          f"wall={elapsed:.2f}s")
    print(f"  blocks scheduled: {len(result.blocks)}  chaos_score={result.chaos_score}  "
          f"best_bound={result.best_bound}")
    print(f"  delayed trains: {len(result.delayed_trains)}")
    passenger_types = {"PREMIUM", "SUPERFAST", "EXPRESS", "LOCAL"}
    lookup = {t.train_id: t.train_type for t in trains}
    pax_delayed = sum(1 for d in result.delayed_trains if lookup.get(d.train_id) in passenger_types)
    frt_delayed = sum(1 for d in result.delayed_trains if lookup.get(d.train_id) not in passenger_types)
    print(f"  passenger delayed: {pax_delayed}  freight/banker delayed: {frt_delayed}")

    section("5. TOPOLOGY / CONSTANTS — quick cross-checks")
    print(f"  Corridor length: {topology_end()} km")
    print(f"  Sections: {len(defects_import_topology_sections())}")
    print(f"  Default spatial clustering gap: {defects.DEFAULT_SPATIAL_GAP_KM} km")
    print(f"  Mobilisation buffer: {defects.MOBILIZATION_BUFFER_MIN} min")
    print(f"  Test function count check happens via grep, not here.")


def topology_end() -> float:
    import topology
    return topology.CORRIDOR_END_KM


def defects_import_topology_sections() -> tuple:
    import topology
    return topology.SECTION_BOUNDS


if __name__ == "__main__":
    main()
