from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import math
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

import uvicorn
from fastapi import FastAPI, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

# Real SIH-26027 backend modules
import topology
import timetable
import defects
import ml_priority
import solver
import metrics
import calendar_plan
import backlog_state
import data_ingestion
import persistence
import block_windows
import sim_clock
import report_store
import report_service
from report_documents import DocumentLimits
from report_provider import ProviderNotConfiguredError, runtime_status
from report_service import ReportRequestError

_LOG = logging.getLogger(__name__)
_LEGACY_MIGRATION: dict[str, object] = {
    "status": "NOT_RUN",
    "archived_defect_ids": [],
    "kept_defect_ids": [],
    "blocked_defect_ids": [],
    "conflict": None,
    "message": "Legacy synthetic records have not been checked yet.",
}


async def _run_simulated_clock() -> None:
    """Advance and save the simulated clock about twice a second."""
    while True:
        try:
            sim_clock.advance()
            sim_clock.save_if_due()
            tick_block_lifecycle()
        except Exception:
            _LOG.exception("Simulated clock tick failed")
        await asyncio.sleep(0.5)


def _install_open_possession(loaded: dict[str, object]) -> None:
    """Put a possession loaded from the database back on this process's console."""
    payload = loaded.get("possession")
    if not isinstance(payload, dict):
        return
    SYSTEM_STATE["active_plan"] = str(loaded.get("plan_type") or SYSTEM_STATE["active_plan"])
    SYSTEM_STATE["sanctioned"] = True
    SYSTEM_STATE["sanctioned_by"] = loaded.get("sanctioned_by")
    SYSTEM_STATE["freight_held"] = True
    SYSTEM_STATE["emergency_active"] = bool(loaded.get("emergency"))
    SYSTEM_STATE["dossier_submitted"] = True
    SYSTEM_STATE["reject_reason"] = None
    SYSTEM_STATE["possession"] = payload
    paused = payload.get("paused_work")
    SYSTEM_STATE["paused_work"] = paused if isinstance(paused, dict) else None
    restored_id = payload.get("coa_dossier_id")
    SYSTEM_STATE["coa_dossier_id"] = restored_id if isinstance(restored_id, str) and restored_id else None
    raw_ids = payload.get("defect_ids")
    if isinstance(raw_ids, list):
        backlog_state.reserve_defects(str(item) for item in raw_ids if isinstance(item, str))


@asynccontextmanager
async def _lifespan(_application: FastAPI) -> AsyncIterator[None]:
    global _LEGACY_MIGRATION
    persistence.init_db()
    sim_clock.restore()
    loaded = persistence.load_open_possession()
    if loaded is not None:
        _install_open_possession(loaded)
    _LEGACY_MIGRATION = backlog_state.migrate_legacy_synthetic_open_records()
    task = asyncio.create_task(_run_simulated_clock())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        sim_clock.save()


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
_ROLE_GANG = "SITE_GANG"
_ROLE_OFFICE = "CONTROL_OFFICE"
_ROLE_TPC = "TRACTION_POWER_CONTROLLER"
_ROLE_STATION = "STATION_MASTER"
# One draw per block, stored on the possession. Negative reports early; positive is overrun.
REPORT_JITTER_MIN: tuple[int, int] = (-10, 20)
_MINUTES_PER_DAY = 1440

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
    "congestion_hold": None,
    "custom_weights": {"pass": 300, "frt": 2, "overdue": 50},
    "earliest_start_min": 0,
    "possession": None,
    "paused_work": None,
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
        "backlog_status": "SCHEDULED" if d.defect_id in backlog_state.scheduled_defect_ids() else "OPEN",
    }


_WINDOW_COLOR = {
    calendar_plan.LABEL_NIGHTLY: "#64748b",
    calendar_plan.LABEL_MEGA_BLOCK: "#38bdf8",
    calendar_plan.LABEL_EXTENDED: "#c084fc",
}


def _stamp_day(payload: dict[str, object], when: date | datetime | str) -> dict[str, object]:
    """Put the simulated day number, label, and week next to an ISO date."""
    payload.update(sim_clock.day_fields(when))
    return payload


def _hhmm_from_stamp(stamp: object) -> str | None:
    """HH:MM from a simulated ISO timestamp. The calendar date is not returned."""
    if not isinstance(stamp, str):
        return None
    marker = stamp.find("T")
    if marker < 0:
        return None
    clock = stamp[marker + 1 :]
    if len(clock) < 5:
        return None
    return clock[:5]


def _saved_minutes(value: int | float) -> int | float:
    number = float(value)
    if number.is_integer():
        return int(number)
    return round(number, 1)


def _blocks_from_completed(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """One entry per possession, built from the completed-maintenance rows."""
    groups: dict[tuple[object, ...], list[dict[str, object]]] = {}
    for record in records:
        key = (
            record.get("bundle_id"),
            record.get("start_min"),
            record.get("end_min"),
            record.get("window_label"),
            record.get("completed_at"),
        )
        groups.setdefault(key, []).append(record)
    blocks: list[dict[str, object]] = []
    for rows in groups.values():
        summary = metrics.summarize_completed_work(rows)
        start_raw = rows[0]["start_min"]
        end_raw = rows[0]["end_min"]
        start_min = int(start_raw) if isinstance(start_raw, (int, float)) else 0
        end_min = int(end_raw) if isinstance(end_raw, (int, float)) else 0
        saved_raw = summary["track_minutes_saved"]
        saved = _saved_minutes(saved_raw if isinstance(saved_raw, (int, float)) else 0)
        cleared_hhmm = _hhmm_from_stamp(rows[0].get("completed_at")) or timetable.minute_to_hhmm(end_min)
        blocks.append({
            "bundle": rows[0]["bundle_id"],
            "window_label": rows[0]["window_label"],
            "start_hhmm": timetable.minute_to_hhmm(start_min),
            "end_hhmm": cleared_hhmm,
            "cleared_hhmm": cleared_hhmm,
            "defects_cleared": len(rows),
            "track_minutes_saved": saved,
        })
    blocks.sort(key=lambda item: (str(item["start_hhmm"]), str(item["bundle"])))
    return blocks


def _daily_record(month_number: int) -> dict[str, object]:
    """Days 1-30 of one simulated month, including days with no maintenance."""
    records = backlog_state.get_completed_log(1000)
    buckets: dict[int, list[dict[str, object]]] = {day: [] for day in range(1, sim_clock.DAYS_PER_MONTH + 1)}
    for record in records:
        stamp = record.get("completed_at")
        if not isinstance(stamp, str) or not stamp:
            continue
        if sim_clock.month_number_of(stamp) != month_number:
            continue
        day_no = sim_clock.day_number_of(stamp)
        if day_no in buckets:
            buckets[day_no].append(record)

    weekly: dict[int, dict[str, object]] = {
        week: {"week_in_month": week, "blocks": 0, "defects_cleared": 0, "track_minutes_saved": 0}
        for week in (1, 2, 3, 4)
    }
    days_out: list[dict[str, object]] = []
    for day_no in range(1, sim_clock.DAYS_PER_MONTH + 1):
        absolute = (month_number - 1) * sim_clock.DAYS_PER_MONTH + (day_no - 1)
        when = sim_clock.date_for_absolute_day(absolute)
        blocks = _blocks_from_completed(buckets[day_no])
        defects_cleared = sum(int(block["defects_cleared"]) for block in blocks)
        track_saved = _saved_minutes(sum(float(block["track_minutes_saved"]) for block in blocks))
        totals = {
            "blocks": len(blocks),
            "defects_cleared": defects_cleared,
            "track_minutes_saved": track_saved,
        }
        days_out.append({
            **sim_clock.day_fields(when),
            "blocks": blocks,
            "totals": totals,
        })
        week = int(sim_clock.week_in_month_for(day_no))
        bucket = weekly[week]
        bucket["blocks"] = int(bucket["blocks"]) + len(blocks)
        bucket["defects_cleared"] = int(bucket["defects_cleared"]) + defects_cleared
        bucket["track_minutes_saved"] = _saved_minutes(
            float(bucket["track_minutes_saved"]) + float(track_saved)
        )
    return {
        "month_number": month_number,
        "days": days_out,
        "weekly_totals": [weekly[week] for week in (1, 2, 3, 4)],
    }


def _server_minute() -> float:
    """Advance the simulated clock and return its minute. Browser minutes are not used."""
    sim_clock.advance()
    return sim_clock.minute_of_day()


def adapt_day_plan(day: calendar_plan.DayPlan) -> dict[str, object]:
    booked = day.result is not None and day.result.status in ("OPTIMAL", "FEASIBLE")
    payload: dict[str, object] = {
        "date": day.calendar_date.isoformat(),
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
    return _stamp_day(payload, day.calendar_date)


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


def _open_caution_center_km(raw: object) -> float:
    """Chainage the console already uses when no block and no bundle exist."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 114.0
    center = float(raw)
    if not math.isfinite(center):
        return 114.0
    return min(192.0, max(0.0, center))


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
    latest_end: int | None = None,
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
            latest_end_min=latest_end,
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
    latest_end_min: int | None = None,
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
        latest_end_min=latest_end_min,
    )


def _planning_defects() -> list[defects.TrackDefect]:
    """Open defects that are not already reserved by the sanctioned block."""
    reserved = backlog_state.scheduled_defect_ids()
    return [item for item in data_ingestion.get_effective_defects() if item.defect_id not in reserved]


def _target_context(
    target_line: str | None,
    sector: str | None,
) -> tuple[defects.MaintenanceBundle, list[defects.MaintenanceBundle], tuple[timetable.TrainTrajectory, ...]]:
    raw_defects = _planning_defects()
    bundles = list(defects.cluster_defects_into_bundles(raw_defects))
    if not bundles:
        raise HTTPException(status_code=409, detail="Every open defect is already scheduled.")
    trains = timetable.get_all_trajectories()
    target = _select_bundle(bundles, target_line, sector)
    solve_bundles = [
        bundle
        for bundle in bundles
        if bundle.section_id == target.section_id and _line_matches(bundle.track_line, target.track_line)
    ] or [target]
    return target, solve_bundles, trains


_SLOT_CACHE: dict[str, object] = {"key": None, "slot": None}


def _planned_target_context(
    target_line: str | None,
    sector: str | None,
) -> tuple[
    defects.MaintenanceBundle,
    list[defects.MaintenanceBundle],
    tuple[timetable.TrainTrajectory, ...],
    block_windows.Slot | None,
]:
    raw_defects = _planning_defects()
    bundles = list(defects.cluster_defects_into_bundles(raw_defects))
    trains = timetable.get_all_trajectories()
    base_date, first_window = backlog_state.get_window_start()
    if not bundles:
        raise HTTPException(status_code=409, detail="Every open defect is already scheduled.")
    cache_key = (
        tuple(sorted(item.defect_id for item in raw_defects)),
        first_window,
        tuple(sorted(backlog_state.get_used_windows())),
        target_line,
        sector,
        base_date,
    )
    slot: block_windows.Slot | None
    if cache_key == _SLOT_CACHE["key"]:
        cached_slot = _SLOT_CACHE["slot"]
        slot = cached_slot if isinstance(cached_slot, block_windows.Slot) else None
    else:
        narrowed = list(bundles)
        if sector:
            allowed = _SECTOR_SECTIONS.get(sector.upper())
            if allowed:
                filtered = [bundle for bundle in narrowed if bundle.section_id in allowed]
                if filtered:
                    narrowed = filtered
        if target_line:
            filtered = [bundle for bundle in narrowed if _line_matches(bundle.track_line, target_line)]
            if filtered:
                narrowed = filtered
        slot = block_windows.find_next_slot(narrowed, trains, first_window, base_date)
        if slot is None and len(narrowed) < len(bundles):
            slot = block_windows.find_next_slot(bundles, trains, first_window, base_date)
        _SLOT_CACHE["key"] = cache_key
        _SLOT_CACHE["slot"] = slot
    if slot is None:
        target, solve_bundles, fallback_trains = _target_context(target_line, sector)
        return target, solve_bundles, fallback_trains, None
    return (
        slot.bundle,
        [slot.bundle],
        calendar_plan._trains_for_window(trains, slot.window),
        slot,
    )


def _slot_payload(slot: block_windows.Slot | None) -> dict[str, object] | None:
    if slot is None:
        return None
    return _stamp_day(
        {
            "bundle_id": slot.bundle.bundle_id,
            "plan_date": slot.plan_date.isoformat(),
            "window_label": slot.window.label,
            "window_index": slot.window_index,
            "window_start_hhmm": timetable.minute_to_hhmm(slot.window.start_min),
            "window_end_hhmm": timetable.minute_to_hhmm(slot.window.end_min),
        },
        slot.plan_date,
    )


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
        "defect_ids": [item.defect_id for item in bundle.defects],
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


def _stamp_absolute_times(record: dict[str, object]) -> None:
    """Store plan_day, start_abs, and end_abs on the possession itself.

    Immediate blocks use today's absolute day and the minute already on the
    record. A planned block uses the absolute day of its plan date.
    """
    start_min = int(record["start_min"])
    end_min = int(record["end_min"])
    if bool(record.get("immediate")):
        plan_day = sim_clock.absolute_day()
        record["plan_date"] = sim_clock.sim_date().isoformat()
    else:
        plan_date = record.get("plan_date")
        if isinstance(plan_date, str) and plan_date.strip():
            plan_day = sim_clock.absolute_day_of(plan_date)
        else:
            plan_day = sim_clock.absolute_day()
            start_today = plan_day * _MINUTES_PER_DAY + start_min
            if start_today <= sim_clock.absolute_minute():
                plan_day += 1
            record["plan_date"] = sim_clock.date_for_absolute_day(plan_day).isoformat()
    record["plan_day"] = plan_day
    record["start_abs"] = plan_day * _MINUTES_PER_DAY + start_min
    if end_min < start_min:
        record["end_abs"] = (plan_day + 1) * _MINUTES_PER_DAY + end_min
    else:
        record["end_abs"] = plan_day * _MINUTES_PER_DAY + end_min
    record["plan_day_label"] = sim_clock.short_label(sim_clock.date_for_absolute_day(plan_day))


def _fresh_order_id(emergency: bool) -> str:
    """A COA order id unique to this possession. It is not the bundle id."""
    kind = "EMG" if emergency else "BLK"
    minute = int(sim_clock.absolute_minute())
    serial = persistence.next_order_serial()
    return f"COA-CR-{kind}-{minute}-{serial}"


def _report_jitter(order_id: str) -> int:
    """One seeded minute offset in REPORT_JITTER_MIN. The same order id draws the same offset."""
    lo, hi = REPORT_JITTER_MIN
    seed = 0
    for character in order_id:
        seed = (seed * 131 + ord(character)) & 0xFFFFFFFF
    return random.Random(seed).randint(int(lo), int(hi))


def _ensure_possession_ref(record: dict[str, object], emergency: bool = False) -> str:
    """The possession ref is the COA order id, kept stable across a shift."""
    existing = record.get("possession_ref")
    if isinstance(existing, str) and existing.startswith("COA-CR-"):
        record["coa_dossier_id"] = existing
        return existing
    order = _fresh_order_id(emergency)
    record["possession_ref"] = order
    record["coa_dossier_id"] = order
    return order


def _ensure_report_jitter(record: dict[str, object]) -> int:
    """Store the block's one jitter. A later read, including after restart, uses that value."""
    stored = record.get("report_jitter_min")
    if isinstance(stored, bool):
        stored = None
    if isinstance(stored, (int, float)):
        jitter = int(stored)
        record["report_jitter_min"] = jitter
        return jitter
    jitter = _report_jitter(str(record.get("possession_ref") or ""))
    record["report_jitter_min"] = jitter
    return jitter


def _mark_abs(record: dict[str, object], minute_value: float) -> float:
    """Timeline minute as an absolute simulated minute on the plan day."""
    plan_day = int(record["plan_day"])
    start_min = float(record["start_min"])
    mark = float(minute_value)
    if mark + 1e-3 < start_min:
        mark += float(_MINUTES_PER_DAY)
    return plan_day * _MINUTES_PER_DAY + mark


def _regulation_window(record: dict[str, object]) -> tuple[float, float] | None:
    """Minute-of-day window for today's timetable, or nothing while the block is still scheduled.

    After the planned end the line stays closed, so the window runs to the end
    of the current simulated day.
    """
    now = sim_clock.absolute_minute()
    start_abs = float(record["start_abs"])
    end_abs = float(record["end_abs"])
    if now + 1e-9 < start_abs:
        return None
    today = sim_clock.absolute_day()
    day_lo = float(today * _MINUTES_PER_DAY)
    day_hi = day_lo + float(_MINUTES_PER_DAY)
    if bool(record.get("emergency")) or bool(record.get("stop_approaching")):
        close_hi = day_hi
    else:
        close_hi = day_hi if now + 1e-9 >= end_abs else end_abs
    lo = max(start_abs, day_lo)
    hi = min(close_hi, day_hi)
    if hi <= lo + 1e-9:
        return None
    return lo - day_lo, hi - day_lo


def _machines_phrase(record: dict[str, object]) -> str:
    specs = record.get("machines")
    names: list[str] = []
    if isinstance(specs, list):
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            name = str(spec.get("machine_type") or "")
            if name and name != "NONE" and name not in names:
                names.append(name)
    if not names:
        return "none"
    return ", ".join(names)


def _annotate_request(record: dict[str, object], bundle: defects.MaintenanceBundle) -> None:
    record["staff_count"] = sum(_crew_headcount(item) for item in bundle.defects)
    record["machines_label"] = _machines_phrase(record)


def _plan_day_text(record: dict[str, object]) -> str:
    label = record.get("plan_day_label")
    if isinstance(label, str) and label:
        return label
    plan_day = record.get("plan_day")
    if isinstance(plan_day, int):
        return sim_clock.short_label(sim_clock.date_for_absolute_day(plan_day))
    return sim_clock.short_label()


def _transmit_text(record: dict[str, object]) -> str:
    lo = float(record["worksite_start_km"])
    hi = float(record["worksite_end_km"])
    label = str(record.get("window_label") or "BLOCK")
    staff = int(record.get("staff_count") or 0)
    machines = str(record.get("machines_label") or _machines_phrase(record))
    return (
        f"Block request {record['bundle_id']} {record['track_line']} KM {lo:.1f}-{hi:.1f}, "
        f"window {label} {record['start_hhmm']}-{record['end_hhmm']} {_plan_day_text(record)}, "
        f"{staff} staff, {machines}"
    )


def _sanction_text(record: dict[str, object]) -> str:
    order = str(record.get("coa_dossier_id") or record.get("possession_ref") or "")
    return (
        f"Block sanctioned. Order {order}. "
        f"Line {record['track_line']} closes {_plan_day_text(record)} {record['start_hhmm']}"
    )


def _is_power_block(record: dict[str, object]) -> bool:
    return str(record.get("block_type") or "") == defects.BlockType.POWER_AND_TRAFFIC.value


def _section_end_stations(record: dict[str, object]) -> tuple[topology.StationNode, topology.StationNode]:
    """The stations at the two ends of the block section that holds this worksite."""
    lo = float(record.get("worksite_start_km") or record.get("closed_start_km") or 0.0)
    hi = float(record.get("worksite_end_km") or record.get("closed_end_km") or lo)
    lo, hi = min(lo, hi), max(lo, hi)
    stations = tuple(sorted(topology.STATIONS, key=lambda station: (station.km, station.code)))
    near_start = stations[0]
    for station in stations:
        if station.km <= lo + 1e-6:
            near_start = station
        else:
            break
    near_end = stations[-1]
    for station in stations:
        if station.km + 1e-6 >= hi:
            near_end = station
            break
    if near_start.code == near_end.code:
        index = stations.index(near_start)
        if index + 1 < len(stations):
            near_end = stations[index + 1]
        elif index > 0:
            near_start = stations[index - 1]
    return near_start, near_end


def _station_master_role(record: dict[str, object]) -> str:
    start_station, end_station = _section_end_stations(record)
    return f"{_ROLE_STATION} {start_station.code}, {_ROLE_STATION} {end_station.code}"


def _demob_minutes(timeline: dict[str, object]) -> float:
    transit = timeline.get("transit_min")
    if isinstance(transit, (int, float)) and not isinstance(transit, bool):
        return max(0.0, float(transit))
    return 0.0


def _report_abs(record: dict[str, object]) -> float:
    return float(record["end_abs"]) + float(_ensure_report_jitter(record))


def _post_line_closed(record: dict[str, object]) -> None:
    ref = str(record["possession_ref"])
    persistence.post_block_message(
        ref,
        "LINE_CLOSED",
        _CONSOLE_CONTROLLER,
        _ROLE_OFFICE,
        "Line closed. Trains regulated.",
        sequence=0,
    )
    persistence.post_block_message(
        ref,
        "LINE_CLOSED",
        _CONSOLE_CONTROLLER,
        _station_master_role(record),
        "Line closed. Trains regulated.",
        sequence=1,
    )


def _post_line_cleared(record: dict[str, object]) -> None:
    ref = str(record["possession_ref"])
    persistence.post_block_message(
        ref,
        "LINE_CLEARED",
        _CONSOLE_CONTROLLER,
        _ROLE_OFFICE,
        "Line cleared. Normal working resumed.",
        sequence=0,
    )
    persistence.post_block_message(
        ref,
        "LINE_CLEARED",
        _CONSOLE_CONTROLLER,
        _station_master_role(record),
        "Line cleared. Normal working resumed.",
        sequence=1,
    )
    if _is_power_block(record):
        persistence.post_block_message(
            ref,
            "POWER_RESTORED",
            _ROLE_TPC,
            _CONSOLE_CONTROLLER,
            "Power restored",
        )


def _post_due_messages(record: dict[str, object], timeline: dict[str, object]) -> None:
    """Post each lifecycle line once, and only once its simulated minute has arrived."""
    ref = str(record["possession_ref"])
    now = sim_clock.absolute_minute()
    start_abs = float(record["start_abs"])
    withdrawing = bool(record.get("withdraw_pending"))
    power = _is_power_block(record)
    persistence.post_block_message(
        ref,
        "TRANSMIT",
        _CONSOLE_OFFICER,
        _CONSOLE_CONTROLLER,
        _transmit_text(record),
    )
    persistence.post_block_message(
        ref,
        "SANCTION",
        _CONSOLE_CONTROLLER,
        f"{_CONSOLE_OFFICER}, {_ROLE_GANG}, {_ROLE_OFFICE}",
        _sanction_text(record),
    )
    if now + 1e-6 < start_abs:
        return
    _post_line_closed(record)
    if power:
        persistence.post_block_message(
            ref,
            "POWER_GRANTED",
            _ROLE_TPC,
            _ROLE_GANG,
            f"Power block granted, feeder isolated, permit to work {ref}",
        )
    arrive_abs = _mark_abs(record, float(timeline["arrive_min"]))
    if now + 1e-6 < arrive_abs and not withdrawing:
        return
    if now + 1e-6 >= arrive_abs:
        persistence.post_block_message(
            ref,
            "GANG_ON_SITE",
            _ROLE_GANG,
            _CONSOLE_CONTROLLER,
            "Gang and machines at site",
        )
    ready_abs = _mark_abs(record, float(timeline["protection_ready_min"]))
    if power and now + 1e-6 >= ready_abs and not withdrawing:
        persistence.post_block_message(
            ref,
            "PROTECTION_READY",
            _ROLE_GANG,
            _CONSOLE_CONTROLLER,
            "Protection and earthing complete, work starting",
        )
        persistence.post_block_message(
            ref,
            "EARTHING_APPLIED",
            _ROLE_GANG,
            _ROLE_TPC,
            "Earthing applied",
        )
    elif power and now + 1e-6 >= ready_abs and withdrawing:
        # Earthing was applied only if protection was already reached when work stopped.
        if float(record.get("withdraw_requested_abs") or now) + 1e-6 >= ready_abs:
            persistence.post_block_message(
                ref,
                "EARTHING_APPLIED",
                _ROLE_GANG,
                _ROLE_TPC,
                "Earthing applied",
            )
            persistence.post_block_message(
                ref,
                "PROTECTION_READY",
                _ROLE_GANG,
                _CONSOLE_CONTROLLER,
                "Protection and earthing complete, work starting",
            )
    if withdrawing:
        demob_ready = float(record.get("demob_ready_abs") or now)
        if now + 1e-6 >= demob_ready:
            persistence.post_block_message(
                ref,
                "WORK_STOPPED",
                _ROLE_GANG,
                _CONSOLE_CONTROLLER,
                "Work stopped. Track restored. Staff and machines clear",
            )
        return
    if now + 1e-6 >= _report_abs(record):
        if power:
            persistence.post_block_message(
                ref,
                "EARTHING_REMOVED",
                _ROLE_GANG,
                _ROLE_TPC,
                "Earthing removed, line clear, request restoration",
            )
        persistence.post_block_message(
            ref,
            "WORK_COMPLETE",
            _ROLE_GANG,
            _CONSOLE_CONTROLLER,
            "Work complete. All staff and machines are clear of the line. Request line clearance.",
        )


def tick_block_lifecycle() -> None:
    """Post board messages whose simulated moment has been reached. Safe to call often."""
    raw = SYSTEM_STATE.get("possession")
    if not SYSTEM_STATE.get("sanctioned") or not isinstance(raw, dict):
        return
    if "start_abs" not in raw or "end_abs" not in raw or "plan_day" not in raw:
        _stamp_absolute_times(raw)
    if "machines_label" not in raw:
        raw["machines_label"] = _machines_phrase(raw)
    if "staff_count" not in raw:
        raw["staff_count"] = 0
    if not raw.get("window_label"):
        raw["window_label"] = "EMERGENCY" if bool(SYSTEM_STATE.get("emergency_active")) else (
            "IMMEDIATE" if bool(raw.get("immediate")) else "BLOCK"
        )
    _ensure_possession_ref(raw, emergency=bool(SYSTEM_STATE.get("emergency_active")))
    _ensure_report_jitter(raw)
    timeline = raw.get("timeline")
    if not isinstance(timeline, dict):
        return
    _post_due_messages(raw, timeline)


def _active_possessions() -> list[timetable.LinePossession]:
    """The closed line for the timetable, only after the absolute start and until clearance.

    A block planned for a later day is omitted. The window passed onward is
    clamped to the current simulated day.
    """
    raw = SYSTEM_STATE.get("possession")
    if not SYSTEM_STATE.get("sanctioned") or not isinstance(raw, dict):
        return []
    if "start_abs" not in raw or "end_abs" not in raw:
        _stamp_absolute_times(raw)
    window = _regulation_window(raw)
    if window is None:
        return []
    start_min, end_min = window
    lines = raw.get("protected_lines") or [raw["track_line"]]
    return [
        timetable.LinePossession(
            possession_id=str(raw["bundle_id"]),
            protected_lines=tuple(str(line) for line in lines),
            start_km=float(raw["closed_start_km"]),
            end_km=float(raw["closed_end_km"]),
            start_min=float(start_min),
            end_min=float(end_min),
            approach_km=float(raw["approach_km"]),
            stop_approaching=bool(raw.get("stop_approaching")),
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
    if end < start:
        end += float(_MINUTES_PER_DAY)
    if arrive < start:
        arrive += float(_MINUTES_PER_DAY)
    if returning < start:
        returning += float(_MINUTES_PER_DAY)
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
    """Phase of the sanctioned block against absolute simulated time.

    The minute argument is ignored. Before start_abs the block is SCHEDULED
    and the line is open. After end_abs the line stays closed until clearance.
    """
    _ = minute
    tick_block_lifecycle()
    raw = SYSTEM_STATE.get("possession")
    if not SYSTEM_STATE.get("sanctioned") or not isinstance(raw, dict):
        return None
    if "start_abs" not in raw or "end_abs" not in raw or "plan_day" not in raw:
        _stamp_absolute_times(raw)
    now = sim_clock.absolute_minute()
    clock = sim_clock.minute_of_day()
    start_abs = float(raw["start_abs"])
    end_abs = float(raw["end_abs"])
    plan_day = int(raw["plan_day"])
    local = now - plan_day * _MINUTES_PER_DAY
    timeline = raw["timeline"]
    assert isinstance(timeline, dict)
    arrive_abs = _mark_abs(raw, float(timeline["arrive_min"]))
    ready_abs = _mark_abs(raw, float(timeline["protection_ready_min"]))
    work_end_abs = _mark_abs(raw, float(timeline["work_end_min"]))
    power = bool(raw["earth_required"])
    ref = str(raw.get("possession_ref") or "")
    withdrawing = bool(raw.get("withdraw_pending"))
    reported = bool(ref) and persistence.has_block_message(ref, "WORK_COMPLETE")
    stopped = bool(ref) and persistence.has_block_message(ref, "WORK_STOPPED")
    clear_ready = stopped if withdrawing else reported
    line_closed = now + 1e-9 >= start_abs
    demob_ready = float(raw.get("demob_ready_abs") or end_abs)
    moving = (start_abs <= now < demob_ready) if withdrawing else (start_abs <= now < end_abs)
    arrived = now + 1e-9 >= arrive_abs
    ready = now + 1e-9 >= ready_abs
    work_finished = withdrawing or now + 1e-9 >= work_end_abs
    machines_on_site = moving and arrived and not work_finished and not withdrawing
    machines_returning = withdrawing or (moving and work_finished)
    # The feeder stays dead until the planned end. Earths come off when the
    # work stops, and the machine then rolls clear on a dead line.
    power_isolated = moving and power and arrived
    earth_on = moving and power and ready and not work_finished
    work_on = moving and ready and not work_finished and ((not power) or earth_on)
    if now + 1e-9 < start_abs:
        phase = "SCHEDULED"
    elif withdrawing:
        phase = "DEMOBILISING"
    elif reported:
        phase = "WORK_DONE_REPORTED"
    elif now + 1e-9 < arrive_abs:
        phase = "MOBILISING"
    elif now + 1e-9 < ready_abs:
        phase = "PROTECTION"
    elif now + 1e-9 < work_end_abs:
        phase = "WORKING"
    else:
        phase = "DEMOBILISING"
    starts_in = max(0.0, start_abs - now)
    overrun = max(0.0, now - end_abs) if line_closed and now + 1e-9 >= end_abs else 0.0
    span = max(1.0, end_abs - start_abs)
    progress = 0.0 if now < start_abs else min(1.0, (now - start_abs) / span)
    inside = _trains_inside(rows, raw) if line_closed else []
    machine_rows: list[dict[str, object]] = []
    specs = raw["machines"]
    assert isinstance(specs, list)
    for spec in specs:
        assert isinstance(spec, dict)
        km = _machine_km(local, spec, raw, moving)
        machine_rows.append(
            {
                "machine_type": spec["machine_type"],
                "unit_index": spec["unit_index"],
                "km": km,
                "line": raw["track_line"],
                "on_line": moving,
                "at_site": machines_on_site,
                "depot_km": spec["depot_km"],
                "speed_kmh": 0.0 if (not moving or machines_on_site) else spec["speed_kmh"],
            }
        )
    _separate_machine_kms(machine_rows)
    past_end = line_closed and now + 1e-9 >= end_abs
    repair = bool(raw.get("emergency"))
    steps = [
        _step(
            "possession",
            "Line protected and signals at danger" if repair else "Line taken and signals at red",
            _mark(line_closed),
        ),
        _step(
            "trains",
            "Approaching trains stopped short of the site" if repair else "Trains held clear of the closed line",
            _mark(line_closed and not inside, active=line_closed and bool(inside)),
        ),
        _step(
            "machines",
            "Machines on site and separated",
            _mark(machines_on_site or machines_returning or past_end or reported, active=phase == "MOBILISING"),
        ),
        _step(
            "power",
            "25 kV feeder isolated",
            _mark(power_isolated or (power and (work_finished or past_end or reported)), active=phase == "PROTECTION" and power, skipped=not power),
        ),
        _step(
            "earth_up",
            "Earth connected at the near end",
            _mark(earth_on or (power and (work_finished or past_end or reported)), active=phase == "PROTECTION" and power, skipped=not power),
        ),
        _step(
            "earth_down",
            "Earth connected at the far end",
            _mark(earth_on or (power and (work_finished or past_end or reported)), active=phase == "PROTECTION" and power, skipped=not power),
        ),
        _step(
            "work",
            "Emergency repair in progress" if repair else "Maintenance work in progress",
            _mark(work_finished or past_end or reported, active=work_on),
        ),
        _step(
            "restore",
            "Earth off, power restored, machines clear",
            _mark(reported or past_end, active=phase == "DEMOBILISING"),
        ),
    ]
    return {
        "phase": phase,
        "live": line_closed,
        "line_closed": line_closed,
        "minute": round(clock, 2),
        "hhmm": timetable.minute_to_hhmm(clock),
        "starts_in_min": round(starts_in, 1),
        "overrun_min": round(overrun, 1),
        "work_done_reported": reported,
        "withdraw_pending": withdrawing,
        "clear_ready": clear_ready,
        "progress": round(progress, 4),
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


def _stamp_clearance(record: dict[str, object]) -> None:
    """Write the simulated clearance instant and the overrun onto the possession payload."""
    now_abs = sim_clock.absolute_minute()
    end_abs = float(record["end_abs"]) if "end_abs" in record else now_abs
    actual_min = int(sim_clock.minute_of_day())
    record["actual_end_abs"] = round(now_abs, 3)
    record["actual_end_min"] = actual_min
    record["actual_end_hhmm"] = timetable.minute_to_hhmm(float(actual_min))
    record["overrun_min"] = round(max(0.0, now_abs - end_abs), 1)


def _complete_possession_defects() -> int:
    """Handing the line back means the work is done: log and remove those defects.

    completed_at is the simulated clearance time. The logged end minute is the
    clearance minute when the possession recorded one.
    """
    raw = SYSTEM_STATE.get("possession")
    if not SYSTEM_STATE.get("sanctioned") or not isinstance(raw, dict):
        return 0
    window_index = raw.get("window_index")
    if window_index is not None:
        backlog_state.mark_window_used(int(window_index))
    ids = set(raw.get("defect_ids") or [])
    if not ids:
        return 0
    items = [d for d in data_ingestion.get_effective_defects() if d.defect_id in ids]
    start_min = int(raw["start_min"])
    actual_end = raw.get("actual_end_min")
    end_min = int(actual_end) if isinstance(actual_end, int) else int(raw["end_min"])
    emergency = bool(SYSTEM_STATE.get("emergency_active"))
    saved_label = raw.get("window_label")
    if not emergency and isinstance(saved_label, str) and saved_label:
        window_label = saved_label
    else:
        window_label = backlog_state.window_label_for(start_min, emergency)
    return backlog_state.complete_defects(
        items,
        str(raw["bundle_id"]),
        start_min,
        end_min,
        window_label,
        _CONSOLE_CONTROLLER,
    )


def _release_possession_defects(raw: dict[str, object]) -> None:
    ids = raw.get("defect_ids") or []
    if isinstance(ids, list):
        backlog_state.release_defects(str(item) for item in ids if isinstance(item, str))


def _clear_possession(
    outcome: str = "CLEARED",
    actor: str | None = None,
    reject_reason: str | None = None,
) -> None:
    raw = SYSTEM_STATE.get("possession")
    if SYSTEM_STATE.get("sanctioned") and isinstance(raw, dict):
        _release_possession_defects(raw)
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


def _impose_incomplete_caution(record: dict[str, object]) -> None:
    """30 km/h caution after work had started and was then withdrawn."""
    start_km = float(record["worksite_start_km"])
    end_km = float(record["worksite_end_km"])
    line = str(record["track_line"])
    cap = 30.0
    loss = metrics.tsr_time_loss_min(abs(end_km - start_km), 80.0, cap)
    persistence.record_tsr_order(
        line=line,
        start_km=start_km,
        end_km=end_km,
        max_kmh=cap,
        extra_minutes=float(loss),
        issued_by=_CONSOLE_CONTROLLER,
    )
    SYSTEM_STATE["tsr"] = {
        "line": line,
        "start_km": start_km,
        "end_km": end_km,
        "max_kmh": cap,
        "extra_minutes": loss,
    }
    persistence.post_block_message(
        str(record["possession_ref"]),
        "CAUTION",
        _CONSOLE_CONTROLLER,
        _ROLE_OFFICE,
        "Caution 30 km/h imposed after incomplete work",
    )


def _lock_possession(
    record: dict[str, object],
    plan_type: str,
    dossier_id: str,
    emergency: bool,
    outcome: str = "SANCTIONED",
) -> None:
    previous = SYSTEM_STATE.get("possession")
    if SYSTEM_STATE.get("sanctioned") and isinstance(previous, dict) and previous is not record:
        old_ids = previous.get("defect_ids") or []
        new_ids = {str(item) for item in (record.get("defect_ids") or []) if isinstance(item, str)}
        if isinstance(old_ids, list):
            backlog_state.release_defects(
                str(item) for item in old_ids if isinstance(item, str) and str(item) not in new_ids
            )
    _stamp_absolute_times(record)
    if "staff_count" not in record:
        record["staff_count"] = 0
    if "machines_label" not in record:
        record["machines_label"] = _machines_phrase(record)
    if not record.get("window_label"):
        record["window_label"] = "EMERGENCY" if emergency else ("IMMEDIATE" if record.get("immediate") else "BLOCK")
    _ = dossier_id
    order = _ensure_possession_ref(record, emergency=emergency)
    _ensure_report_jitter(record)
    record["coa_dossier_id"] = order
    if isinstance(record.get("plan_date"), str):
        _stamp_day(record, str(record["plan_date"]))
    _record_possession(record, plan_type, _CONSOLE_CONTROLLER, emergency, outcome, None)
    ids = record.get("defect_ids") or []
    if isinstance(ids, list):
        backlog_state.reserve_defects(str(item) for item in ids if isinstance(item, str))
    SYSTEM_STATE["active_plan"] = plan_type
    SYSTEM_STATE["sanctioned"] = True
    SYSTEM_STATE["sanctioned_by"] = _CONSOLE_CONTROLLER
    SYSTEM_STATE["freight_held"] = True
    SYSTEM_STATE["dossier_submitted"] = True
    SYSTEM_STATE["reject_reason"] = None
    SYSTEM_STATE["congestion_hold"] = None
    SYSTEM_STATE["coa_dossier_id"] = order
    SYSTEM_STATE["possession"] = record
    SYSTEM_STATE["emergency_active"] = emergency
    if not emergency:
        SYSTEM_STATE["paused_work"] = None
    tick_block_lifecycle()


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

    Accepted rows are returned in this response. They are not written to the
    active backlog. /api/master-data, /api/optimize, and /api/horizon-plan
    read the persisted report-derived backlog only.
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
    accepted = len(summary.all_defects())
    if accepted:
        message = (
            f"{accepted} structured row(s) were parsed. "
            "The active backlog was not changed. Apply a maintenance report to add defects."
        )
    else:
        message = "No structured rows were accepted. The active backlog was not changed."
    return {
        "status": "FALLBACK" if summary.used_fallback else "INGESTED",
        "message": message,
        **summary.to_dict(),
    }


def _report_error(exc: ReportRequestError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message})


async def _read_limited_upload(file: UploadFile) -> bytes:
    limit = DocumentLimits().max_bytes
    chunks: list[bytes] = []
    size = 0
    while True:
        block = await file.read(1024 * 1024)
        if not block:
            break
        size += len(block)
        if size > limit:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "FILE_TOO_LARGE",
                    "message": "The file is larger than 15 MB. Nothing was added to the backlog.",
                },
            )
        chunks.append(block)
    return b"".join(chunks)


@app.get("/api/ingest/report/status")
def report_provider_status() -> dict[str, object]:
    """Say whether a live model, OCR, or the professor NLP module is available."""
    return runtime_status()


@app.get("/api/ingest/report/{report_id}")
def get_stored_report(report_id: str) -> dict[str, object]:
    """Reopen a stored preview. Applied candidates stay marked. Nothing is written."""
    try:
        return report_service.reopen_stored_report(report_id)
    except ReportRequestError as exc:
        raise _report_error(exc) from exc


@app.post("/api/ingest/report/preview")
async def preview_maintenance_report(file: UploadFile = File(...)) -> dict[str, object]:
    """Read one maintenance report and store a pending preview. The backlog is not changed."""
    report_store.ensure_report_tables()
    data = await _read_limited_upload(file)
    try:
        return report_service.preview_uploaded_report(
            data,
            file.filename or "",
            possession_active=bool(SYSTEM_STATE.get("sanctioned")),
        )
    except ProviderNotConfiguredError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "PROVIDER_UNAVAILABLE",
                "message": f"{exc} Nothing was added to the backlog.",
            },
        ) from exc
    except ReportRequestError as exc:
        raise _report_error(exc) from exc


@app.post("/api/ingest/report/{report_id}/apply")
def apply_maintenance_report(report_id: str, payload: dict[str, object] | None = Body(None)) -> dict[str, object]:
    """Append selected stored candidates, or replace when that operation is explicit."""
    try:
        return report_service.apply_stored_report(
            report_id,
            payload or {},
            possession_active=bool(SYSTEM_STATE.get("sanctioned")),
        )
    except ReportRequestError as exc:
        raise _report_error(exc) from exc


@app.get("/api/defects/{defect_id}/evidence")
def get_defect_evidence(defect_id: str) -> dict[str, object]:
    """Read the report evidence linked to one operational defect. Nothing is written."""
    wanted = str(defect_id).strip()
    rows = report_store.evidence_for_defect(wanted) if wanted else []
    known = any(item.defect_id == wanted for item in data_ingestion.get_effective_defects())
    if not wanted or (not known and not rows):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "EVIDENCE_NOT_FOUND",
                "message": "No linked report evidence exists for that defect. Nothing was changed.",
            },
        )
    return {"defect_id": wanted, "evidence": rows}


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
        "clock": _clock_payload(),
        "system_state": SYSTEM_STATE,
        "backlog": _backlog_summary(),
        "applied_reports": report_store.applied_report_summaries(),
        "legacy_migration": _LEGACY_MIGRATION,
    }


def _clock_payload() -> dict[str, object]:
    """The simulated clock, plus the length of one operating day."""
    sim_clock.advance()
    payload = sim_clock.snapshot()
    payload["day_minutes"] = timetable.TOTAL_SIMULATION_MINUTES
    return payload


def _backlog_summary() -> dict[str, int]:
    return {
        "cycle": backlog_state.current_cycle(),
        "open": len(data_ingestion.get_effective_defects()),
        "completed": len(backlog_state.get_completed_log(1000)),
    }


@app.post("/api/backlog/next-cycle")
def backlog_next_cycle() -> dict[str, object]:
    sim_clock.advance()
    cycle = backlog_state.next_maintenance_cycle()
    return {**cycle, "backlog": _backlog_summary()}


@app.post("/api/backlog/reset-demo")
def backlog_reset_demo() -> dict[str, object]:
    """The synthetic demo set is retired. This route does not change the open backlog."""
    retired = backlog_state.reset_to_demo_backlog()
    return {**retired, "backlog": _backlog_summary(), "clock": sim_clock.snapshot()}


@app.get("/api/clock")
def get_clock() -> dict[str, object]:
    """The simulated clock. This is the only request that marks a page as watching."""
    sim_clock.advance()
    sim_clock.note_poll()
    return sim_clock.snapshot()


@app.post("/api/clock/speed")
def set_clock_speed(payload: dict[str, object] | None = Body(None)) -> dict[str, object]:
    sim_clock.advance()
    body = payload or {}
    raw = body.get("multiplier")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or int(raw) != raw:
        raise HTTPException(status_code=400, detail="multiplier must be 1, 5, or 10.")
    speed = int(raw)
    if speed not in sim_clock.SPEED_OPTIONS:
        raise HTTPException(status_code=400, detail="multiplier must be 1, 5, or 10.")
    sim_clock.set_multiplier(speed)
    return sim_clock.snapshot()


@app.get("/api/daily-record")
def get_daily_record(month: int | None = None) -> dict[str, object]:
    """Days 1-30 of a simulated month, built from completed-maintenance timestamps."""
    sim_clock.advance()
    month_number = sim_clock.month_number() if month is None else int(month)
    if month_number < 1:
        raise HTTPException(status_code=400, detail="month must be a positive integer.")
    return _daily_record(month_number)


@app.get("/api/completed-log")
def completed_log(limit: int = 50) -> list[dict[str, object]]:
    return backlog_state.get_completed_log(limit)


@app.get("/api/positions")
def get_positions(minute: float = 240.0) -> dict[str, object]:
    """Live snapshot. The minute query is accepted and ignored; the server clock is used."""
    _ = minute
    clock = timetable.wrap_minute(_server_minute())
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
    sim_clock.advance()
    pass_weight = pass_weight if pass_weight is not None else SYSTEM_STATE["custom_weights"]["pass"]
    overdue_weight = overdue_weight if overdue_weight is not None else SYSTEM_STATE["custom_weights"]["overdue"]
    SYSTEM_STATE["custom_weights"]["pass"] = int(pass_weight)
    SYSTEM_STATE["custom_weights"]["overdue"] = int(overdue_weight)

    # An empty backlog is not the same case as open work that is already reserved.
    if not data_ingestion.get_effective_defects():
        return {
            "status": "NO_DEFECTS",
            "target_bundle": None,
            "candidate_options": [],
            "freight_forecast_range": {"optimistic": 0, "expected": 0, "pessimistic": 0},
            "system_state": SYSTEM_STATE,
        }

    target_bundle, solve_bundles, _planned_trains, slot = _planned_target_context(target_line, sector)

    if not solve_bundles:
        return {
            "status": "NO_DEFECTS",
            "target_bundle": None,
            "candidate_options": [],
            "freight_forecast_range": {"optimistic": 0, "expected": 0, "pessimistic": 0},
            "system_state": SYSTEM_STATE,
        }

    # Three different windows for this one bundle. Profile ids stay in the
    # same order so the existing cards and the sanction call still line up.
    profiles = ("PUNCTUALITY_FIRST", "ASSET_FIRST", "MEGA_BLOCK")
    base_date, first_index = backlog_state.get_window_start()
    all_trains = timetable.get_all_trajectories()
    option_slots = block_windows.find_option_slots(
        target_bundle,
        all_trains,
        first_index,
        base_date,
        used=backlog_state.get_used_windows(),
        count=len(profiles),
    )
    bundling_proof = _spatial_bundling_payload()

    candidate_options: list[dict[str, object]] = []
    solved_slots: list[block_windows.Slot] = []
    for option_slot in option_slots:
        if len(candidate_options) >= len(profiles):
            break
        profile_name = profiles[len(candidate_options)]
        day_trains = calendar_plan._trains_for_window(all_trains, option_slot.window)
        result = _solve_bundles_for(
            [target_bundle],
            day_trains,
            profile_name,
            int(pass_weight),
            int(overdue_weight),
            option_slot.window.start_min,
            time_limit_sec=3.0,
            latest_end_min=option_slot.window.end_min,
        )
        block = _block_for(target_bundle, result)
        if result.status not in ("OPTIMAL", "FEASIBLE") or block is None:
            continue

        window_min = int(block.end_min - block.start_min)
        schedule_metrics = _schedule_metrics_payload((target_bundle,), result)
        schedule_metrics.update(bundling_proof)
        train_impacts = [
            {"train_id": dt.train_id, "delay_minutes": dt.delay_min}
            for dt in result.delayed_trains
        ]
        candidate_options.append(_stamp_day({
            "profile": profile_name,
            "solver_status": result.status,
            "start_time_str": timetable.minute_to_hhmm(block.start_min),
            "end_time_str": timetable.minute_to_hhmm(block.end_min),
            "net_work_duration_min": window_min,
            "total_window_required_min": window_min,
            "confidence_score": _confidence_from_solve(result),
            "train_impacts": train_impacts,
            "downtime_reduction_pct": bundling_proof["bundling_gain_pct"],
            "metrics": schedule_metrics,
            "window_index": option_slot.window_index,
            "window_label": option_slot.window.label,
            "plan_date": option_slot.plan_date.isoformat(),
            "window_start_hhmm": timetable.minute_to_hhmm(option_slot.window.start_min),
            "window_end_hhmm": timetable.minute_to_hhmm(option_slot.window.end_min),
        }, option_slot.plan_date))
        solved_slots.append(option_slot)

    best_option = _best_solved_option(candidate_options)
    forecast_slot = solved_slots[0] if solved_slots else slot
    if best_option is not None:
        wanted_index = best_option.get("window_index")
        matched = next((item for item in solved_slots if item.window_index == wanted_index), None)
        if matched is not None:
            forecast_slot = matched
    if forecast_slot is not None:
        forecast_trains = calendar_plan._trains_for_window(all_trains, forecast_slot.window)
        forecast_earliest = forecast_slot.window.start_min
        forecast_latest: int | None = forecast_slot.window.end_min
        forecast_bundles = [target_bundle]
    else:
        forecast_trains = all_trains
        forecast_earliest = _earliest()
        forecast_latest = None
        forecast_bundles = solve_bundles

    return {
        "status": "SUCCESS" if candidate_options else "INFEASIBLE",
        "target_bundle": _console_bundle(target_bundle),
        "candidate_options": candidate_options,
        "freight_forecast_range": _freight_forecast_range(
            candidate_options,
            forecast_bundles,
            forecast_trains,
            int(pass_weight),
            int(overdue_weight),
            forecast_earliest,
            forecast_latest,
        ),
        "system_state": SYSTEM_STATE,
        "planned_slot": _slot_payload(slot),
    }


@app.get("/api/horizon-plan")
def get_horizon_plan(days: int = 7, start_date: str | None = None) -> dict[str, object]:
    """
    days=7  -> weekly view
    days=28 -> monthly view (frontend chunks the 28 days into 4 weeks)
    start_date -> "YYYY-MM-DD"; defaults to the simulated day if omitted.
    """
    sim_clock.advance()
    start = (
        datetime.strptime(start_date, "%Y-%m-%d").date()
        if start_date
        else sim_clock.sim_date()
    )
    days = max(1, min(28, int(days)))

    all_bundles = list(defects.cluster_defects_into_bundles(_planning_defects()))
    plan = calendar_plan.build_horizon_plan(all_bundles, start, days)

    day_payloads = [adapt_day_plan(d) for d in plan.days]
    weeks = [day_payloads[i : i + 7] for i in range(0, len(day_payloads), 7)]

    return _stamp_day({
        "start_date": start.isoformat(),
        "num_days": days,
        "days": day_payloads,
        "weeks": weeks,
        "unscheduled_bundle_ids": list(plan.unscheduled_bundle_ids),
        "total_bundles": len(all_bundles),
        "scheduled_bundles": len(all_bundles) - len(plan.unscheduled_bundle_ids),
    }, start)


@app.post("/api/officer/submit")
def officer_submit(
    payload: dict[str, object] | None = Body(None),
) -> dict[str, object]:
    body = payload or {}
    sim_clock.advance()
    plan_type = str(body.get("plan_type") or SYSTEM_STATE["active_plan"])
    if (
        bool(SYSTEM_STATE.get("dossier_submitted"))
        and not bool(SYSTEM_STATE.get("sanctioned"))
        and not SYSTEM_STATE.get("reject_reason")
    ):
        return {
            "status": "ALREADY_SUBMITTED",
            "message": "A dossier is already with the controller.",
            "plan_type": str(SYSTEM_STATE.get("active_plan") or plan_type),
            "system_state": SYSTEM_STATE,
        }
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
    SYSTEM_STATE["congestion_hold"] = None
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
    planned: bool = False,
    bound_to_window: bool = False,
) -> tuple[defects.MaintenanceBundle, list[defects.MaintenanceBundle], solver.SolverResult]:
    pass_weight, overdue_weight = _weights()
    latest_end_min: int | None = None
    if planned:
        target, solve_bundles, trains, slot = _planned_target_context(target_line, sector)
        if bound_to_window and slot is not None:
            earliest_start_min = max(earliest_start_min, slot.window.start_min)
            latest_end_min = slot.window.end_min
        elif slot is not None:
            trains = timetable.get_all_trajectories()
    else:
        target, solve_bundles, trains = _target_context(target_line, sector)
    result = _solve_bundles_for(
        solve_bundles,
        trains,
        plan_type,
        pass_weight,
        overdue_weight,
        earliest_start_min,
        time_limit_sec=4.0,
        latest_end_min=latest_end_min,
    )
    return target, solve_bundles, result


def _block_for(target: defects.MaintenanceBundle, result: solver.SolverResult) -> solver.ScheduledBlock | None:
    block = next((item for item in result.blocks if item.bundle_id == target.bundle_id), None)
    if block is None and result.blocks:
        return result.blocks[0]
    return block


def _body_window_index(body: dict[str, object]) -> int | None:
    """The requested window, or nothing when the caller did not send one."""
    raw = body.get("window_index")
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str) and raw.strip():
        return int(raw.strip())
    return None


def _solve_target_in_window(
    plan_type: str,
    target_line: str | None,
    sector: str | None,
    window_index: int,
) -> tuple[defects.MaintenanceBundle, solver.SolverResult, dict[str, object]]:
    """Solve the current target bundle inside the window the controller picked."""
    target, _solve_bundles, _trains, _slot = _planned_target_context(target_line, sector)
    base_date, _first = backlog_state.get_window_start()
    plan_date, window = block_windows.window_for_index(base_date, window_index)
    day_trains = calendar_plan._trains_for_window(timetable.get_all_trajectories(), window)
    pass_weight, overdue_weight = _weights()
    result = _solve_bundles_for(
        [target],
        day_trains,
        plan_type,
        pass_weight,
        overdue_weight,
        max(_earliest(), window.start_min),
        time_limit_sec=4.0,
        latest_end_min=window.end_min,
    )
    payload = _stamp_day({
        "bundle_id": target.bundle_id,
        "plan_date": plan_date.isoformat(),
        "window_label": window.label,
        "window_index": window_index,
        "window_start_hhmm": timetable.minute_to_hhmm(window.start_min),
        "window_end_hhmm": timetable.minute_to_hhmm(window.end_min),
    }, plan_date)
    return target, result, payload


def _refuse_while_emergency(action: str) -> None:
    """Keep a live emergency possession from being replaced by a planned order."""
    if not SYSTEM_STATE.get("emergency_active"):
        return
    raise HTTPException(
        status_code=409,
        detail=(
            "An emergency possession is protecting the line. "
            f"{action} Clear it only after the site gang reports the track safe."
        ),
    )


@app.post("/api/chief/sanction")
def chief_sanction(
    payload: dict[str, object] | None = Body(None),
) -> dict[str, object]:
    _refuse_while_emergency("A planned block cannot take the line while it is protected.")
    body = payload or {}
    plan_type = str(body.get("plan_type") or SYSTEM_STATE["active_plan"])
    target_line = str(body["target_line"]) if body.get("target_line") else None
    sector = str(body["sector"]) if body.get("sector") else None
    # The browser may send minute. The server clock is the only clock.
    body.get("minute")
    clock = timetable.wrap_minute(_server_minute())
    chosen_index = _body_window_index(body)
    if chosen_index is None:
        target, _solve_bundles, result = _solve_selected(plan_type, target_line, sector, _earliest(), planned=True, bound_to_window=True)
        _, _, _, slot = _planned_target_context(target_line, sector)
        slot_payload = _slot_payload(slot)
    else:
        target, result, slot_payload = _solve_target_in_window(plan_type, target_line, sector, chosen_index)
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
    if slot_payload is not None:
        for field in (
            "plan_date",
            "window_label",
            "window_index",
            "day_number",
            "day_label",
            "week_in_month",
            "weekday",
            "short_label",
            "compact_label",
        ):
            if field in slot_payload:
                record[field] = slot_payload[field]
    _annotate_request(record, target)
    _lock_possession(
        record,
        plan_type,
        "COA-CR-2026-BLK-9182",
        emergency=False,
        outcome="SANCTIONED",
    )
    rows = timetable.get_regulated_positions(clock, _active_possessions())
    execution = _phase_snapshot(clock, rows)
    when = record.get("plan_day_label") or ""
    if execution is not None and execution.get("phase") == "SCHEDULED":
        message = (
            f"Order locked for {when} {record['start_hhmm']}–{record['end_hhmm']}. "
            "The line is still open. It closes at that time."
        )
    elif execution is not None and bool(execution.get("live")):
        message = (
            f"Order locked. {record['track_line']} is closed now, until {record['end_hhmm']}."
        )
    else:
        message = (
            f"Order locked for {when} {record['start_hhmm']}–{record['end_hhmm']}."
        )
    if slot_payload is not None:
        when = slot_payload.get("compact_label") or slot_payload.get("day_label")
        message = f"Window: {slot_payload['window_label']} on {when}. " + message
    return {
        "status": "SANCTIONED",
        "message": message,
        "coa_dossier_id": SYSTEM_STATE["coa_dossier_id"],
        "plan_type": plan_type,
        "solver_status": result.status,
        "possession": record,
        "execution": execution,
        "system_state": SYSTEM_STATE,
        "planned_slot": slot_payload,
    }


@app.post("/api/chief/take-line")
def chief_take_line(
    payload: dict[str, object] | None = Body(None),
) -> dict[str, object]:
    """Section controller takes the line at the current clock, then runs the work sequence."""
    _refuse_while_emergency("The line stays protected for the emergency repair.")
    body = payload or {}
    plan_type = str(body.get("plan_type") or SYSTEM_STATE["active_plan"])
    target_line = str(body["target_line"]) if body.get("target_line") else None
    sector = str(body["sector"]) if body.get("sector") else None
    body.get("minute")
    clock = timetable.wrap_minute(_server_minute())
    target, _solve_bundles, result = _solve_selected(plan_type, target_line, sector, int(clock), planned=True)
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
    _annotate_request(record, target)
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


_CONGESTION_HOLD_REASON: str = "Corridor congestion. The demand stays open for the next path."


def _hold_bundle(raw: dict[str, object] | None) -> defects.MaintenanceBundle | None:
    """The open bundle this hold can offer again, when the backlog still has it."""
    bundles = list(defects.cluster_defects_into_bundles(_planning_defects()))
    if not bundles:
        return None
    if isinstance(raw, dict):
        wanted = str(raw.get("bundle_id") or "")
        return next((bundle for bundle in bundles if bundle.bundle_id == wanted), None)
    return bundles[0]


def _alternate_window(raw: dict[str, object] | None) -> dict[str, object]:
    """The next possession window engineering can apply for."""
    base = sim_clock.sim_date()
    skip = raw.get("window_index") if isinstance(raw, dict) else None
    start = int(skip) + 1 if isinstance(skip, int) and not isinstance(skip, bool) else 0
    bundle = _hold_bundle(raw)
    if bundle is not None:
        placed = block_windows.find_option_slots(
            bundle,
            timetable.get_all_trajectories(),
            start,
            base,
            count=1,
        )
        if placed:
            payload = _slot_payload(placed[0]) or {}
            payload["available"] = True
            payload["placed"] = True
            return payload
    last = start + block_windows.MAX_WINDOWS_AHEAD
    for index in range(start, last):
        plan_date, window = block_windows.window_for_index(base, index)
        if block_windows.window_is_past(plan_date, window):
            continue
        payload = _stamp_day(
            {
                "plan_date": plan_date.isoformat(),
                "window_label": window.label,
                "window_index": index,
                "window_start_hhmm": timetable.minute_to_hhmm(window.start_min),
                "window_end_hhmm": timetable.minute_to_hhmm(window.end_min),
                "available": True,
                "placed": False,
            },
            plan_date,
        )
        return payload
    return {"available": False, "placed": False}


def _alternate_phrase(alternate: dict[str, object]) -> str:
    if not alternate.get("available", True):
        return "No later window is open in the next seven days. Engineering can apply again when a path is free."
    label = str(alternate.get("window_label") or "the next window")
    start = str(alternate.get("window_start_hhmm") or "")
    end = str(alternate.get("window_end_hhmm") or "")
    when = str(alternate.get("day_label") or alternate.get("plan_date") or "")
    if alternate.get("placed"):
        return f"The same job fits {label} {start}–{end} on {when}."
    return f"Engineering can apply again in {label} {start}–{end} on {when}."


def _post_congestion_hold(
    raw: dict[str, object] | None,
    alternate: dict[str, object],
    *,
    clearing: bool,
    caution: bool,
) -> str:
    """Tell engineering, the station master, and the traction power controller."""
    count = int(SYSTEM_STATE.get("hold_count") or 0) + 1
    SYSTEM_STATE["hold_count"] = count
    base = count * 10
    existing = raw.get("possession_ref") if isinstance(raw, dict) else None
    ref = existing if isinstance(existing, str) and existing else f"COA-CR-HOLD-{int(sim_clock.absolute_minute())}-{count}"
    path = _alternate_phrase(alternate)
    power = isinstance(raw, dict) and _shift_is_power(raw)
    caution_text = " Caution 30 km/h is imposed until the line is clear." if caution else ""
    if clearing:
        gang = "Congestion hold. Clear the line. Staff and machines off the track." + caution_text
        stations = "Congestion hold. The gang is clearing the line. Keep signals at danger." + caution_text
        if power:
            traction = "Keep the feeder isolated until the gang reports the line clear. " + path
        else:
            traction = "No power block is agreed. The demand is held. " + path
    else:
        gang = "The block has not started. Stand down. " + path
        stations = "The block was not taken. " + path
        if power:
            traction = "Do not isolate. The agreed power block is held. " + path
        else:
            traction = "No power block is agreed. The demand is held. " + path
    officer = f"Congestion hold. The demand is not closed. {path}"
    station_role = _station_master_role(raw) if isinstance(raw, dict) and (
        "worksite_start_km" in raw or "closed_start_km" in raw
    ) else _ROLE_STATION
    persistence.post_block_message(ref, "CONGESTION_HOLD", _CONSOLE_CONTROLLER, _CONSOLE_OFFICER, officer, sequence=base)
    persistence.post_block_message(ref, "CONGESTION_HOLD", _CONSOLE_CONTROLLER, station_role, stations, sequence=base + 1)
    persistence.post_block_message(ref, "CONGESTION_HOLD", _CONSOLE_CONTROLLER, _ROLE_TPC, traction, sequence=base + 2)
    persistence.post_block_message(ref, "CONGESTION_HOLD", _CONSOLE_CONTROLLER, _ROLE_GANG, gang, sequence=base + 3)
    if clearing:
        persistence.post_block_message(
            ref,
            "CLEAR_LINE",
            _CONSOLE_CONTROLLER,
            _ROLE_GANG,
            gang,
            sequence=base,
        )
    return ref


def _remember_hold(alternate: dict[str, object], ref: str, phase: str) -> None:
    SYSTEM_STATE["congestion_hold"] = {
        "reason": _CONGESTION_HOLD_REASON,
        "alternate_window": alternate,
        "possession_ref": ref,
        "phase": phase,
    }
    SYSTEM_STATE["dossier_submitted"] = True
    SYSTEM_STATE["reject_reason"] = None
    persistence.record_dossier_submission(
        plan_type=str(SYSTEM_STATE["active_plan"]),
        submitted_by=_CONSOLE_CONTROLLER,
        status="HELD",
        reject_reason=_CONGESTION_HOLD_REASON,
    )


def _hold_message(alternate: dict[str, object], *, clearing: bool, caution: bool) -> str:
    path = _alternate_phrase(alternate)
    if clearing and caution:
        return f"Congestion hold. The gang is clearing the line. Caution 30 km/h is on. {path}"
    if clearing:
        return f"Congestion hold. The gang is clearing the line. {path}"
    return f"Congestion hold. The demand stays open. {path}"


@app.post("/api/chief/reject")
def chief_reject(
    payload: dict[str, object] | None = Body(None),
) -> dict[str, object]:
    """Hold a transmitted demand for the next path. A live gang clears the line first."""
    _refuse_while_emergency("A congestion hold does not reopen a line that is under emergency protection.")
    body = payload or {}
    body.get("reason")
    sim_clock.advance()
    tick_block_lifecycle()
    raw = SYSTEM_STATE.get("possession")
    sanctioned = bool(SYSTEM_STATE.get("sanctioned")) and isinstance(raw, dict)
    if not sanctioned and not SYSTEM_STATE.get("dossier_submitted"):
        raise HTTPException(
            status_code=409,
            detail="No dossier has been sent. There is nothing to hold.",
        )
    if sanctioned:
        assert isinstance(raw, dict)
        ref = _ensure_possession_ref(raw, emergency=bool(SYSTEM_STATE.get("emergency_active")))
        if persistence.has_block_message(ref, "WORK_COMPLETE"):
            raise HTTPException(
                status_code=409,
                detail="The site gang has reported the work complete. Clear the line.",
            )
        if bool(raw.get("withdraw_pending")):
            raise HTTPException(status_code=409, detail="The block is already being withdrawn.")
        if "start_abs" not in raw:
            _stamp_absolute_times(raw)
        now = sim_clock.absolute_minute()
        if now + 1e-9 >= float(raw["start_abs"]):
            caution = _start_withdrawal(raw, now)
            alternate = _alternate_window(raw)
            _post_congestion_hold(raw, alternate, clearing=True, caution=caution)
            _remember_hold(alternate, ref, "DEMOBILISING")
            rows = timetable.get_regulated_positions(sim_clock.minute_of_day(), _active_possessions())
            return {
                "status": "HELD",
                "phase": "DEMOBILISING",
                "message": _hold_message(alternate, clearing=True, caution=caution),
                "reason": _CONGESTION_HOLD_REASON,
                "alternate_window": alternate,
                "possession": raw,
                "execution": _phase_snapshot(sim_clock.minute_of_day(), rows),
                "system_state": SYSTEM_STATE,
            }
        notice = dict(raw)
        _clear_possession(outcome="WITHDRAWN", actor=_CONSOLE_CONTROLLER)
        persistence.post_block_message(
            ref,
            "WITHDRAWN",
            _CONSOLE_CONTROLLER,
            _ROLE_OFFICE,
            "Block withdrawn before the start. The demand is held for the next path.",
        )
        alternate = _alternate_window(notice)
        posted_ref = _post_congestion_hold(notice, alternate, clearing=False, caution=False)
        _remember_hold(alternate, posted_ref, "HELD")
        return {
            "status": "HELD",
            "phase": "HELD",
            "message": _hold_message(alternate, clearing=False, caution=False),
            "reason": _CONGESTION_HOLD_REASON,
            "alternate_window": alternate,
            "possession": None,
            "system_state": SYSTEM_STATE,
        }
    alternate = _alternate_window(None)
    ref = _post_congestion_hold(None, alternate, clearing=False, caution=False)
    _remember_hold(alternate, ref, "HELD")
    return {
        "status": "HELD",
        "phase": "HELD",
        "message": _hold_message(alternate, clearing=False, caution=False),
        "reason": _CONGESTION_HOLD_REASON,
        "alternate_window": alternate,
        "possession": None,
        "system_state": SYSTEM_STATE,
    }


def _finish_withdrawn(raw: dict[str, object]) -> dict[str, object]:
    """The gang has restored the track. The line reopens and the defects stay open."""
    ref = _ensure_possession_ref(raw, emergency=bool(SYSTEM_STATE.get("emergency_active")))
    _stamp_clearance(raw)
    started = float(raw.get("start_abs") or 0.0) <= float(raw.get("withdraw_requested_abs") or 0.0) or bool(raw.get("withdraw_pending"))
    _clear_possession(outcome="WITHDRAWN", actor=_CONSOLE_CONTROLLER)
    persistence.post_block_message(
        ref,
        "WITHDRAWN",
        _CONSOLE_CONTROLLER,
        _ROLE_OFFICE,
        "Block withdrawn. Work incomplete. Defects remain open.",
    )
    if started and bool(raw.get("withdraw_pending")):
        _post_line_cleared(raw)
    SYSTEM_STATE["earliest_start_min"] = 0
    SYSTEM_STATE["dossier_submitted"] = False
    SYSTEM_STATE["reject_reason"] = None
    return {
        "status": "WITHDRAWN",
        "phase": "WITHDRAWN",
        "message": "Block withdrawn. Work incomplete. Defects remain open.",
        "completed_defects": 0,
        "system_state": SYSTEM_STATE,
    }


@app.post("/api/chief/clear")
def chief_clear() -> dict[str, object]:
    sim_clock.advance()
    tick_block_lifecycle()
    raw = SYSTEM_STATE.get("possession")
    sanctioned = bool(SYSTEM_STATE.get("sanctioned")) and isinstance(raw, dict)
    if sanctioned:
        assert isinstance(raw, dict)
        ref = _ensure_possession_ref(raw, emergency=bool(SYSTEM_STATE.get("emergency_active")))
        if bool(raw.get("withdraw_pending")):
            if not persistence.has_block_message(ref, "WORK_STOPPED"):
                raise HTTPException(
                    status_code=409,
                    detail="Site gang has not reported the track restored.",
                )
            return _finish_withdrawn(raw)
        if not persistence.has_block_message(ref, "WORK_COMPLETE"):
            if bool(raw.get("emergency")) or bool(SYSTEM_STATE.get("emergency_active")):
                raise HTTPException(
                    status_code=409,
                    detail="The site gang has not reported the emergency repair complete. The line stays protected.",
                )
            raise HTTPException(
                status_code=409,
                detail="Site gang has not reported work complete. Use Withdraw block to cancel.",
            )
        _stamp_clearance(raw)
        paused_open = SYSTEM_STATE.get("paused_work")
        if isinstance(paused_open, dict) and paused_open.get("bundle_id"):
            persistence.post_block_message(
                ref,
                "REPLAN",
                _CONSOLE_CONTROLLER,
                _ROLE_OFFICE,
                (
                    "Emergency cleared. Planned block "
                    f"{paused_open.get('possession_ref') or paused_open.get('bundle_id')} "
                    "is still paused. Replan it before the gang goes back."
                ),
                sequence=0,
            )
        completed = _complete_possession_defects()
        _clear_possession(outcome="CLEARED", actor=_CONSOLE_CONTROLLER)
        _post_line_cleared(raw)
    else:
        completed = _complete_possession_defects()
        _clear_possession(outcome="CLEARED", actor=_CONSOLE_CONTROLLER)
    SYSTEM_STATE["tsr"] = None
    SYSTEM_STATE["earliest_start_min"] = 0
    SYSTEM_STATE["dossier_submitted"] = False
    SYSTEM_STATE["reject_reason"] = None
    paused = SYSTEM_STATE.get("paused_work")
    message = "Line given back. Signals cleared and the feeder is live."
    if isinstance(paused, dict) and paused.get("bundle_id"):
        paused_ref = str(paused.get("possession_ref") or paused.get("bundle_id"))
        message += f" Planned block {paused_ref} is still paused. Replan it before the gang goes back."
    return {
        "status": "CLEARED",
        "phase": "CLEARED",
        "message": message,
        "completed_defects": completed,
        "paused_work": paused if isinstance(paused, dict) else None,
        "system_state": SYSTEM_STATE,
    }


def _start_withdrawal(raw: dict[str, object], now: float) -> bool:
    """Move a block that has already started into demobilising.

    The gang is told to clear by the withdrawal record. When protection was
    already on, the 30 km/h caution is imposed. Returns whether that caution
    was imposed.
    """
    timeline = raw.get("timeline")
    if not isinstance(timeline, dict):
        raise HTTPException(status_code=409, detail="The sanctioned block has no timeline.")
    ready_abs = _mark_abs(raw, float(timeline["protection_ready_min"]))
    raw["withdraw_pending"] = True
    raw["withdraw_requested_abs"] = round(now, 3)
    raw["demob_ready_abs"] = round(now + _demob_minutes(timeline), 3)
    caution = now + 1e-9 >= ready_abs
    if caution:
        raw["work_had_started"] = True
        _impose_incomplete_caution(raw)
    _record_possession(
        raw,
        str(SYSTEM_STATE["active_plan"]),
        _CONSOLE_CONTROLLER,
        bool(SYSTEM_STATE.get("emergency_active")),
        "WITHDRAWING",
        None,
    )
    tick_block_lifecycle()
    return caution


@app.post("/api/chief/withdraw")
def chief_withdraw() -> dict[str, object]:
    """Cancel a sanctioned block that the site gang has not yet reported complete.

    Before the start the line reopens at once. After mobilising has begun the
    block moves to demobilising and stays closed until the gang reports clear.
    """
    _refuse_while_emergency("Withdraw does not reopen a line that is under emergency protection.")
    sim_clock.advance()
    tick_block_lifecycle()
    raw = SYSTEM_STATE.get("possession")
    if not SYSTEM_STATE.get("sanctioned") or not isinstance(raw, dict):
        raise HTTPException(status_code=409, detail="No sanctioned block to withdraw.")
    ref = _ensure_possession_ref(raw, emergency=bool(SYSTEM_STATE.get("emergency_active")))
    if persistence.has_block_message(ref, "WORK_COMPLETE"):
        raise HTTPException(
            status_code=409,
            detail="Site gang has reported work complete. Clear the line.",
        )
    if bool(raw.get("withdraw_pending")):
        raise HTTPException(status_code=409, detail="The block is already being withdrawn.")
    now = sim_clock.absolute_minute()
    start_abs = float(raw["start_abs"])
    if now + 1e-9 < start_abs:
        _clear_possession(outcome="WITHDRAWN", actor=_CONSOLE_CONTROLLER)
        persistence.post_block_message(
            ref,
            "WITHDRAWN",
            _CONSOLE_CONTROLLER,
            _ROLE_OFFICE,
            "Block withdrawn. Work incomplete. Defects remain open.",
        )
        return {
            "status": "WITHDRAWN",
            "message": "Block withdrawn. Work incomplete. Defects remain open.",
            "system_state": SYSTEM_STATE,
        }
    _start_withdrawal(raw, now)
    rows = timetable.get_regulated_positions(sim_clock.minute_of_day(), _active_possessions())
    return {
        "status": "DEMOBILISING",
        "phase": "DEMOBILISING",
        "message": "Block withdrawn. The gang is clearing the line.",
        "possession": raw,
        "execution": _phase_snapshot(sim_clock.minute_of_day(), rows),
        "system_state": SYSTEM_STATE,
    }


# Mumbai suburban peak on the CSMT–Karjat section. The daytime gang duty
# opens at 11:30, which is the end of the morning peak.
_MORNING_PEAK: tuple[int, int] = (7 * 60, calendar_plan.DAY_OFFPEAK_START_MIN)
_EVENING_PEAK: tuple[int, int] = (16 * 60 + 30, 21 * 60 + 30)
_MAIL_EXPRESS: frozenset[str] = frozenset(
    {timetable.TYPE_PREMIUM, timetable.TYPE_SUPERFAST, timetable.TYPE_EXPRESS}
)
_SHIFT_STEP_MIN: int = 5
_SHIFT_CLOCKS: tuple[str, ...] = (
    "start_min",
    "end_min",
    "arrive_min",
    "protection_ready_min",
    "work_start_min",
    "work_end_min",
    "return_start_min",
)


def _shift_duty(label: str) -> tuple[int, int] | None:
    """Gang duty hours for a possession window, or nothing when the label has none."""
    duties: dict[str, tuple[int, int]] = {
        calendar_plan.LABEL_NIGHTLY: (calendar_plan.NIGHTLY_START_MIN, calendar_plan.NIGHTLY_END_MIN),
        calendar_plan.LABEL_MEGA_BLOCK: (calendar_plan.MEGA_BLOCK_START_MIN, calendar_plan.MEGA_BLOCK_END_MIN),
        calendar_plan.LABEL_EXTENDED: (calendar_plan.EXTENDED_START_MIN, calendar_plan.EXTENDED_END_MIN),
        calendar_plan.LABEL_DAY_OFFPEAK: (calendar_plan.DAY_OFFPEAK_START_MIN, calendar_plan.DAY_OFFPEAK_END_MIN),
    }
    return duties.get(label)


def _shift_plan_date(raw: dict[str, object]) -> date:
    stored = raw.get("plan_date")
    if isinstance(stored, str) and stored.strip():
        return date.fromisoformat(stored.strip()[:10])
    plan_day = raw.get("plan_day")
    if isinstance(plan_day, int) and not isinstance(plan_day, bool):
        return sim_clock.date_for_absolute_day(plan_day)
    return sim_clock.sim_date()


def _shift_is_power(raw: dict[str, object]) -> bool:
    return _is_power_block(raw) or bool(raw.get("earth_required"))


def _shift_is_suburban(raw: dict[str, object]) -> bool:
    section = str(raw.get("section_id") or "")
    if section in (topology.SECTION_1_ID, topology.SECTION_2_ID):
        return True
    if section in (topology.SECTION_3_ID, topology.SECTION_4_ID):
        return False
    km = float(raw.get("worksite_start_km") or raw.get("closed_start_km") or 0.0)
    return topology.get_section_id_at_km(km) in (topology.SECTION_1_ID, topology.SECTION_2_ID)


def _shift_duration(raw: dict[str, object]) -> int:
    start = int(raw["start_min"]) % _MINUTES_PER_DAY
    end = int(raw["end_min"]) % _MINUTES_PER_DAY
    if end <= start:
        return end + _MINUTES_PER_DAY - start
    return end - start


def _shift_lines(raw: dict[str, object]) -> list[str]:
    lines = raw.get("protected_lines")
    if isinstance(lines, list):
        found = [str(line) for line in lines if isinstance(line, str) and line]
        if found:
            return found
    line = raw.get("track_line")
    if isinstance(line, str) and line:
        return [line]
    return []


def _shift_ranges_overlap(start: int, end: int, lo: int, hi: int) -> bool:
    return start < hi and end > lo


def _shift_peak_hit(start: int, end: int, suburban: bool, label: str) -> bool:
    """True when this span sits in the suburban peak.

    An extended block is a total traffic block already agreed through the peak.
    """
    if not suburban or label == calendar_plan.LABEL_EXTENDED:
        return False
    return _shift_ranges_overlap(start, end, *_MORNING_PEAK) or _shift_ranges_overlap(
        start, end, *_EVENING_PEAK
    )


def _shift_mail_blockers(raw: dict[str, object], start: int, end: int) -> list[str]:
    """Mail, express, and superfast trains whose path meets the closed line."""
    lo = float(raw["closed_start_km"])
    hi = float(raw["closed_end_km"])
    found: list[str] = []
    seen: set[str] = set()
    for line in _shift_lines(raw):
        for train in timetable.get_trains_in_block(lo, hi, float(start), float(end), line):
            if train.train_type not in _MAIL_EXPRESS or train.train_id in seen:
                continue
            seen.add(train.train_id)
            found.append(f"{train.train_id} {train.train_type}")
    return found


def _shift_live_reason(raw: dict[str, object]) -> str | None:
    """Why a possession that has already started cannot be slid."""
    if bool(raw.get("withdraw_pending")):
        return "This possession is being withdrawn. Finish the withdrawal."
    ref = str(raw.get("possession_ref") or "")
    if ref and persistence.has_block_message(ref, "WORK_COMPLETE"):
        return "The site gang has reported the work complete. Clear the line."
    if "start_abs" not in raw:
        _stamp_absolute_times(raw)
    now = sim_clock.absolute_minute()
    start_abs = float(raw["start_abs"])
    if now + 1e-9 < start_abs:
        return None
    end_abs = float(raw["end_abs"])
    if now + 1e-9 >= end_abs:
        return (
            "This possession is already running past its planned end. "
            "Finish it, let it overrun, or withdraw it."
        )
    timeline = raw.get("timeline")
    arrived = False
    if isinstance(timeline, dict) and isinstance(timeline.get("arrive_min"), (int, float)):
        arrived = now + 1e-9 >= _mark_abs(raw, float(timeline["arrive_min"]))
    if _shift_is_power(raw) and arrived:
        return (
            "The feeder is already isolated. "
            "Finish this possession, let it overrun, or withdraw it."
        )
    if arrived:
        return (
            "Machines are already on the line. "
            "Finish this possession, let it overrun, or withdraw it."
        )
    return (
        "The line is already taken for this possession. "
        "Finish it, let it overrun, or withdraw it."
    )


def _shift_duties(raw: dict[str, object]) -> list[tuple[date, str, int, int, bool]]:
    """Later gang duties to try, earliest first.

    The last flag is true when the duty is the one already agreed on the sanction.
    A power block, and an extended block, stay inside that agreement.
    """
    label = str(raw.get("window_label") or "BLOCK")
    plan = _shift_plan_date(raw)
    agreed = _shift_duty(label)
    if agreed is not None:
        rows: list[tuple[date, str, int, int, bool]] = [(plan, label, agreed[0], agreed[1], True)]
        if _shift_is_power(raw) or label == calendar_plan.LABEL_EXTENDED:
            return rows
        if label == calendar_plan.LABEL_NIGHTLY:
            rows.append(
                (
                    plan,
                    calendar_plan.LABEL_DAY_OFFPEAK,
                    calendar_plan.DAY_OFFPEAK_START_MIN,
                    calendar_plan.DAY_OFFPEAK_END_MIN,
                    False,
                )
            )
        next_day = plan + timedelta(days=1)
        if label == calendar_plan.LABEL_DAY_OFFPEAK:
            rows.append(
                (
                    next_day,
                    calendar_plan.LABEL_DAY_OFFPEAK,
                    calendar_plan.DAY_OFFPEAK_START_MIN,
                    calendar_plan.DAY_OFFPEAK_END_MIN,
                    False,
                )
            )
        else:
            rows.append(
                (
                    next_day,
                    calendar_plan.LABEL_NIGHTLY,
                    calendar_plan.NIGHTLY_START_MIN,
                    calendar_plan.NIGHTLY_END_MIN,
                    False,
                )
            )
        return rows
    next_day = plan + timedelta(days=1)
    return [
        (plan, calendar_plan.LABEL_NIGHTLY, calendar_plan.NIGHTLY_START_MIN, calendar_plan.NIGHTLY_END_MIN, False),
        (
            plan,
            calendar_plan.LABEL_DAY_OFFPEAK,
            calendar_plan.DAY_OFFPEAK_START_MIN,
            calendar_plan.DAY_OFFPEAK_END_MIN,
            False,
        ),
        (
            next_day,
            calendar_plan.LABEL_NIGHTLY,
            calendar_plan.NIGHTLY_START_MIN,
            calendar_plan.NIGHTLY_END_MIN,
            False,
        ),
    ]


def _shift_floor(raw: dict[str, object], plan_date: date, duty_lo: int, same_window: bool) -> int:
    today = sim_clock.absolute_day()
    plan_day = sim_clock.absolute_day_of(plan_date.isoformat())
    if plan_day < today:
        return _MINUTES_PER_DAY
    floor = duty_lo
    if same_window and plan_date == _shift_plan_date(raw):
        floor = max(floor, int(raw["start_min"]) + _SHIFT_STEP_MIN)
    if plan_day == today:
        floor = max(floor, int(sim_clock.minute_of_day()) + _SHIFT_STEP_MIN)
    remainder = floor % _SHIFT_STEP_MIN
    if remainder:
        floor += _SHIFT_STEP_MIN - remainder
    return floor


def _shift_abs(plan_date: date, minute: int) -> int:
    return sim_clock.absolute_day_of(plan_date.isoformat()) * _MINUTES_PER_DAY + int(minute)


def _shift_proposal(
    raw: dict[str, object],
    plan_date: date,
    label: str,
    duty_lo: int,
    duty_hi: int,
    same_window: bool,
    start: int,
    end: int,
) -> dict[str, object]:
    duty_lo_hhmm = timetable.minute_to_hhmm(duty_lo)
    duty_hi_hhmm = timetable.minute_to_hhmm(duty_hi)
    suburban = _shift_is_suburban(raw)
    checks: list[str] = ["Mail and express paths are clear."]
    if label == calendar_plan.LABEL_EXTENDED:
        checks.append("The extended block already covers the suburban peak.")
    elif suburban:
        checks.append("The time is outside the suburban peak (07:00–11:30 and 16:30–21:30).")
    else:
        checks.append("The suburban peak does not apply beyond Karjat.")
    if _shift_is_power(raw):
        checks.append(f"The time stays inside the agreed power block {duty_lo_hhmm}–{duty_hi_hhmm}.")
    else:
        checks.append("No power block is agreed on this possession.")
    if same_window:
        checks.append(f"The time stays inside gang duty {duty_lo_hhmm}–{duty_hi_hhmm}.")
    else:
        checks.append(
            f"This moves the gang onto {label} duty {duty_lo_hhmm}–{duty_hi_hhmm}. "
            "Engineering has to accept that roster."
        )
    return {
        "start_min": start,
        "end_min": end if end < _MINUTES_PER_DAY else 0,
        "start_hhmm": timetable.minute_to_hhmm(start),
        "end_hhmm": timetable.minute_to_hhmm(end),
        "plan_date": plan_date.isoformat(),
        "window_label": label,
        "same_window": same_window,
        "checks": checks,
    }


def _shift_search(raw: dict[str, object]) -> tuple[dict[str, object] | None, str]:
    """The next legal start, or the reason every later time was refused."""
    if "closed_start_km" not in raw or "closed_end_km" not in raw:
        return None, "This sanctioned block has no closed limits."
    if not _shift_lines(raw):
        return None, "This sanctioned block has no protected line."
    duration = _shift_duration(raw)
    if duration < _SHIFT_STEP_MIN:
        duration = _SHIFT_STEP_MIN
    current_abs = _shift_abs(_shift_plan_date(raw), int(raw["start_min"]))
    mail_hits: list[str] = []
    peak_skips = 0
    saw_room = False
    for plan_date, label, duty_lo, duty_hi, same_window in _shift_duties(raw):
        latest = duty_hi - duration
        floor = _shift_floor(raw, plan_date, duty_lo, same_window)
        if floor > latest:
            continue
        saw_room = True
        start = floor
        while start <= latest:
            end = start + duration
            if _shift_abs(plan_date, start) <= current_abs:
                start += _SHIFT_STEP_MIN
                continue
            if _shift_peak_hit(start, end, _shift_is_suburban(raw), label):
                peak_skips += 1
                start += _SHIFT_STEP_MIN
                continue
            blockers = _shift_mail_blockers(raw, start, end if end < _MINUTES_PER_DAY else end - _MINUTES_PER_DAY)
            if blockers:
                for name in blockers:
                    if name not in mail_hits and len(mail_hits) < 3:
                        mail_hits.append(name)
                start += _SHIFT_STEP_MIN
                continue
            return _shift_proposal(raw, plan_date, label, duty_lo, duty_hi, same_window, start, end), ""
    if _shift_is_power(raw) and mail_hits:
        listed = ", ".join(mail_hits)
        return None, (
            "Every later minute inside the agreed power block puts a mail or express train "
            f"on the closed line ({listed}). Ask the traction power controller for a different isolation."
        )
    if _shift_is_power(raw):
        return None, (
            "The agreed power block cannot take this job any later. "
            "Ask the traction power controller for a new isolation."
        )
    if mail_hits and peak_skips == 0:
        listed = ", ".join(mail_hits)
        return None, (
            "Every later time inside gang duty puts a mail or express train "
            f"on the closed line ({listed})."
        )
    if peak_skips and not mail_hits:
        return None, "Every later time falls in the suburban peak."
    if not saw_room:
        return None, "The job does not fit in the remaining gang duty."
    return None, (
        "No later time keeps the mail and express paths clear, stays out of the suburban peak, "
        "and stays inside gang duty."
    )


def _shift_request_still_legal(raw: dict[str, object], request: dict[str, object]) -> str | None:
    live = _shift_live_reason(raw)
    if live:
        return live
    start = int(request["start_min"])
    end = int(request["end_min"])
    if end <= start:
        end += _MINUTES_PER_DAY
    label = str(request["window_label"])
    duty = _shift_duty(label)
    if duty is None:
        return "The proposed time is outside gang duty."
    if start < duty[0] or end > duty[1]:
        return "The proposed time has left gang duty."
    plan_date = date.fromisoformat(str(request["plan_date"])[:10])
    if _shift_abs(plan_date, start) <= _shift_abs(_shift_plan_date(raw), int(raw["start_min"])):
        return "The proposed time is no later than the sanctioned window."
    if _shift_is_power(raw):
        agreed_label = str(raw.get("window_label") or "")
        same_day = str(request["plan_date"])[:10] == _shift_plan_date(raw).isoformat()
        if label != agreed_label or not same_day:
            return "A power block stays inside the agreed isolation. Ask the traction power controller."
    if _shift_peak_hit(start, end if end < _MINUTES_PER_DAY else end - _MINUTES_PER_DAY, _shift_is_suburban(raw), label):
        return "That time is in the suburban peak."
    blockers = _shift_mail_blockers(raw, start, int(request["end_min"]))
    if blockers:
        return "A mail or express train is on that path (" + ", ".join(blockers[:3]) + ")."
    return None


def _shift_clock(mark: float, old_start: int, new_start: int) -> float:
    offset = float(mark) - float(old_start)
    if offset < -1e-6:
        offset += float(_MINUTES_PER_DAY)
    moved = float(new_start) + offset
    if moved >= _MINUTES_PER_DAY:
        moved -= float(_MINUTES_PER_DAY)
    return round(moved, 3)


def _slide_possession(raw: dict[str, object], proposal: dict[str, object]) -> dict[str, object]:
    record = copy.deepcopy(raw)
    old_start = int(record["start_min"])
    new_start = int(proposal["start_min"])
    new_end = int(proposal["end_min"])
    record["start_min"] = new_start
    record["end_min"] = new_end
    record["start_hhmm"] = str(proposal["start_hhmm"])
    record["end_hhmm"] = str(proposal["end_hhmm"])
    record["plan_date"] = str(proposal["plan_date"])
    record["window_label"] = str(proposal["window_label"])
    record["immediate"] = False
    record["shift_count"] = int(record.get("shift_count") or 0) + 1
    record.pop("shift_request", None)
    for key in ("start_abs", "end_abs", "plan_day", "plan_day_label"):
        record.pop(key, None)
    timeline = record.get("timeline")
    if isinstance(timeline, dict):
        moved = dict(timeline)
        for key in _SHIFT_CLOCKS:
            value = moved.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                moved[key] = _shift_clock(float(value), old_start, new_start)
        moved["start_min"] = float(new_start)
        moved["end_min"] = float(new_end)
        record["timeline"] = moved
    return record


def _remember_open_possession(raw: dict[str, object]) -> None:
    """Write the current sanction again so a restart keeps the shift request."""
    _record_possession(
        raw,
        str(SYSTEM_STATE["active_plan"]),
        str(SYSTEM_STATE.get("sanctioned_by") or _CONSOLE_CONTROLLER),
        False,
        "SANCTIONED",
        None,
    )


def _ask_engineering(raw: dict[str, object], proposal: dict[str, object]) -> None:
    count = int(raw.get("shift_request_count") or 0) + 1
    raw["shift_request_count"] = count
    raw["shift_request"] = proposal
    ref = str(raw.get("possession_ref") or "")
    old = f"{raw.get('start_hhmm', timetable.minute_to_hhmm(int(raw['start_min'])))}–{raw.get('end_hhmm', timetable.minute_to_hhmm(int(raw['end_min'])))}"
    new = f"{proposal['start_hhmm']}–{proposal['end_hhmm']}"
    checks = " ".join(str(item) for item in proposal["checks"])
    text = (
        f"Section controller asks to move {ref} from {old} to {new} on {proposal['plan_date']}. "
        f"The block still stands at {old} until engineering agrees. {checks}"
    )
    base = count * 10
    targets = [_CONSOLE_OFFICER, _ROLE_GANG, _ROLE_OFFICE]
    if _shift_is_power(raw):
        targets.append(_ROLE_TPC)
    for offset, role in enumerate(targets):
        persistence.post_block_message(
            ref,
            "SHIFT_REQUEST",
            _CONSOLE_CONTROLLER,
            role,
            text,
            sequence=base + offset,
        )
    _remember_open_possession(raw)


def _decline_shift(raw: dict[str, object]) -> dict[str, object]:
    request = raw.get("shift_request")
    if not isinstance(request, dict):
        raise HTTPException(status_code=409, detail="Engineering has not been asked.")
    raw.pop("shift_request", None)
    ref = str(raw.get("possession_ref") or "")
    standing = (
        f"{raw.get('start_hhmm', timetable.minute_to_hhmm(int(raw['start_min'])))}"
        f"–{raw.get('end_hhmm', timetable.minute_to_hhmm(int(raw['end_min'])))}"
    )
    persistence.post_block_message(
        ref,
        "ENGINEERING_DECLINED",
        _CONSOLE_OFFICER,
        f"{_CONSOLE_CONTROLLER}, {_ROLE_GANG}",
        f"Engineering declines the move. The block stays at {standing}.",
        sequence=int(raw.get("shift_request_count") or 1),
    )
    _remember_open_possession(raw)
    return {
        "status": "DECLINED",
        "message": f"Engineering declined the move. The block stays at {standing}.",
        "possession": raw,
        "system_state": SYSTEM_STATE,
    }


def _confirm_shift(raw: dict[str, object]) -> dict[str, object]:
    request = raw.get("shift_request")
    if not isinstance(request, dict):
        raise HTTPException(
            status_code=409,
            detail="Ask engineering before moving the block.",
        )
    reason = _shift_request_still_legal(raw, request)
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    record = _slide_possession(raw, request)
    _lock_possession(
        record,
        str(SYSTEM_STATE["active_plan"]),
        str(record.get("possession_ref") or SYSTEM_STATE.get("coa_dossier_id") or ""),
        emergency=False,
        outcome="SHIFTED",
    )
    ref = str(record["possession_ref"])
    window = f"{record['start_hhmm']}–{record['end_hhmm']}"
    persistence.post_block_message(
        ref,
        "ENGINEERING_AGREED",
        _CONSOLE_OFFICER,
        f"{_CONSOLE_CONTROLLER}, {_ROLE_GANG}",
        f"Engineering agrees. The block moves to {window} on {record.get('plan_date')}.",
        sequence=int(record.get("shift_count") or 1),
    )
    persistence.post_block_message(
        ref,
        "SHIFTED",
        _CONSOLE_CONTROLLER,
        _ROLE_OFFICE,
        f"Block moved to {window} after engineering agreed.",
        sequence=int(record.get("shift_count") or 1),
    )
    rows = timetable.get_regulated_positions(sim_clock.minute_of_day(), _active_possessions())
    return {
        "status": "SHIFTED",
        "message": f"Engineering agreed. The block now stands at {window}.",
        "possession": record,
        "execution": _phase_snapshot(sim_clock.minute_of_day(), rows),
        "system_state": SYSTEM_STATE,
    }


@app.post("/api/chief/shift")
def chief_shift(
    payload: dict[str, object] | None = Body(None),
) -> dict[str, object]:
    """Ask engineering for a later legal window. A live possession is left where it is."""
    _refuse_while_emergency("An emergency possession cannot be time-shifted.")
    body = payload or {}
    sim_clock.advance()
    raw = SYSTEM_STATE.get("possession")
    if not isinstance(raw, dict) or not SYSTEM_STATE.get("sanctioned"):
        raise HTTPException(
            status_code=409,
            detail="Nothing is sanctioned. The next solve still uses the normal start.",
        )
    if bool(body.get("decline")):
        return _decline_shift(raw)
    if bool(body.get("confirm")):
        return _confirm_shift(raw)
    live = _shift_live_reason(raw)
    if live:
        raise HTTPException(status_code=409, detail=live)
    proposal, refusal = _shift_search(raw)
    if proposal is None:
        raise HTTPException(status_code=409, detail=refusal)
    _ask_engineering(raw, proposal)
    standing = (
        f"{raw.get('start_hhmm', timetable.minute_to_hhmm(int(raw['start_min'])))}"
        f"–{raw.get('end_hhmm', timetable.minute_to_hhmm(int(raw['end_min'])))}"
    )
    checks = " ".join(str(item) for item in proposal["checks"])
    return {
        "status": "ASKED",
        "message": (
            f"Asked engineering to move the block to {proposal['start_hhmm']}–{proposal['end_hhmm']} "
            f"on {proposal['plan_date']}. It still stands at {standing} until engineering agrees. {checks}"
        ),
        "proposal": proposal,
        "possession": raw,
        "system_state": SYSTEM_STATE,
    }


@app.post("/api/chief/caution")
def chief_caution(
    payload: dict[str, object] | None = Body(None),
) -> dict[str, object]:
    body = payload or {}
    sim_clock.advance()
    raw = SYSTEM_STATE.get("possession")
    if isinstance(raw, dict):
        start_km = float(raw["worksite_start_km"])
        end_km = float(raw["worksite_end_km"])
        line = str(raw["track_line"])
    else:
        bundles = list(defects.cluster_defects_into_bundles(data_ingestion.get_effective_defects()))
        if bundles:
            target = _select_bundle(bundles, topology.LINE_MID, "GHAT")
            start_km, end_km, line = target.start_km, target.end_km, target.track_line
        else:
            center = _open_caution_center_km(body.get("center_km"))
            start_km = max(0.0, center - 3.0)
            end_km = min(192.0, center + 3.0)
            line = "CORRIDOR"
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


class _EmergencySpec:
    """One emergency the section controller can declare on the live corridor."""

    def __init__(
        self,
        label: str,
        short: str,
        half_km: float,
        work_min: int,
        clearance_min: int,
        all_lines: bool,
        ghat_only: bool,
        earth: bool,
        staff: int,
    ) -> None:
        self.label = label
        self.short = short
        self.half_km = half_km
        self.work_min = work_min
        self.clearance_min = clearance_min
        self.all_lines = all_lines
        self.ghat_only = ghat_only
        self.earth = earth
        self.staff = staff


_EMERGENCY_SPECS: dict[str, _EmergencySpec] = {
    "RAIL_FRACTURE": _EmergencySpec("Rail fracture", "FRACTURE", 0.10, 90, 5, False, False, False, 8),
    "LANDSLIDE": _EmergencySpec("Landslide on the Bhor Ghat", "LANDSLIDE", 0.25, 180, 5, True, True, False, 20),
    "OHE_BREAKDOWN": _EmergencySpec("OHE breakdown", "OHE", 0.20, 90, 15, False, False, True, 6),
}
_EMERGENCY_ALIASES: dict[str, str] = {
    "RAIL_FRACTURE": "RAIL_FRACTURE",
    "FRACTURE": "RAIL_FRACTURE",
    "RAIL_WEB_FRACTURE": "RAIL_FRACTURE",
    "LANDSLIDE": "LANDSLIDE",
    "LAND_SLIDE": "LANDSLIDE",
    "LANDSLIP": "LANDSLIDE",
    "OHE_BREAKDOWN": "OHE_BREAKDOWN",
    "OHE": "OHE_BREAKDOWN",
    "OHE_FAILURE": "OHE_BREAKDOWN",
    "OHE_PARTING": "OHE_BREAKDOWN",
}
_PROTECTION_APPROACH_KM = 1.2
_GHAT_LO_KM = 100.0
_GHAT_HI_KM = 128.0


def _parse_emergency_kind(body: dict[str, object]) -> tuple[str, _EmergencySpec]:
    raw = body.get("emergency_type")
    if raw is None or raw == "":
        raw = body.get("kind")
    token = str(raw or "").strip().upper().replace("-", "_").replace(" ", "_")
    kind = _EMERGENCY_ALIASES.get(token)
    if kind is None:
        names = "rail fracture, landslide on the Bhor Ghat, or OHE breakdown"
        raise HTTPException(
            status_code=400,
            detail=f"Choose the emergency: {names}. The line was not taken.",
        )
    return kind, _EMERGENCY_SPECS[kind]


def _parse_emergency_km(body: dict[str, object]) -> float:
    raw: object | None = None
    for key in ("chainage_km", "km", "emergency_km"):
        if key in body and body[key] not in (None, ""):
            raw = body[key]
            break
    if raw is None:
        raise HTTPException(
            status_code=400,
            detail="Enter the chainage in KM. The line was not taken.",
        )
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise HTTPException(status_code=400, detail="Chainage must be a KM value. The line was not taken.")
    try:
        km = float(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Chainage must be a KM value. The line was not taken.") from None
    if km < topology.CORRIDOR_START_KM or km > topology.CORRIDOR_END_KM:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Chainage must be between KM {topology.CORRIDOR_START_KM:.0f} and "
                f"KM {topology.CORRIDOR_END_KM:.0f}. The line was not taken."
            ),
        )
    return km


def _parse_emergency_line(body: dict[str, object]) -> str:
    raw = body.get("target_line")
    if raw is None or raw == "":
        raw = body.get("line")
    return str(raw or "").strip().upper()


def _emergency_lines(spec: _EmergencySpec, km: float, line: str) -> tuple[list[str], str]:
    """Lines to protect, and the feeder id for the site. Raises before any possession is locked."""
    if spec.ghat_only and not (_GHAT_LO_KM - 1e-6 <= km <= _GHAT_HI_KM + 1e-6):
        raise HTTPException(
            status_code=400,
            detail=(
                "A landslide block is only for the Bhor Ghat, KM 100.0 to KM 128.0. "
                "The line was not taken."
            ),
        )
    try:
        available = topology.get_available_lines_at_km(km)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{exc} The line was not taken.") from None
    if spec.all_lines:
        chosen = list(available)
    else:
        if not line:
            raise HTTPException(status_code=400, detail="Choose the line. The line was not taken.")
        if line not in available:
            present = ", ".join(available)
            raise HTTPException(
                status_code=400,
                detail=f"Line {line} is not at KM {km:.1f}. Lines there: {present}. The line was not taken.",
            )
        chosen = [line]
    try:
        sector_id = defects.get_power_subsector_for_location(km, chosen[0])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{exc} The line was not taken.") from None
    if spec.earth:
        isolated = defects.get_power_subsector(sector_id).isolated_line
        if isolated not in chosen:
            chosen.append(isolated)
    return chosen, sector_id


def _wrapped_end(start: int, duration: int) -> int:
    total = start + max(1, duration)
    if total >= _MINUTES_PER_DAY:
        return total - _MINUTES_PER_DAY
    return total


def _pause_other_work(reason: str) -> dict[str, object] | None:
    """Stand the open planned possession down without marking its defects done."""
    raw = SYSTEM_STATE.get("possession")
    if not SYSTEM_STATE.get("sanctioned") or not isinstance(raw, dict):
        return None
    if bool(SYSTEM_STATE.get("emergency_active")) or bool(raw.get("emergency")):
        return None
    ref = str(raw.get("possession_ref") or "")
    summary: dict[str, object] = {
        "possession_ref": ref,
        "bundle_id": raw.get("bundle_id"),
        "section_id": raw.get("section_id"),
        "track_line": raw.get("track_line"),
        "worksite_start_km": raw.get("worksite_start_km"),
        "worksite_end_km": raw.get("worksite_end_km"),
        "start_hhmm": raw.get("start_hhmm"),
        "end_hhmm": raw.get("end_hhmm"),
        "paused_reason": reason,
    }
    if ref:
        persistence.post_block_message(
            ref,
            "WORK_PAUSED",
            _CONSOLE_CONTROLLER,
            str(raw.get("role_gang") or _ROLE_GANG),
            (
                f"Stop work and stand clear. {reason} has taken the section. "
                "Do not resume. Control will replan this block."
            ),
            sequence=0,
        )
        persistence.post_block_message(
            ref,
            "WORK_PAUSED",
            _CONSOLE_CONTROLLER,
            _station_master_role(raw),
            (
                f"Planned work on {raw.get('track_line')} is paused. "
                "Protect the emergency site. Do not resume the planned block."
            ),
            sequence=1,
        )
        persistence.post_block_message(
            ref,
            "WORK_PAUSED",
            _CONSOLE_CONTROLLER,
            _ROLE_OFFICE,
            f"Planned block {ref} paused for {reason}. Defects stay open.",
            sequence=2,
        )
    if "start_min" in raw and "end_min" in raw and "power_sector_id" in raw and "section_id" in raw:
        _record_possession(
            raw,
            str(SYSTEM_STATE.get("active_plan") or "PUNCTUALITY_FIRST"),
            _CONSOLE_CONTROLLER,
            False,
            "PAUSED",
            reason,
        )
    SYSTEM_STATE["paused_work"] = summary
    return summary


def _note_superseded(previous: dict[str, object], reason: str) -> None:
    ref = str(previous.get("possession_ref") or "")
    if ref:
        persistence.post_block_message(
            ref,
            "SUPERSEDED",
            _CONSOLE_CONTROLLER,
            _ROLE_OFFICE,
            f"Emergency order {ref} is superseded. {reason}. Keep only the new site protected.",
            sequence=0,
        )
    if "start_min" in previous and "end_min" in previous and "power_sector_id" in previous and "section_id" in previous:
        _record_possession(
            previous,
            str(SYSTEM_STATE.get("active_plan") or "PUNCTUALITY_FIRST"),
            _CONSOLE_CONTROLLER,
            True,
            "SUPERSEDED",
            reason,
        )


def _build_emergency_record(
    kind: str,
    spec: _EmergencySpec,
    km: float,
    lines: list[str],
    sector_id: str,
    start: int,
) -> dict[str, object]:
    half = spec.half_km
    lo = round(max(topology.CORRIDOR_START_KM, km - half), 3)
    hi = round(min(topology.CORRIDOR_END_KM, km + half), 3)
    if hi <= lo:
        hi = round(min(topology.CORRIDOR_END_KM, lo + 0.1), 3)
    duration = spec.clearance_min + spec.work_min
    end = _wrapped_end(start, duration)
    start_f = float(start)
    ready = start_f + float(spec.clearance_min)
    work_end = ready + float(spec.work_min)
    site = round(km, 3)
    earth = spec.earth
    block_type = defects.BlockType.POWER_AND_TRAFFIC if earth else defects.BlockType.TRAFFIC_ONLY
    timeline: dict[str, object] = {
        "start_min": start_f,
        "end_min": float(end if end >= start else end + _MINUTES_PER_DAY),
        "transit_min": 0.0,
        "clearance_min": spec.clearance_min,
        "work_min": float(spec.work_min),
        "arrive_min": start_f,
        "protection_ready_min": ready,
        "work_start_min": ready,
        "work_end_min": work_end,
        "return_start_min": work_end,
        "power_block": earth,
        "machines": [],
        "worksite_start_km": lo,
        "worksite_end_km": hi,
        "track_line": lines[0],
        "power_sector_id": sector_id,
        "block_type": block_type.value,
    }
    machines: list[dict[str, object]] = [
        {
            "machine_type": "EMERGENCY_GANG",
            "unit_index": 1,
            "depot_km": site,
            "speed_kmh": 0.0,
            "transit_min": 0.0,
            "site_km": site,
            "path_offset_km": 0.0,
        }
    ]
    return {
        "bundle_id": f"EMG-{kind}-{km:.1f}",
        "defect_ids": [],
        "section_id": topology.get_section_id_at_km(km),
        "track_line": lines[0],
        "block_type": block_type.value,
        "power_sector_id": sector_id,
        "worksite_start_km": lo,
        "worksite_end_km": hi,
        "closed_start_km": lo,
        "closed_end_km": hi,
        "protected_lines": list(lines),
        "approach_km": _PROTECTION_APPROACH_KM,
        "stop_approaching": True,
        "start_min": start,
        "end_min": end,
        "start_hhmm": timetable.minute_to_hhmm(start),
        "end_hhmm": timetable.minute_to_hhmm(end),
        "earth_required": earth,
        "earth_up_km": lo,
        "earth_down_km": hi,
        "immediate": True,
        "emergency": True,
        "emergency_type": kind,
        "emergency_label": spec.label,
        "emergency_short": spec.short,
        "emergency_km": round(km, 3),
        "timeline": timeline,
        "machines": machines,
        "solver_status": "EMERGENCY",
        "chaos_score": None,
        "delayed_trains": [],
        "staff_count": spec.staff,
        "machines_label": "Emergency gang",
        "window_label": "EMERGENCY",
        "report_jitter_min": 0,
    }


def _post_emergency_orders(record: dict[str, object]) -> None:
    ref = str(record["possession_ref"])
    label = str(record["emergency_label"])
    km = float(str(record["emergency_km"]))
    protected = record.get("protected_lines")
    line_names = [str(line) for line in protected] if isinstance(protected, list) else [str(record["track_line"])]
    lines = ", ".join(line_names)
    approach = float(str(record["approach_km"]))
    lo = float(str(record["closed_start_km"]))
    hi = float(str(record["closed_end_km"]))
    extra = " This order supersedes the previous emergency." if record.get("supersedes") else ""
    station_text = (
        f"{label} at KM {km:.1f} on {lines}. "
        "Stop approaching trains. Signals at danger. "
        f"Protect {approach:.1f} km either side of KM {lo:.1f}–{hi:.1f}. "
        f"Engineering is proceeding now.{extra}"
    )
    start_station, end_station = _section_end_stations(record)
    persistence.post_block_message(
        ref,
        "EMERGENCY_ORDER",
        _CONSOLE_CONTROLLER,
        f"{_ROLE_STATION} {start_station.code}",
        station_text,
        sequence=0,
    )
    persistence.post_block_message(
        ref,
        "EMERGENCY_ORDER",
        _CONSOLE_CONTROLLER,
        f"{_ROLE_STATION} {end_station.code}",
        station_text,
        sequence=1,
    )
    persistence.post_block_message(
        ref,
        "EMERGENCY_ORDER",
        _CONSOLE_CONTROLLER,
        _ROLE_GANG,
        (
            f"Attend {label} at KM {km:.1f} on {lines} immediately. "
            "Other work on this section is paused. Repair this site only."
        ),
        sequence=2,
    )
    persistence.post_block_message(
        ref,
        "EMERGENCY_ORDER",
        _CONSOLE_CONTROLLER,
        _ROLE_OFFICE,
        (
            f"Emergency possession {ref}. {label} at KM {km:.1f} on {lines}. "
            "Line closed. Planned work stays paused until you replan it."
        ),
        sequence=3,
    )
    if bool(record.get("earth_required")):
        tpc_text = (
            f"OHE breakdown at KM {km:.1f} on {lines}. "
            "Isolate and earth the feeder before the gang works. "
            "Confirm the power block to the section controller."
        )
    else:
        tpc_text = (
            f"Traffic emergency at KM {km:.1f} on {lines}. The corridor is 25 kV. "
            "No isolation is requested yet. Stand by if engineering asks for a power block."
        )
    persistence.post_block_message(
        ref,
        "EMERGENCY_ORDER",
        _CONSOLE_CONTROLLER,
        _ROLE_TPC,
        tpc_text,
        sequence=4,
    )


@app.post("/api/simulate-emergency")
def simulate_emergency(
    payload: dict[str, object] | None = Body(None),
) -> dict[str, object]:
    """Protect a live site, hold approaching trains, and start that repair now.

    The chainage comes from the controller. A short operating day does not
    refuse the protection: the line stays closed until the site is cleared.
    """
    body = payload or {}
    kind, spec = _parse_emergency_kind(body)
    km = _parse_emergency_km(body)
    line = _parse_emergency_line(body)
    lines, sector_id = _emergency_lines(spec, km, line)
    clock = timetable.wrap_minute(_server_minute())
    record = _build_emergency_record(kind, spec, km, lines, sector_id, int(clock))
    superseded = bool(SYSTEM_STATE.get("emergency_active"))
    previous = SYSTEM_STATE.get("possession")
    paused: dict[str, object] | None
    if superseded and isinstance(previous, dict):
        _note_superseded(previous, spec.label)
        record["supersedes"] = str(previous.get("possession_ref") or "")
        stored = SYSTEM_STATE.get("paused_work")
        paused = stored if isinstance(stored, dict) else None
    else:
        paused = _pause_other_work(spec.label)
    if isinstance(paused, dict):
        record["paused_work"] = paused
    plan_type = str(SYSTEM_STATE.get("active_plan") or "PUNCTUALITY_FIRST")
    _lock_possession(record, plan_type, str(record["bundle_id"]), emergency=True, outcome="SANCTIONED")
    _post_emergency_orders(record)
    rows = timetable.get_regulated_positions(clock, _active_possessions())
    execution = _phase_snapshot(clock, rows)
    held = [str(row["train_id"]) for row in rows if row.get("held")]
    line_text = ", ".join(lines)
    message = (
        f"{spec.label} at KM {km:.1f} on {line_text}. "
        f"Approaching trains are held {_PROTECTION_APPROACH_KM:.1f} km short. "
        "Station masters, engineering, and the traction power controller have been told."
    )
    if isinstance(paused, dict) and paused.get("bundle_id"):
        message += f" Planned block {paused.get('bundle_id')} is paused."
    elif superseded:
        message += " The previous emergency order is superseded."
    return {
        "status": "SANCTIONED",
        "event": "EMERGENCY_POSSESSION",
        "emergency_type": kind,
        "emergency_label": spec.label,
        "message": message,
        "paused_work": paused,
        "held_trains": held,
        "possession": record,
        "execution": execution,
        "coa_dossier_id": SYSTEM_STATE.get("coa_dossier_id"),
        "system_state": SYSTEM_STATE,
        "dispatched_plan": {
            "feasible": True,
            "profile": "EMERGENCY",
            "status": "SANCTIONED",
            "chainage_km": km,
        },
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


@app.get("/api/messages")
def get_messages(after_id: int = 0, limit: int = 100) -> dict[str, object]:
    """Board messages after `after_id`, oldest first. Day labels only; no calendar dates."""
    sim_clock.advance()
    tick_block_lifecycle()
    bounded = 100 if limit < 1 or limit > 100 else limit
    rows = persistence.list_block_messages(after_id, bounded)
    return {"after_id": after_id if after_id > 0 else 0, "limit": bounded, "messages": rows}


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


@app.get("/api/maintenance-log")
def get_maintenance_log(limit: int = 200) -> dict[str, object]:
    records = backlog_state.get_completed_log(limit)
    for record in records:
        record["department"] = _SOURCE_BY_DEFECT_TYPE.get(
            defects.DefectType(record["defect_type"]),
            "TMS",
        )
        record["start_hhmm"] = timetable.minute_to_hhmm(record["start_min"])
        cleared_hhmm = _hhmm_from_stamp(record.get("completed_at"))
        record["end_hhmm"] = cleared_hhmm or timetable.minute_to_hhmm(record["end_min"])
        record["cleared_hhmm"] = record["end_hhmm"]
    return {"records": records, "total_completed": len(records), "summary": metrics.summarize_completed_work(records)}


@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/assets/indian-railways-logo.png")
def indian_railways_logo() -> FileResponse:
    return FileResponse("assets/indian-railways-logo.png", media_type="image/png")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)