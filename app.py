from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime

import uvicorn
from fastapi import FastAPI, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

# Real SIH-26027 backend modules
import topology
import timetable
import defects
import ml_priority
import solver
import metrics
import calendar_plan
import data_ingestion
import persistence


@asynccontextmanager
async def _lifespan(_application: FastAPI) -> AsyncIterator[None]:
    persistence.init_db()
    persistence.restore_open_possession_on_startup()
    yield


app = FastAPI(title="CRIS Block Planning DSS (SIH-26027)", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Console actions are open: there is no login. Audit rows use these role names.
_CONSOLE_OFFICER = "MAINTENANCE_OFFICER"
_CONSOLE_CONTROLLER = "SECTION_CONTROLLER"

# SYSTEM_STATE is one in-memory dict in this process. It is not safe across
# multiple uvicorn workers. The database is the durable audit log; this dict
# is only the fast cache for the worker that owns it.
SYSTEM_STATE: dict[str, object] = {
    "active_plan": "PUNCTUALITY_FIRST",
    "sanctioned": False,
    "sanctioned_by": None,
    "submitted_by": None,
    "freight_held": False,
    "emergency_active": False,
    "dossier_submitted": False,
    "coa_dossier_id": None,
    "reject_reason": None,
    "custom_weights": {"pass": 300, "frt": 2, "overdue": 50},
    "earliest_start_min": 0,
    "possession": None,
    "tsr": None,
}

# ---------------------------------------------------------------------------
# Adapter layer: backend dataclasses  ->  exact JSON shape index.html expects
#
# defects.py / timetable.py carry no `task_id` / `chainage_km` / `description`
# / `priority_score` / `machine_id` / `crew_headcount` / `source` fields, and
# the frontend never gets those keys from a raw dataclass dump. Everything
# below is the missing translation layer. A few fields (source, crew count)
# aren't modelled in defects.py at all yet, so they're derived heuristically
# here and flagged — replace with real fields on TrackDefect when you have
# that data.
# ---------------------------------------------------------------------------

_SOURCE_BY_DEFECT_TYPE = {
    defects.DefectType.RAIL_FRACTURE: defects.SOURCE_TMS,
    defects.DefectType.WELD_FAILURE: defects.SOURCE_TMS,
    defects.DefectType.SIGNAL_INTERLOCK: defects.SOURCE_SMMS,
    defects.DefectType.OHE_SAG: defects.SOURCE_TDMS,
    defects.DefectType.TAMPING_REQUIRED: defects.SOURCE_TMS,
    defects.DefectType.DEEP_SCREENING: defects.SOURCE_TMS,
}


def _crew_headcount(defect: "defects.TrackDefect") -> int:
    """Same headcount the defect table already shows. TrackDefect has no crew field."""
    machine = defect.required_machine
    return (
        4
        + (3 if machine is not defects.MachineType.NONE else 0)
        + (2 if defect.is_mandatory else 0)
    )


def adapt_defect(d: "defects.TrackDefect") -> dict:
    machine = d.required_machine
    return {
        "task_id": d.defect_id,
        "source": _SOURCE_BY_DEFECT_TYPE.get(d.defect_type, defects.SOURCE_TMS),
        "chainage_km": d.km_location,
        "track_line": d.track_line,
        "description": d.defect_type.value.replace("_", " ").title(),
        "priority_score": round(
            defects.compute_priority_urgency_score(
                d,
                days_overdue=d.days_overdue,
                has_tsr=d.has_tsr,
                traffic_density=defects.estimate_traffic_density(d),
            )
        ),
        "predicted_days_to_critical": round(ml_priority.predict_days_to_critical(d), 1),
        "required_duration_min": int(d.required_block_min),
        "machine_id": None if machine is defects.MachineType.NONE else machine.value,
        "crew_headcount": _crew_headcount(d),
    }


_WINDOW_COLOR = {
    calendar_plan.LABEL_NIGHTLY: "#64748b",
    calendar_plan.LABEL_MEGA_BLOCK: "#38bdf8",
    calendar_plan.LABEL_EXTENDED: "#c084fc",
}


def adapt_day_plan(day: calendar_plan.DayPlan) -> dict[str, object]:
    booked = day.result is not None and day.result.status in ("OPTIMAL", "FEASIBLE")
    return {
        "date": day.calendar_date.isoformat(),
        "weekday": day.calendar_date.strftime("%A"),
        "window_label": day.window.label,
        "window_color": _WINDOW_COLOR.get(day.window.label, "#64748b"),
        "window_start_hhmm": timetable.minute_to_hhmm(day.window.start_min),
        "window_end_hhmm": timetable.minute_to_hhmm(day.window.end_min),
        "assigned_bundle_ids": list(day.assigned_bundle_ids),
        "solver_status": day.result.status if day.result is not None else "NO_WORK",
        "blocks": [
            {
                "bundle_id": block.bundle_id,
                "start_hhmm": timetable.minute_to_hhmm(block.start_min),
                "end_hhmm": timetable.minute_to_hhmm(block.end_min),
            }
            for block in (day.result.blocks if booked else ())
        ],
    }


def _console_bundle(bundle: "defects.MaintenanceBundle") -> dict[str, object]:
    """Facts the officer and section-controller consoles render for one cluster."""
    sources = sorted(
        {
            _SOURCE_BY_DEFECT_TYPE.get(defect.defect_type, defects.SOURCE_TMS)
            for defect in bundle.defects
        }
    )
    return {
        "bundle_id": bundle.bundle_id,
        "track_line": bundle.track_line,
        "section_id": bundle.section_id,
        "start_km": bundle.start_km,
        "end_km": bundle.end_km,
        "required_duration_min": int(bundle.required_duration_min),
        "work_duration_min": int(bundle.required_duration_min),
        "sequential_duration_sum": sum(int(defect.required_block_min) for defect in bundle.defects),
        "clearance_min": solver.clearance_min(bundle),
        "block_type": bundle.block_type.value,
        "power_sector_id": bundle.power_sector_id,
        "machine_type": bundle.required_machine.value,
        "sources": sources,
        "crew_headcount": sum(_crew_headcount(defect) for defect in bundle.defects),
        "tasks": [{"task_id": defect.defect_id} for defect in bundle.defects],
        "machines": [
            {
                "machine_type": str(row["machine_type"]),
                "depot_km": float(row["depot_km"]),
                "transit_min": float(row["transit_min"]),
                "site_km": float(row["site_km"]),
            }
            for row in solver.machine_transits(bundle)
        ],
    }


def adapt_bundle(b: "defects.MaintenanceBundle") -> dict:
    # Frontend clusters defects into a bundle by matching task_id here.
    return _console_bundle(b)


def _train_priority(train_type: str) -> int:
    return 1 if train_type in (timetable.TYPE_PREMIUM, timetable.TYPE_SUPERFAST) else 0


def _train_current_line(t: "timetable.TrainTrajectory") -> str:
    if t.waypoints:
        return t.waypoints[0].line
    return "UP"


def adapt_train(t: "timetable.TrainTrajectory") -> dict[str, object]:
    return {
        "id": t.train_id,
        "type": t.train_type,
        "name": t.service_name,
        "line": _train_current_line(t),
        "start_km": t.origin_km,
        "end_km": t.dest_km,
        "min_km": t.min_km,
        "max_km": t.max_km,
        "priority": _train_priority(t.train_type),
        "direction": t.direction,
        "start_min": t.window.start_min,
        "end_min": t.window.end_min,
        "origin_code": t.origin_code,
        "dest_code": t.dest_code,
        "uses_mid_ghat": t.uses_mid_ghat,
        "waypoints": [
            {"m": wp.minute, "km": wp.km, "line": wp.line, "status": wp.status}
            for wp in t.waypoints
        ],
    }


_SECTION_COPY: dict[str, tuple[str, str]] = {
    topology.SECTION_1_ID: ("Section 1", "Suburban quadruple line"),
    topology.SECTION_2_ID: ("Section 2", "Kalyan–Karjat double line"),
    topology.SECTION_3_ID: ("Section 3", "Bhor Ghat bottleneck · 3 track"),
    topology.SECTION_4_ID: ("Section 4", "Pune plateau double line"),
}

_SECTOR_SECTIONS: dict[str, frozenset[str]] = {
    "GHAT": frozenset({topology.SECTION_3_ID}),
    "SUBURBAN": frozenset({topology.SECTION_1_ID, topology.SECTION_2_ID}),
    "PUNE": frozenset({topology.SECTION_4_ID}),
}


def adapt_station(station: topology.StationNode) -> dict[str, object]:
    return {
        "code": station.code,
        "name": station.name,
        "km": station.km,
        "lat": station.lat,
        "lng": station.lng,
        "elev": station.elevation_m,
        "section_id": station.section_id,
        "crossover": station.has_crossover,
        "zone": station.zone,
    }


def adapt_sections() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for section_id, (title, detail) in _SECTION_COPY.items():
        start_km, end_km = topology.SECTION_BOUNDS[section_id]
        rows.append(
            {
                "id": section_id,
                "name": title,
                "detail": detail,
                "start_km": start_km,
                "end_km": end_km,
                "lines": list(topology.LINES_BY_SECTION[section_id]),
            }
        )
    return rows


def _line_matches(bundle_line: str, requested: str | None) -> bool:
    if not requested:
        return True
    req = requested.upper()
    line = bundle_line.upper()
    if req == "MID":
        return line == topology.LINE_MID
    if req == "UP":
        return line.startswith("UP")
    if req == "DN":
        return line.startswith("DN")
    return line == req


def _select_bundle(
    bundles: list[defects.MaintenanceBundle],
    target_line: str | None,
    sector: str | None,
) -> defects.MaintenanceBundle:
    chosen = list(bundles)
    if sector:
        allowed = _SECTOR_SECTIONS.get(sector.upper())
        if allowed:
            filtered = [bundle for bundle in chosen if bundle.section_id in allowed]
            if filtered:
                chosen = filtered
    if target_line:
        filtered = [bundle for bundle in chosen if _line_matches(bundle.track_line, target_line)]
        if filtered:
            chosen = filtered
    return max(chosen, key=lambda bundle: bundle.urgency_score)


def _finite_or_none(value: float) -> float | None:
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return None
    return round(number, 2)


def _spatial_bundling_payload() -> dict[str, object]:
    """Corridor Formula 2 proof: minutes handed back by packing nearby jobs."""
    bundles = tuple(defects.cluster_defects_into_bundles(data_ingestion.get_effective_defects()))
    standalone_min, bundled_min, gain_pct, clusters, defect_count = (
        metrics.calculate_spatial_bundling_proof(bundles)
    )
    return {
        "bundling_standalone_min": standalone_min,
        "bundling_bundled_min": bundled_min,
        "bundling_saved_min": standalone_min - bundled_min,
        "bundling_gain_pct": gain_pct,
        "bundling_clusters": clusters,
        "bundling_defects": defect_count,
    }


def _schedule_metrics_payload(
    bundles: tuple[defects.MaintenanceBundle, ...],
    result: solver.SolverResult,
) -> dict[str, object]:
    """Turn one CP-SAT result into the numbers the VDU ribbon renders.

    ``booked`` is true only for OPTIMAL and FEASIBLE. Other statuses still
    carry the blocks the solver returned, and those blocks are scored so the
    ribbon can show defects, machines, and time saved. Chaos is omitted when
    the objective is infinite.
    """
    booked = result.status in ("OPTIMAL", "FEASIBLE")
    report = metrics.generate_metrics(bundles, result)
    saved = report.standalone_downtime_min - report.bundled_downtime_min
    return {
        "booked": booked,
        "solver_status": result.status,
        "chaos_score": _finite_or_none(result.chaos_score),
        "blocks_scheduled": len(result.blocks),
        "gain_pct": report.efficiency_gain_pct,
        "defects_cleared": report.total_defects_cleared,
        "standalone_min": report.standalone_downtime_min,
        "bundled_min": report.bundled_downtime_min,
        "track_time_saved_min": saved,
        "passenger_delayed": report.passenger_trains_delayed,
        "freight_delayed": report.freight_trains_delayed,
        "total_delay_min": report.total_delay_minutes,
        "cascade_impact": report.cascade_impact_score,
        "machines": report.machine_utilization_hours,
        "report": metrics.get_metrics_report(report),
    }


def _confidence_from_solve(result: solver.SolverResult) -> int:
    """Map one CP-SAT result to a 0–99 confidence for the VDU.

    A proven optimum is 99. A feasible incumbent is 100 minus the optimality
    gap, and never below 40. An unsolved status is 0.
    """
    if result.status == "OPTIMAL":
        return 99
    if result.status == "FEASIBLE":
        gap_pct = (
            abs(result.chaos_score - result.best_bound)
            / max(1.0, abs(result.chaos_score))
            * 100.0
        )
        return max(40, round(100.0 - gap_pct))
    return 0


def _profile_weights(profile_name: str, pass_weight: int, overdue_weight: int) -> tuple[int, int, int]:
    """Passenger, freight, and postponement weights. Every profile still sees every train."""
    passenger = max(1, int(pass_weight))
    overdue = max(1, int(overdue_weight))
    if profile_name == "ASSET_FIRST":
        return max(20, passenger // 4), solver.W_FREIGHT_DELAY, overdue * 3
    if profile_name == "MEGA_BLOCK":
        return max(40, passenger // 2), 1, overdue * 2
    return passenger, solver.W_FREIGHT_DELAY, overdue


def _trains_with_freight_forecast(
    trains: tuple[timetable.TrainTrajectory, ...],
    seed: int,
) -> tuple[timetable.TrainTrajectory, ...]:
    """Replace freight trips with one seeded forecast. Passenger trips stay put."""
    forecast = {
        item.train_id: item for item in timetable.generate_freight_forecast(seed)
    }
    return tuple(forecast.get(train.train_id, train) for train in trains)


def _freight_delay_count(
    bundles: list[defects.MaintenanceBundle],
    result: solver.SolverResult,
) -> int:
    report = metrics.generate_metrics(tuple(bundles), result)
    return int(report.freight_trains_delayed)


def _best_solved_option(options: list[dict[str, object]]) -> dict[str, object] | None:
    """Booked plan with the calmest timetable. Ties keep the earlier profile."""
    if not options:
        return None

    def sort_key(opt: dict[str, object]) -> tuple[int, float, int]:
        raw_metrics = opt.get("metrics")
        metrics_payload = raw_metrics if isinstance(raw_metrics, dict) else {}
        booked = (
            opt.get("solver_status") in ("OPTIMAL", "FEASIBLE")
            and opt.get("start_time_str") != "—"
        )
        chaos = metrics_payload.get("chaos_score")
        chaos_key = (
            float(chaos)
            if isinstance(chaos, (int, float)) and math.isfinite(float(chaos))
            else float("inf")
        )
        passengers = int(metrics_payload.get("passenger_delayed") or 0)
        return (0 if booked else 1, chaos_key, passengers)

    return min(options, key=sort_key)


def _freight_forecast_range(
    options: list[dict[str, object]],
    solve_bundles: list[defects.MaintenanceBundle],
    trains: tuple[timetable.TrainTrajectory, ...],
    pass_weight: int,
    overdue_weight: int,
    earliest: int,
) -> dict[str, int]:
    """
    Nominal freight delay for the best profile, plus two jittered re-solves.

    ``expected`` is the deterministic WTT. ``optimistic`` and ``pessimistic``
    are the lower and higher freight-delay counts from seeds 1 and 2.
    """
    best = _best_solved_option(options)
    if best is None:
        return {"optimistic": 0, "expected": 0, "pessimistic": 0}
    raw_metrics = best.get("metrics")
    metrics_payload = raw_metrics if isinstance(raw_metrics, dict) else {}
    expected = int(metrics_payload.get("freight_delayed") or 0)
    profile_name = str(best.get("profile") or "PUNCTUALITY_FIRST")
    scenario_counts: list[int] = []
    for seed in (1, 2):
        forecast_result = _solve_bundles_for(
            solve_bundles,
            _trains_with_freight_forecast(trains, seed),
            profile_name,
            pass_weight,
            overdue_weight,
            earliest,
            time_limit_sec=3.0,
        )
        scenario_counts.append(_freight_delay_count(solve_bundles, forecast_result))
    return {
        "optimistic": min(scenario_counts),
        "expected": expected,
        "pessimistic": max(scenario_counts),
    }


def _solve_bundles_for(
    bundles: list[defects.MaintenanceBundle],
    trains: tuple[timetable.TrainTrajectory, ...],
    profile_name: str,
    pass_weight: int,
    overdue_weight: int,
    earliest_start_min: int,
    time_limit_sec: float,
) -> solver.SolverResult:
    passenger_weight, freight_weight, postponement_weight = _profile_weights(
        profile_name, pass_weight, overdue_weight
    )
    return solver.solve_corridor_schedule(
        bundles,
        trains,
        time_limit_sec=time_limit_sec,
        earliest_start_min=earliest_start_min,
        passenger_weight=passenger_weight,
        freight_weight=freight_weight,
        postponement_weight=postponement_weight,
    )


def _target_context(
    target_line: str | None,
    sector: str | None,
) -> tuple[defects.MaintenanceBundle, list[defects.MaintenanceBundle], tuple[timetable.TrainTrajectory, ...]]:
    raw_defects = data_ingestion.get_effective_defects()
    bundles = list(defects.cluster_defects_into_bundles(raw_defects))
    trains = timetable.get_all_trajectories()
    target = _select_bundle(bundles, target_line, sector)
    solve_bundles = [
        bundle
        for bundle in bundles
        if bundle.section_id == target.section_id and _line_matches(bundle.track_line, target.track_line)
    ] or [target]
    return target, solve_bundles, trains


def _weights() -> tuple[int, int]:
    custom = SYSTEM_STATE["custom_weights"]
    assert isinstance(custom, dict)
    return int(custom["pass"]), int(custom["overdue"])


def _earliest() -> int:
    return max(0, int(SYSTEM_STATE["earliest_start_min"]))


def _machine_site_offsets(
    timeline: dict[str, object],
    bundle: defects.MaintenanceBundle,
) -> list[dict[str, object]]:
    specs = timeline["machines"]
    assert isinstance(specs, list)
    count = len(specs)
    placed: list[dict[str, object]] = []
    allocations = defects.allocate_machinery(
        [(bundle, float(timeline["start_min"]), float(timeline["end_min"]))]
    )
    for index, spec in enumerate(specs):
        assert isinstance(spec, dict)
        offset = (index - (count - 1) / 2.0) * 0.45
        site = float(spec["site_km"]) + offset
        site = min(bundle.end_km - 0.05, max(bundle.start_km + 0.05, site))
        machine_name = str(spec["machine_type"])
        unit = next(
            (item.unit_index for item in allocations if item.machine_type.value == machine_name),
            index + 1,
        )
        placed.append(
            {
                "machine_type": machine_name,
                "unit_index": unit,
                "depot_km": float(spec["depot_km"]),
                "speed_kmh": float(spec["speed_kmh"]),
                "transit_min": float(spec["transit_min"]),
                "site_km": round(site, 3),
                "path_offset_km": round(offset, 3),
            }
        )
    return placed


def _execution_record(
    bundle: defects.MaintenanceBundle,
    block: solver.ScheduledBlock,
    result: solver.SolverResult,
    immediate: bool,
) -> dict[str, object]:
    timeline = solver.possession_timeline(bundle, block.start_min, block.end_min)
    power = bundle.block_type is defects.BlockType.POWER_AND_TRAFFIC
    closed_lo, closed_hi, protected = solver.protection_zone(bundle)
    earth_up, earth_down = closed_lo, closed_hi
    chaos = None if math.isinf(result.chaos_score) else round(result.chaos_score, 2)
    return {
        "bundle_id": bundle.bundle_id,
        "section_id": bundle.section_id,
        "track_line": protected,
        "block_type": bundle.block_type.value,
        "power_sector_id": bundle.power_sector_id,
        "worksite_start_km": bundle.start_km,
        "worksite_end_km": bundle.end_km,
        "closed_start_km": closed_lo,
        "closed_end_km": closed_hi,
        "protected_lines": [protected],
        "approach_km": 1.6,
        "start_min": int(block.start_min),
        "end_min": int(block.end_min),
        "start_hhmm": timetable.minute_to_hhmm(block.start_min),
        "end_hhmm": timetable.minute_to_hhmm(block.end_min),
        "earth_required": power,
        "earth_up_km": earth_up,
        "earth_down_km": earth_down,
        "immediate": immediate,
        "timeline": timeline,
        "machines": _machine_site_offsets(timeline, bundle),
        "solver_status": result.status,
        "chaos_score": chaos,
        "delayed_trains": [
            {"train_id": item.train_id, "delay_minutes": item.delay_min}
            for item in result.delayed_trains
        ],
    }


def _active_possessions() -> list[timetable.LinePossession]:
    raw = SYSTEM_STATE.get("possession")
    if not SYSTEM_STATE.get("sanctioned") or not isinstance(raw, dict):
        return []
    lines = raw.get("protected_lines") or [raw["track_line"]]
    return [
        timetable.LinePossession(
            possession_id=str(raw["bundle_id"]),
            protected_lines=tuple(str(line) for line in lines),
            start_km=float(raw["closed_start_km"]),
            end_km=float(raw["closed_end_km"]),
            start_min=float(raw["start_min"]),
            end_min=float(raw["end_min"]),
            approach_km=float(raw["approach_km"]),
        )
    ]


def _machine_km(minute: float, spec: dict[str, object], plan: dict[str, object], live: bool) -> float:
    depot = float(spec["depot_km"])
    site = float(spec["site_km"])
    if not live:
        return round(min(192.0, max(0.0, depot)), 3)
    timeline = plan["timeline"]
    assert isinstance(timeline, dict)
    start = float(plan["start_min"])
    end = float(plan["end_min"])
    arrive = float(timeline["arrive_min"])
    returning = float(timeline["return_start_min"])
    if minute <= start:
        km = depot
    elif minute < arrive:
        span = max(1e-6, arrive - start)
        ratio = min(1.0, max(0.0, (minute - start) / span))
        km = depot + (site - depot) * ratio
    elif minute < returning:
        km = site
    else:
        span = max(1e-6, end - returning)
        ratio = min(1.0, max(0.0, (minute - returning) / span))
        km = site + (depot - site) * ratio
    return round(min(192.0, max(0.0, km)), 3)


def _separate_machine_kms(machine_rows: list[dict[str, object]]) -> None:
    """Keep division plant off the same chainage while they share the closed line."""
    ordered = sorted(machine_rows, key=lambda row: float(row["km"]))
    for index in range(1, len(ordered)):
        previous = float(ordered[index - 1]["km"])
        current = float(ordered[index]["km"])
        if current - previous < 0.35:
            ordered[index]["km"] = round(min(192.0, previous + 0.45), 3)


def _step(step_id: str, label: str, state: str) -> dict[str, str]:
    return {"id": step_id, "label": label, "state": state}


def _mark(done: bool, active: bool = False, skipped: bool = False) -> str:
    if skipped:
        return "na"
    if done:
        return "done"
    if active:
        return "active"
    return "pending"


def _trains_inside(rows: list[dict[str, object]], plan: dict[str, object]) -> list[str]:
    lo = float(plan["closed_start_km"])
    hi = float(plan["closed_end_km"])
    lines = {str(line) for line in plan["protected_lines"]}
    found: list[str] = []
    for row in rows:
        km = float(row["current_km"])
        if str(row["current_line"]) in lines and lo < km < hi:
            found.append(str(row["train_id"]))
    return found


def _phase_snapshot(minute: float, rows: list[dict[str, object]]) -> dict[str, object] | None:
    raw = SYSTEM_STATE.get("possession")
    if not SYSTEM_STATE.get("sanctioned") or not isinstance(raw, dict):
        return None
    clock = timetable.wrap_minute(minute)
    start = float(raw["start_min"])
    end = float(raw["end_min"])
    live = start <= clock < end
    if clock >= end:
        starts_in = (1440.0 - clock) + start
    elif clock < start:
        starts_in = start - clock
    else:
        starts_in = 0.0
    timeline = raw["timeline"]
    assert isinstance(timeline, dict)
    arrive = float(timeline["arrive_min"])
    ready = float(timeline["protection_ready_min"])
    work_start = float(timeline["work_start_min"])
    work_end = float(timeline["work_end_min"])
    power = bool(raw["earth_required"])
    machines_arrived = live and clock >= arrive
    machines_returning = live and clock >= float(timeline["return_start_min"])
    machines_on_site = machines_arrived and not machines_returning
    # The feeder stays dead until the possession ends. Earths come off when
    # the work stops, and the machine then rolls clear on a dead line.
    power_isolated = live and power and clock >= arrive and clock < end
    earth_on = live and power and clock >= ready and clock < work_end
    prerequisites = machines_on_site and ((not power) or (power_isolated and earth_on))
    work_on = live and clock >= work_start and clock < work_end and prerequisites
    inside = _trains_inside(rows, raw) if live else []
    machine_rows: list[dict[str, object]] = []
    specs = raw["machines"]
    assert isinstance(specs, list)
    for spec in specs:
        assert isinstance(spec, dict)
        km = _machine_km(clock, spec, raw, live)
        machine_rows.append(
            {
                "machine_type": spec["machine_type"],
                "unit_index": spec["unit_index"],
                "km": km,
                "line": raw["track_line"],
                "on_line": live,
                "at_site": machines_on_site,
                "depot_km": spec["depot_km"],
                "speed_kmh": 0.0 if (not live or machines_on_site) else spec["speed_kmh"],
            }
        )
    _separate_machine_kms(machine_rows)
    if not live:
        phase = "WAITING"
    elif work_on:
        phase = "WORK"
    elif machines_returning:
        phase = "RESTORING"
    elif power and machines_arrived and not earth_on:
        phase = "ISOLATING"
    elif not machines_arrived:
        phase = "MACHINES_ENROUTE"
    else:
        phase = "POSSESSION"
    steps = [
        _step("possession", "Line taken and signals at red", _mark(live)),
        _step("trains", "Trains held clear of the closed line", _mark(live and not inside, active=live and bool(inside))),
        _step("machines", "Machines on site and separated", _mark(machines_on_site or (live and machines_returning), active=live and not machines_arrived)),
        _step("power", "25 kV feeder isolated", _mark(power_isolated or (live and power and clock >= work_end), active=live and power and machines_arrived and not power_isolated, skipped=not power)),
        _step("earth_up", "Earth connected at the near end", _mark(earth_on or (live and power and clock >= work_end), active=live and power and machines_arrived and not earth_on, skipped=not power)),
        _step("earth_down", "Earth connected at the far end", _mark(earth_on or (live and power and clock >= work_end), active=live and power and machines_arrived and not earth_on, skipped=not power)),
        _step("work", "Maintenance work in progress", _mark(live and clock >= work_end, active=work_on)),
        _step("restore", "Earth off, power restored, machines clear", _mark(False, active=live and machines_returning)),
    ]
    return {
        "phase": phase,
        "live": live,
        "minute": round(clock, 2),
        "hhmm": timetable.minute_to_hhmm(clock),
        "starts_in_min": round(starts_in, 1),
        "possession": raw,
        "power": "ISOLATED" if power_isolated else "LIVE",
        "earth_required": power,
        "earth_up": earth_on,
        "earth_down": earth_on,
        "earth_up_km": raw["earth_up_km"],
        "earth_down_km": raw["earth_down_km"],
        "machines_on_site": machines_on_site,
        "work_in_progress": work_on,
        "trains_inside": inside,
        "trains_inside_count": len(inside),
        "machines": machine_rows,
        "steps": steps,
    }


def _machine_type_of(record: dict[str, object]) -> str:
    machines = record.get("machines")
    if isinstance(machines, list) and machines and isinstance(machines[0], dict):
        name = machines[0].get("machine_type")
        if isinstance(name, str) and name:
            return name
    return "NONE"


def _required_int(record: dict[str, object], key: str) -> int:
    value = record[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"Possession field {key} must be an integer.")
    return value


def _record_possession(
    record: dict[str, object],
    plan_type: str,
    sanctioned_by: str,
    emergency: bool,
    outcome: str,
    reject_reason: str | None,
) -> None:
    persistence.record_possession_event(
        bundle_id=str(record["bundle_id"]),
        track_line=str(record["track_line"]),
        section_id=str(record["section_id"]),
        start_min=_required_int(record, "start_min"),
        end_min=_required_int(record, "end_min"),
        machine_type=_machine_type_of(record),
        power_sector_id=str(record["power_sector_id"]),
        plan_type=plan_type,
        solver_status=str(record["solver_status"]),
        sanctioned_by=sanctioned_by,
        emergency=emergency,
        outcome=outcome,
        reject_reason=reject_reason,
        possession_payload=json.dumps(record),
    )


def _clear_possession(
    outcome: str = "CLEARED",
    actor: str | None = None,
    reject_reason: str | None = None,
) -> None:
    raw = SYSTEM_STATE.get("possession")
    if SYSTEM_STATE.get("sanctioned") and isinstance(raw, dict):
        actor_name = actor if actor else str(SYSTEM_STATE.get("sanctioned_by") or _CONSOLE_CONTROLLER)
        _record_possession(
            raw,
            str(SYSTEM_STATE["active_plan"]),
            actor_name,
            bool(SYSTEM_STATE.get("emergency_active")),
            outcome,
            reject_reason,
        )
    SYSTEM_STATE["sanctioned"] = False
    SYSTEM_STATE["sanctioned_by"] = None
    SYSTEM_STATE["coa_dossier_id"] = None
    SYSTEM_STATE["freight_held"] = False
    SYSTEM_STATE["possession"] = None
    SYSTEM_STATE["emergency_active"] = False


def _lock_possession(
    record: dict[str, object],
    plan_type: str,
    dossier_id: str,
    emergency: bool,
    outcome: str = "SANCTIONED",
) -> None:
    record["coa_dossier_id"] = dossier_id
    _record_possession(record, plan_type, _CONSOLE_CONTROLLER, emergency, outcome, None)
    SYSTEM_STATE["active_plan"] = plan_type
    SYSTEM_STATE["sanctioned"] = True
    SYSTEM_STATE["sanctioned_by"] = _CONSOLE_CONTROLLER
    SYSTEM_STATE["freight_held"] = True
    SYSTEM_STATE["dossier_submitted"] = True
    SYSTEM_STATE["reject_reason"] = None
    SYSTEM_STATE["coa_dossier_id"] = dossier_id
    SYSTEM_STATE["possession"] = record
    SYSTEM_STATE["emergency_active"] = emergency


@app.post("/api/ingest")
async def ingest_defects(
    tms_file: UploadFile | None = File(None),
    smms_file: UploadFile | None = File(None),
    tdms_file: UploadFile | None = File(None),
    coa_file: UploadFile | None = File(None),
    folder: str | None = Form(None),
) -> dict[str, object]:
    """
    Run the simulated TMS / SMMS / TDMS / COA ingestion pipeline.

    Two ways to call it:
      - Upload one or more files (tms_file / smms_file / tdms_file / coa_file);
        any source left out is simply not re-ingested this run.
      - Dev mode: pass `folder` (form field), or omit everything, to read the
        four well-known filenames straight off disk (defaults to
        ./sample_data) -- useful for the SIH demo without a real upload UI.

    The accepted defects become the process-wide "effective" defect set used
    by /api/master-data, /api/optimize, and /api/horizon-plan. If nothing is
    ever ingested (or every row across every source is rejected), those
    endpoints keep using defects.get_default_defects() automatically.
    """
    provided_any_file = any((tms_file, smms_file, tdms_file, coa_file))

    if provided_any_file:
        tms_text = (await tms_file.read()).decode("utf-8") if tms_file is not None else None
        smms_text = (await smms_file.read()).decode("utf-8") if smms_file is not None else None
        tdms_text = (await tdms_file.read()).decode("utf-8") if tdms_file is not None else None
        coa_text = (await coa_file.read()).decode("utf-8") if coa_file is not None else None
        summary = data_ingestion.ingest_all(
            tms_text=tms_text,
            smms_text=smms_text,
            tdms_text=tdms_text,
            coa_text=coa_text,
        )
    else:
        summary = data_ingestion.ingest_from_folder(folder or "sample_data")

    data_ingestion.set_last_ingest(summary)
    return {
        "status": "FALLBACK" if summary.used_fallback else "INGESTED",
        "message": (
            "No defects were accepted from any source; /api/master-data will keep "
            "using the synthetic default backlog."
            if summary.used_fallback
            else f"{len(summary.all_defects())} defect(s) accepted; now the effective backlog."
        ),
        **summary.to_dict(),
    }


@app.get("/api/master-data")
def get_master_data():
    raw_defects = data_ingestion.get_effective_defects()
    bundles = defects.cluster_defects_into_bundles(raw_defects)
    trains = timetable.get_all_trajectories()

    return {
        "corridor": "Mumbai CSMT - Pune Jn",
        "total_km": topology.CORRIDOR_END_KM,
        "stations": [adapt_station(station) for station in topology.STATIONS],
        "sections": adapt_sections(),
        "trains": [adapt_train(t) for t in trains],
        "defects": [adapt_defect(d) for d in raw_defects],
        "bundles": [adapt_bundle(b) for b in bundles],
        "clock": {
            "day_minutes": timetable.TOTAL_SIMULATION_MINUTES,
            "start_min": 240.0,
            "minutes_per_second": timetable.BASE_SPEED_RATIO,
        },
        "system_state": SYSTEM_STATE,
    }


@app.get("/api/positions")
def get_positions(minute: float = 240.0) -> dict[str, object]:
    """Live snapshot. Section 1 and 3 spare lines divert; other sections hold clear."""
    clock = timetable.wrap_minute(minute)
    active = timetable.get_regulated_positions(clock, _active_possessions())
    for row in active:
        row["diverted"] = row.get("status") == timetable.STATUS_DIVERTED
    execution = _phase_snapshot(clock, active)
    return {
        "minute": clock,
        "hhmm": timetable.minute_to_hhmm(clock),
        "active_count": len(active),
        "active": active,
        "execution": execution,
    }


@app.get("/api/system-state")
def get_system_state():
    return SYSTEM_STATE


@app.get("/api/optimize")
def get_optimized_plans(
    pass_weight: int | None = None,
    overdue_weight: int | None = None,
    target_line: str | None = None,
    sector: str | None = None,
) -> dict[str, object]:
    pass_weight = pass_weight if pass_weight is not None else SYSTEM_STATE["custom_weights"]["pass"]
    overdue_weight = overdue_weight if overdue_weight is not None else SYSTEM_STATE["custom_weights"]["overdue"]
    SYSTEM_STATE["custom_weights"]["pass"] = int(pass_weight)
    SYSTEM_STATE["custom_weights"]["overdue"] = int(overdue_weight)

    target_bundle, solve_bundles, trains = _target_context(target_line, sector)

    if not solve_bundles:
        return {
            "status": "NO_DEFECTS",
            "target_bundle": None,
            "candidate_options": [],
            "freight_forecast_range": {"optimistic": 0, "expected": 0, "pessimistic": 0},
            "system_state": SYSTEM_STATE,
        }

    # Three real solves of the same model. Weights change. No train is deleted
    # from the constraint set, so a "cheaper" profile cannot leave a rake in the block.
    profiles = ("PUNCTUALITY_FIRST", "ASSET_FIRST", "MEGA_BLOCK")
    earliest = _earliest()
    bundling_proof = _spatial_bundling_payload()

    candidate_options: list[dict[str, object]] = []
    for profile_name in profiles:
        result = _solve_bundles_for(
            solve_bundles,
            trains,
            profile_name,
            int(pass_weight),
            int(overdue_weight),
            earliest,
            time_limit_sec=3.0,
        )
        block = _block_for(target_bundle, result)
        if result.status not in ("OPTIMAL", "FEASIBLE") or block is None:
            candidate_options.append(
                {
                    "profile": profile_name,
                    "solver_status": result.status,
                    "start_time_str": "—",
                    "end_time_str": "—",
                    "net_work_duration_min": 0,
                    "total_window_required_min": 0,
                    "confidence_score": 0,
                    "train_impacts": [],
                    "downtime_reduction_pct": bundling_proof["bundling_gain_pct"],
                    "metrics": {
                        "booked": False,
                        "solver_status": result.status,
                        "chaos_score": None,
                        "blocks_scheduled": 0,
                        "gain_pct": bundling_proof["bundling_gain_pct"],
                        "defects_cleared": 0,
                        "standalone_min": bundling_proof["bundling_standalone_min"],
                        "bundled_min": bundling_proof["bundling_bundled_min"],
                        "track_time_saved_min": bundling_proof["bundling_saved_min"],
                        "passenger_delayed": 0,
                        "freight_delayed": 0,
                        "total_delay_min": 0,
                        "cascade_impact": 0,
                        "machines": {},
                        "report": {},
                        **bundling_proof,
                    },
                }
            )
            continue

        window_min = int(block.end_min - block.start_min)
        schedule_metrics = _schedule_metrics_payload(tuple(solve_bundles), result)
        schedule_metrics.update(bundling_proof)

        train_impacts = [
            {"train_id": dt.train_id, "delay_minutes": dt.delay_min}
            for dt in result.delayed_trains
        ]
        confidence = _confidence_from_solve(result)

        candidate_options.append({
            "profile": profile_name,
            "solver_status": result.status,
            "start_time_str": timetable.minute_to_hhmm(block.start_min),
            "end_time_str": timetable.minute_to_hhmm(block.end_min),
            "net_work_duration_min": window_min,
            "total_window_required_min": window_min,
            "confidence_score": confidence,
            "train_impacts": train_impacts,
            "downtime_reduction_pct": bundling_proof["bundling_gain_pct"],
            "metrics": schedule_metrics,
        })

    return {
        "status": "SUCCESS" if candidate_options else "INFEASIBLE",
        "target_bundle": _console_bundle(target_bundle),
        "candidate_options": candidate_options,
        "freight_forecast_range": _freight_forecast_range(
            candidate_options,
            solve_bundles,
            trains,
            int(pass_weight),
            int(overdue_weight),
            earliest,
        ),
        "system_state": SYSTEM_STATE,
    }


@app.get("/api/horizon-plan")
def get_horizon_plan(days: int = 7, start_date: str | None = None) -> dict[str, object]:
    """
    days=7  -> weekly view
    days=28 -> monthly view (frontend chunks the 28 days into 4 weeks)
    start_date -> "YYYY-MM-DD"; defaults to today if omitted.
    """
    from datetime import date as _date, datetime as _datetime

    start = (
        _datetime.strptime(start_date, "%Y-%m-%d").date()
        if start_date
        else _date.today()
    )
    days = max(1, min(28, int(days)))

    all_bundles = list(defects.cluster_defects_into_bundles(data_ingestion.get_effective_defects()))
    plan = calendar_plan.build_horizon_plan(all_bundles, start, days)

    day_payloads = [adapt_day_plan(d) for d in plan.days]
    weeks = [day_payloads[i : i + 7] for i in range(0, len(day_payloads), 7)]

    return {
        "start_date": start.isoformat(),
        "num_days": days,
        "days": day_payloads,
        "weeks": weeks,
        "unscheduled_bundle_ids": list(plan.unscheduled_bundle_ids),
        "total_bundles": len(all_bundles),
        "scheduled_bundles": len(all_bundles) - len(plan.unscheduled_bundle_ids),
    }


@app.post("/api/officer/submit")
def officer_submit(
    payload: dict[str, object] | None = Body(None),
) -> dict[str, object]:
    body = payload or {}
    plan_type = str(body.get("plan_type") or SYSTEM_STATE["active_plan"])
    if SYSTEM_STATE.get("sanctioned"):
        _clear_possession(outcome="CLEARED", actor=_CONSOLE_CONTROLLER)
    persistence.record_dossier_submission(
        plan_type=plan_type,
        submitted_by=_CONSOLE_OFFICER,
        status="SUBMITTED",
        reject_reason=None,
    )
    SYSTEM_STATE["active_plan"] = plan_type
    SYSTEM_STATE["dossier_submitted"] = True
    SYSTEM_STATE["submitted_by"] = _CONSOLE_OFFICER
    SYSTEM_STATE["sanctioned"] = False
    SYSTEM_STATE["possession"] = None
    SYSTEM_STATE["coa_dossier_id"] = None
    SYSTEM_STATE["freight_held"] = False
    SYSTEM_STATE["reject_reason"] = None
    return {
        "status": "SUBMITTED",
        "message": "Dossier transmitted.",
        "plan_type": plan_type,
        "system_state": SYSTEM_STATE,
    }


def _solve_selected(
    plan_type: str,
    target_line: str | None,
    sector: str | None,
    earliest_start_min: int,
) -> tuple[defects.MaintenanceBundle, list[defects.MaintenanceBundle], solver.SolverResult]:
    pass_weight, overdue_weight = _weights()
    target, solve_bundles, trains = _target_context(target_line, sector)
    result = _solve_bundles_for(
        solve_bundles,
        trains,
        plan_type,
        pass_weight,
        overdue_weight,
        earliest_start_min,
        time_limit_sec=4.0,
    )
    return target, solve_bundles, result


def _block_for(target: defects.MaintenanceBundle, result: solver.SolverResult) -> solver.ScheduledBlock | None:
    block = next((item for item in result.blocks if item.bundle_id == target.bundle_id), None)
    if block is None and result.blocks:
        return result.blocks[0]
    return block


@app.post("/api/chief/sanction")
def chief_sanction(
    payload: dict[str, object] | None = Body(None),
) -> dict[str, object]:
    body = payload or {}
    plan_type = str(body.get("plan_type") or SYSTEM_STATE["active_plan"])
    target_line = str(body["target_line"]) if body.get("target_line") else None
    sector = str(body["sector"]) if body.get("sector") else None
    clock = float(body.get("minute") or 0.0)
    target, _solve_bundles, result = _solve_selected(plan_type, target_line, sector, _earliest())
    block = _block_for(target, result)
    if result.status not in ("OPTIMAL", "FEASIBLE") or block is None:
        _clear_possession(outcome="CLEARED", actor=_CONSOLE_CONTROLLER)
        return {
            "status": "INFEASIBLE",
            "message": "No safe window. The line was not taken.",
            "solver_status": result.status,
            "plan_type": plan_type,
            "system_state": SYSTEM_STATE,
        }
    record = _execution_record(target, block, result, immediate=False)
    _lock_possession(
        record,
        plan_type,
        "COA-CR-2026-BLK-9182",
        emergency=False,
        outcome="SANCTIONED",
    )
    rows = timetable.get_regulated_positions(clock, _active_possessions())
    execution = _phase_snapshot(clock, rows)
    if execution is not None and bool(execution["live"]):
        message = (
            f"Order locked. {record['track_line']} is closed now, until {record['end_hhmm']}."
        )
    else:
        message = (
            f"Order locked for {record['start_hhmm']}–{record['end_hhmm']}. "
            "The line is still open. It closes at that time, or use Block Line to take it now."
        )
    return {
        "status": "SANCTIONED",
        "message": message,
        "coa_dossier_id": SYSTEM_STATE["coa_dossier_id"],
        "plan_type": plan_type,
        "solver_status": result.status,
        "possession": record,
        "execution": execution,
        "system_state": SYSTEM_STATE,
    }


@app.post("/api/chief/take-line")
def chief_take_line(
    payload: dict[str, object] | None = Body(None),
) -> dict[str, object]:
    """Section controller takes the line at the current clock, then runs the work sequence."""
    body = payload or {}
    plan_type = str(body.get("plan_type") or SYSTEM_STATE["active_plan"])
    target_line = str(body["target_line"]) if body.get("target_line") else None
    sector = str(body["sector"]) if body.get("sector") else None
    clock = timetable.wrap_minute(float(body.get("minute") or 0.0))
    target, _solve_bundles, result = _solve_selected(plan_type, target_line, sector, int(clock))
    envelope = solver.calculate_transit_envelope(target)
    start = int(clock)
    if start + envelope > 1440:
        return {
            "status": "INFEASIBLE",
            "message": "Not enough of the operating day is left to finish the machine move, the earths, and the work.",
            "system_state": SYSTEM_STATE,
        }
    end = start + envelope
    block = solver.ScheduledBlock(
        bundle_id=target.bundle_id,
        start_min=start,
        end_min=end,
        machine_type=target.required_machine.value,
        power_sector_id=target.power_sector_id,
    )
    record = _execution_record(target, block, result, immediate=True)
    _lock_possession(
        record,
        plan_type,
        "COA-CR-2026-BLK-9182",
        emergency=False,
        outcome="SANCTIONED",
    )
    rows = timetable.get_regulated_positions(clock, _active_possessions())
    return {
        "status": "SANCTIONED",
        "message": (
            f"{record['track_line']} line taken at {timetable.minute_to_hhmm(clock)}. Trains are held clear. "
            + (
                "This clock was not a conflict-free solver window; the controller order is what is holding the trains."
                if result.status not in ("OPTIMAL", "FEASIBLE")
                else "The solver also found a feasible window from this minute."
            )
        ),
        "coa_dossier_id": SYSTEM_STATE["coa_dossier_id"],
        "plan_type": plan_type,
        "solver_status": result.status,
        "possession": record,
        "execution": _phase_snapshot(clock, rows),
        "system_state": SYSTEM_STATE,
    }


@app.post("/api/chief/reject")
def chief_reject(
    payload: dict[str, object] | None = Body(None),
) -> dict[str, object]:
    body = payload or {}
    reason = str(body.get("reason") or "Capacity constraints")
    plan_type = str(SYSTEM_STATE["active_plan"])
    _clear_possession(outcome="REJECTED", actor=_CONSOLE_CONTROLLER, reject_reason=reason)
    SYSTEM_STATE["reject_reason"] = reason
    persistence.record_dossier_submission(
        plan_type=plan_type,
        submitted_by=_CONSOLE_CONTROLLER,
        status="REJECTED",
        reject_reason=reason,
    )
    return {"status": "REJECTED", "message": "Block rejected.", "reason": reason, "system_state": SYSTEM_STATE}


@app.post("/api/chief/clear")
def chief_clear() -> dict[str, object]:
    _clear_possession(outcome="CLEARED", actor=_CONSOLE_CONTROLLER)
    SYSTEM_STATE["tsr"] = None
    SYSTEM_STATE["earliest_start_min"] = 0
    SYSTEM_STATE["dossier_submitted"] = False
    SYSTEM_STATE["reject_reason"] = None
    return {
        "status": "CLEARED",
        "message": "Line given back. Signals cleared and the feeder is live.",
        "system_state": SYSTEM_STATE,
    }


@app.post("/api/chief/shift")
def chief_shift(
    payload: dict[str, object] | None = Body(None),
) -> dict[str, object]:
    body = payload or {}
    delta = max(5, int(body.get("delta_min") or 30))
    raw = SYSTEM_STATE.get("possession")
    base = int(raw["start_min"]) if isinstance(raw, dict) else _earliest()
    shifted = min(1430, base + delta)
    SYSTEM_STATE["earliest_start_min"] = shifted
    if not isinstance(raw, dict) or not SYSTEM_STATE.get("sanctioned"):
        return {
            "status": "SHIFTED",
            "message": f"Next solve cannot start before {timetable.minute_to_hhmm(shifted)}.",
            "earliest_start_min": shifted,
            "system_state": SYSTEM_STATE,
        }
    plan_type = str(SYSTEM_STATE["active_plan"])
    locked_line = str(raw["track_line"])
    target, _solve_bundles, result = _solve_selected(plan_type, locked_line, None, shifted)
    if target.bundle_id != raw["bundle_id"]:
        matched = next(
            (
                bundle
                for bundle in defects.cluster_defects_into_bundles(data_ingestion.get_effective_defects())
                if bundle.bundle_id == raw["bundle_id"]
            ),
            None,
        )
        if matched is not None:
            target = matched
            result = _solve_bundles_for(
                [matched],
                timetable.get_all_trajectories(),
                plan_type,
                *_weights(),
                shifted,
                time_limit_sec=4.0,
            )
    block = _block_for(target, result)
    if result.status not in ("OPTIMAL", "FEASIBLE") or block is None or block.start_min < shifted:
        SYSTEM_STATE["earliest_start_min"] = base
        return {
            "status": "INFEASIBLE",
            "message": "No later window keeps the line clear. The locked possession was left as it is.",
            "solver_status": result.status,
            "system_state": SYSTEM_STATE,
        }
    record = _execution_record(target, block, result, immediate=False)
    _lock_possession(
        record,
        plan_type,
        str(SYSTEM_STATE["coa_dossier_id"] or "COA-CR-2026-BLK-9182"),
        emergency=False,
        outcome="SHIFTED",
    )
    return {
        "status": "SHIFTED",
        "message": f"Possession moved to {record['start_hhmm']}–{record['end_hhmm']}.",
        "possession": record,
        "solver_status": result.status,
        "system_state": SYSTEM_STATE,
    }


@app.post("/api/chief/caution")
def chief_caution(
    payload: dict[str, object] | None = Body(None),
) -> dict[str, object]:
    body = payload or {}
    raw = SYSTEM_STATE.get("possession")
    if isinstance(raw, dict):
        start_km = float(raw["worksite_start_km"])
        end_km = float(raw["worksite_end_km"])
        line = str(raw["track_line"])
    else:
        target = _select_bundle(
            list(defects.cluster_defects_into_bundles(data_ingestion.get_effective_defects())),
            topology.LINE_MID,
            "GHAT",
        )
        start_km, end_km, line = target.start_km, target.end_km, target.track_line
    cap = float(body.get("max_kmh") or 30.0)
    distance = abs(end_km - start_km)
    loss = metrics.tsr_time_loss_min(distance, 80.0, cap)
    order = {
        "line": line,
        "start_km": start_km,
        "end_km": end_km,
        "max_kmh": cap,
        "extra_minutes": loss,
    }
    persistence.record_tsr_order(
        line=line,
        start_km=float(start_km),
        end_km=float(end_km),
        max_kmh=float(cap),
        extra_minutes=float(loss),
        issued_by=_CONSOLE_CONTROLLER,
    )
    SYSTEM_STATE["tsr"] = order
    return {
        "status": "CAUTION",
        "message": (
            f"TSR {cap:.0f} km/h on {line} KM {start_km:.1f}–{end_km:.1f}. "
            f"Extra running time {loss:.1f} min against an 80 km/h path."
        ),
        "tsr": order,
        "system_state": SYSTEM_STATE,
    }


@app.post("/api/simulate-emergency")
def simulate_emergency(
    payload: dict[str, object] | None = Body(None),
) -> dict[str, object]:
    body = payload or {}
    clock = timetable.wrap_minute(float(body.get("minute") or 0.0))
    target, _solve_bundles, result = _solve_selected("ASSET_FIRST", topology.LINE_MID, "GHAT", int(clock))
    envelope = solver.calculate_transit_envelope(target)
    start = int(clock)
    if start + envelope > 1440:
        return {
            "event": "DYNAMIC_PREEMPTION_TRIGGERED",
            "dispatched_plan": {
                "feasible": False,
                "profile": "ASSET_FIRST",
                "status": "INFEASIBLE",
                "chainage_km": 111.4,
            },
            "message": "Not enough of the operating day is left to protect this fracture.",
            "system_state": SYSTEM_STATE,
        }
    end = start + envelope
    block = solver.ScheduledBlock(
        bundle_id=target.bundle_id,
        start_min=start,
        end_min=max(start + 1, end),
        machine_type=target.required_machine.value,
        power_sector_id=target.power_sector_id,
    )
    record = _execution_record(target, block, result, immediate=True)
    record["emergency_km"] = 111.4
    _lock_possession(
        record,
        "ASSET_FIRST",
        "COA-CR-EMG-1114",
        emergency=True,
        outcome="SANCTIONED",
    )
    rows = timetable.get_regulated_positions(clock, _active_possessions())
    execution = _phase_snapshot(clock, rows)
    feasible = result.status in ("OPTIMAL", "FEASIBLE")
    return {
        "event": "DYNAMIC_PREEMPTION_TRIGGERED",
        "dispatched_plan": {
            "feasible": feasible,
            "profile": "ASSET_FIRST",
            "status": result.status,
            "chainage_km": 111.4,
        },
        "possession": record,
        "execution": execution,
        "system_state": SYSTEM_STATE,
    }


def _history_bounds(limit: int, offset: int) -> tuple[int, int]:
    bounded_limit = 50 if limit < 1 else limit
    if bounded_limit > 200:
        bounded_limit = 200
    bounded_offset = offset if offset > 0 else 0
    return bounded_limit, bounded_offset


def _history_day(value: str | None, field_name: str) -> date | None:
    if value is None or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field_name} must be YYYY-MM-DD.") from None


@app.get("/api/history/possessions")
def history_possessions(
    line: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, object]:
    bounded_limit, bounded_offset = _history_bounds(limit, offset)
    rows = persistence.get_possession_history(
        line=line,
        start_date=_history_day(start_date, "start_date"),
        end_date=_history_day(end_date, "end_date"),
        limit=bounded_limit,
        offset=bounded_offset,
    )
    return {"limit": bounded_limit, "offset": bounded_offset, "count": len(rows), "rows": rows}


@app.get("/api/history/dossiers")
def history_dossiers(
    line: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, object]:
    bounded_limit, bounded_offset = _history_bounds(limit, offset)
    rows = persistence.get_dossier_history(
        line=line,
        start_date=_history_day(start_date, "start_date"),
        end_date=_history_day(end_date, "end_date"),
        limit=bounded_limit,
        offset=bounded_offset,
    )
    return {"limit": bounded_limit, "offset": bounded_offset, "count": len(rows), "rows": rows}


@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)