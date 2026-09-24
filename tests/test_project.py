"""Regression checks for clustering, urgency, horizon windows, and source mapping."""

from __future__ import annotations

from datetime import date

import app
import calendar_plan
import defects
import metrics
from defects import DefectType, MaintenanceBundle, TrackDefect

_START = date(2026, 9, 21)


def _defects_by_id() -> dict[str, TrackDefect]:
    return {item.defect_id: item for item in defects.get_default_defects()}


def _bundles() -> tuple[MaintenanceBundle, ...]:
    return defects.cluster_defects_into_bundles(defects.get_default_defects())


def _bundle_containing(
    bundles: tuple[MaintenanceBundle, ...],
    defect_id: str,
) -> MaintenanceBundle:
    matches = [
        bundle
        for bundle in bundles
        if any(item.defect_id == defect_id for item in bundle.defects)
    ]
    assert len(matches) == 1, defect_id
    return matches[0]


def test_bundle_clustering_groups_nearby_same_line_defects() -> None:
    catalog = _defects_by_id()
    near_a = catalog["DEF_S2_01"]
    near_b = catalog["DEF_S2_03"]
    assert near_a.track_line == near_b.track_line
    assert near_a.power_sector_id == near_b.power_sector_id
    assert abs(near_a.km_location - near_b.km_location) <= defects.DEFAULT_SPATIAL_GAP_KM

    bundles = _bundles()
    assert _bundle_containing(bundles, "DEF_S2_01").bundle_id == _bundle_containing(
        bundles, "DEF_S2_03"
    ).bundle_id


def test_bundle_clustering_splits_far_defects() -> None:
    # DEF_S1_01 and DEF_S1_04 are only 1.6 km apart on DN_FAST, so they cluster.
    # These two DN defects sit 18.6 km apart, past DEFAULT_SPATIAL_GAP_KM.
    catalog = _defects_by_id()
    far_a = catalog["DEF_S4_01"]
    far_b = catalog["DEF_S4_03"]
    assert far_a.track_line == far_b.track_line
    assert abs(far_a.km_location - far_b.km_location) > defects.DEFAULT_SPATIAL_GAP_KM

    bundles = _bundles()
    assert _bundle_containing(bundles, "DEF_S4_01").bundle_id != _bundle_containing(
        bundles, "DEF_S4_03"
    ).bundle_id


def test_urgency_score_ordering() -> None:
    catalog = _defects_by_id()
    milder = catalog["DEF_S2_03"]
    sharper = catalog["DEF_S2_01"]
    assert sharper.severity > milder.severity

    short_wait = defects.compute_priority_urgency_score(
        milder, days_overdue=5.0, has_tsr=False, traffic_density=1
    )
    long_wait = defects.compute_priority_urgency_score(
        milder, days_overdue=20.0, has_tsr=False, traffic_density=1
    )
    assert long_wait > short_wait

    without_tsr = defects.compute_priority_urgency_score(
        milder, days_overdue=10.0, has_tsr=False, traffic_density=1
    )
    with_tsr = defects.compute_priority_urgency_score(
        milder, days_overdue=10.0, has_tsr=True, traffic_density=1
    )
    assert with_tsr > without_tsr

    low_severity = defects.compute_priority_urgency_score(
        milder, days_overdue=0.0, has_tsr=False, traffic_density=1
    )
    high_severity = defects.compute_priority_urgency_score(
        sharper, days_overdue=0.0, has_tsr=False, traffic_density=1
    )
    assert high_severity > low_severity


def test_horizon_plan_blocks_stay_within_window() -> None:
    plan = calendar_plan.build_horizon_plan(_bundles(), _START, 10)
    checked = 0
    for day in plan.days:
        if day.result is None or day.result.status not in ("OPTIMAL", "FEASIBLE"):
            continue
        for block in day.result.blocks:
            assert day.window.start_min <= block.start_min <= day.window.end_min
            assert day.window.start_min <= block.end_min <= day.window.end_min
            checked += 1
    assert checked > 0


def test_mandatory_defect_gets_scheduled() -> None:
    bundles = _bundles()
    mandatory_ids = {bundle.bundle_id for bundle in bundles if bundle.is_mandatory}
    assert mandatory_ids
    plan = calendar_plan.build_horizon_plan(bundles, _START, 14)
    leftover = mandatory_ids & set(plan.unscheduled_bundle_ids)
    assert leftover == set()


def test_console_bundle_uses_cluster_facts() -> None:
    bundle = _bundle_containing(_bundles(), "DEF_S2_01")
    brief = app._console_bundle(bundle)
    adapted = app.adapt_bundle(bundle)
    assert adapted == brief
    assert brief["bundle_id"] == bundle.bundle_id
    assert brief["track_line"] == bundle.track_line
    assert brief["sequential_duration_sum"] == sum(int(item.required_block_min) for item in bundle.defects)
    assert brief["work_duration_min"] == int(bundle.required_duration_min)
    assert brief["clearance_min"] in (5, 15)
    assert brief["sources"]
    assert brief["crew_headcount"] == sum(app._crew_headcount(item) for item in bundle.defects)
    assert {task["task_id"] for task in brief["tasks"]} == {item.defect_id for item in bundle.defects}
    for machine in brief["machines"]:
        assert machine["machine_type"] in {"TIE_TAMPER", "BCM_MACHINE", "TOWER_WAGON"}
        assert isinstance(machine["depot_km"], float)
    rendered = str(brief)
    assert "CSM-01" not in rendered
    assert "TW-04" not in rendered
    assert "BDMS" not in rendered


def test_spatial_bundling_proof_counts_clustered_jobs() -> None:
    standalone, bundled, gain, clusters, defect_count = metrics.calculate_spatial_bundling_proof(
        defects.cluster_defects_into_bundles(defects.get_default_defects())
    )
    assert clusters == 3
    assert defect_count == 6
    assert standalone == 450
    assert bundled == 330
    assert standalone - bundled == 120
    assert gain == 26.67


def test_source_system_mapping() -> None:
    expected = {
        DefectType.RAIL_FRACTURE: defects.SOURCE_TMS,
        DefectType.WELD_FAILURE: defects.SOURCE_TMS,
        DefectType.TAMPING_REQUIRED: defects.SOURCE_TMS,
        DefectType.DEEP_SCREENING: defects.SOURCE_TMS,
        DefectType.SIGNAL_INTERLOCK: defects.SOURCE_SMMS,
        DefectType.OHE_SAG: defects.SOURCE_TDMS,
    }
    assert set(expected) == set(DefectType)
    for defect_type in DefectType:
        assert app._SOURCE_BY_DEFECT_TYPE[defect_type] == expected[defect_type]
