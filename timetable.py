"""
timetable.py
====================================================================
Central Railway Working Timetable (WTT) for CSMT (KM 0.0) → Pune (KM 192.0).

Category 1 — full-corridor mainline (CSMT ↔ Pune), cycles C1–C6
Category 2 — suburban shuttles CKL1–CKL4 (CSMT ↔ Karjat) and PLL1–PLL4
            (Pune ↔ Lonavala)
Category 3 — Bhor Ghat bankers BNK1–BNK3 on MID, Sector 3 (KM 100–128)

Station chainage, sections, and running-line names are imported from
topology.py. This module does not contain solver constraints.
====================================================================
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from typing import Final, Sequence

import topology
from topology import (
    CORRIDOR_END_KM,
    CORRIDOR_START_KM,
    LINE_DN,
    LINE_DN_FAST,
    LINE_DN_SLOW,
    LINE_MID,
    LINE_UP,
    LINE_UP_FAST,
    LINE_UP_SLOW,
    SECTION_1_ID,
    STATIONS,
    StationNode,
    get_section_id_at_km,
    get_station_by_code,
)

# One operating day, in minutes. The live clock is sim_clock.py.
TOTAL_SIMULATION_MINUTES: Final[float] = 1440.0

STATUS_RUNNING: Final[str] = "RUNNING"
STATUS_STOPPED: Final[str] = "STOPPED"
STATUS_HELD: Final[str] = "HELD"
STATUS_DIVERTED: Final[str] = "DIVERTED"
STATUS_BANKER_PUSH: Final[str] = "BANKER_PUSH"
STATUS_BANKER_RETURN: Final[str] = "BANKER_RETURN"
_CROSSOVER_SLOW_KM: Final[float] = 1.0
_TSLW_HEADWAY_MIN: Final[float] = 3.0
_TSLW_SPEED_KMH: Final[float] = 20.0
QUEUE_GAP_KM: Final[float] = 0.65

TYPE_PREMIUM: Final[str] = "PREMIUM"
TYPE_SUPERFAST: Final[str] = "SUPERFAST"
TYPE_EXPRESS: Final[str] = "EXPRESS"
TYPE_FREIGHT: Final[str] = "FREIGHT"
TYPE_LOCAL: Final[str] = "LOCAL"
TYPE_BANKER: Final[str] = "BANKER"

DIR_DN: Final[str] = "DN"
DIR_UP: Final[str] = "UP"

KM_CSMT: Final[float] = 0.0
KM_KYN: Final[float] = 54.0
KM_KJT: Final[float] = 100.0
KM_LNL: Final[float] = 128.0
KM_PUNE: Final[float] = 192.0

BANKER_PUSH_MIN: Final[float] = 45.0
BANKER_UNCOUPLE_MIN: Final[float] = 10.0
BANKER_RETURN_MIN: Final[float] = 35.0
BANKER_DEPOT_MIN: Final[float] = 15.0
BANKER_CYCLE_MIN: Final[float] = (
    BANKER_PUSH_MIN + BANKER_UNCOUPLE_MIN + BANKER_RETURN_MIN + BANKER_DEPOT_MIN
)

CKL_TRIP_MIN: Final[float] = 65.0
PLL_TRIP_MIN: Final[float] = 70.0
LOCAL_DWELL_MIN: Final[float] = 1.0
MAINLINE_DWELL_MIN: Final[float] = 2.0


def wrap_minute(minute: float) -> float:
    """Fold an absolute minute onto [0, TOTAL_SIMULATION_MINUTES)."""
    return float(minute) % TOTAL_SIMULATION_MINUTES


def minute_to_hhmm(minute: float) -> str:
    folded = wrap_minute(minute)
    hours = int(folded) // 60
    mins = int(folded) % 60
    return f"{hours:02d}:{mins:02d}"


# ---------------------------------------------------------------------------
# Frozen timetable dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class StopCall:
    """A scheduled station call (origin, intermediate, or destination)."""

    station_code: str
    km: float
    arrival_min: float
    departure_min: float

    @property
    def dwell_min(self) -> float:
        return max(0.0, self.departure_min - self.arrival_min)


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """Half-open occupancy window on the unwrapped operating clock."""

    start_min: float
    end_min: float

    def duration_min(self) -> float:
        return max(0.0, self.end_min - self.start_min)

    def contains(self, minute: float) -> bool:
        return _unwrapped_in_window(float(minute), self.start_min, self.end_min)


@dataclass(frozen=True, slots=True)
class Waypoint:
    """A timed chainage sample used for linear interpolation."""

    minute: float
    km: float
    status: str
    line: str


@dataclass(frozen=True, slots=True)
class TrainTrajectory:
    """One cycle/trip of a rake, with stop calls and interpolation waypoints."""

    train_id: str
    service_name: str
    train_type: str
    direction: str
    cycle_id: str
    origin_code: str
    dest_code: str
    origin_km: float
    dest_km: float
    stop_calls: tuple[StopCall, ...]
    waypoints: tuple[Waypoint, ...]
    window: TimeWindow
    uses_mid_ghat: bool
    assigned_mission: str

    @property
    def min_km(self) -> float:
        return min(self.origin_km, self.dest_km)

    @property
    def max_km(self) -> float:
        return max(self.origin_km, self.dest_km)

    def line_at_km(self, km: float) -> str:
        return resolve_running_line(self.direction, km, self.train_type, self.uses_mid_ghat)


# ---------------------------------------------------------------------------
# Clock helpers
# ---------------------------------------------------------------------------

def _unwrapped_in_window(minute: float, start_min: float, end_min: float) -> bool:
    if end_min <= start_min:
        return False
    span = end_min - start_min
    if span >= TOTAL_SIMULATION_MINUTES:
        return True
    offset = (wrap_minute(minute) - wrap_minute(start_min)) % TOTAL_SIMULATION_MINUTES
    return offset < span


def _windows_overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    if a_end <= a_start or b_end <= b_start:
        return False
    a_span = a_end - a_start
    b_span = b_end - b_start
    if a_span >= TOTAL_SIMULATION_MINUTES or b_span >= TOTAL_SIMULATION_MINUTES:
        return True
    a0 = wrap_minute(a_start)
    b0 = wrap_minute(b_start)
    a_points = (a0, a0 + a_span)
    b_points = (b0, b0 + b_span)
    b_shift = (b0, b0 + b_span - TOTAL_SIMULATION_MINUTES)
    a_shift = (a0, a0 + a_span - TOTAL_SIMULATION_MINUTES)
    return (
        a_points[0] < b_points[1] and b_points[0] < a_points[1]
        or a_points[0] < b_shift[1] and b_shift[0] < a_points[1]
        or a_shift[0] < b_points[1] and b_points[0] < a_shift[1]
    )


def _map_to_unwrapped(minute: float, start_min: float, end_min: float) -> float:
    mapped = start_min + (wrap_minute(minute) - wrap_minute(start_min)) % TOTAL_SIMULATION_MINUTES
    if mapped > end_min:
        return end_min
    return mapped


def _ranges_overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    lo_a, hi_a = (a0, a1) if a0 <= a1 else (a1, a0)
    lo_b, hi_b = (b0, b1) if b0 <= b1 else (b1, b0)
    return lo_a <= hi_b and lo_b <= hi_a


# ---------------------------------------------------------------------------
# Topology-backed path and line resolution
# ---------------------------------------------------------------------------

def resolve_running_line(
    direction: str,
    km: float,
    train_type: str,
    uses_mid_ghat: bool,
) -> str:
    chainage = min(max(float(km), CORRIDOR_START_KM), CORRIDOR_END_KM)
    if uses_mid_ghat and KM_KJT <= chainage <= KM_LNL:
        return LINE_MID
    if train_type == TYPE_BANKER:
        return LINE_MID
    section_id = get_section_id_at_km(chainage)
    is_dn = direction == DIR_DN
    if section_id == SECTION_1_ID:
        if train_type == TYPE_LOCAL:
            return LINE_DN_SLOW if is_dn else LINE_UP_SLOW
        return LINE_DN_FAST if is_dn else LINE_UP_FAST
    return LINE_DN if is_dn else LINE_UP


def _stations_between(origin_km: float, dest_km: float) -> tuple[StationNode, ...]:
    if dest_km >= origin_km:
        chosen = [station for station in STATIONS if origin_km <= station.km <= dest_km]
        chosen.sort(key=lambda s: s.km)
        return tuple(chosen)
    chosen = [station for station in STATIONS if dest_km <= station.km <= origin_km]
    chosen.sort(key=lambda s: s.km, reverse=True)
    return tuple(chosen)


_LOCAL_STOP_CODES: frozenset[str] = frozenset(
    {"CSMT", "DR", "TNA", "KYN", "BUD", "NRL", "KJT", "LNL", "TGN", "DEHR", "CCH", "PMP", "SVJR", "PUNE"}
)


def _allocate_stop_calls(
    origin_km: float,
    dest_km: float,
    depart_min: float,
    arrive_min: float,
    stopping_codes: frozenset[str],
    dwell_min: float,
) -> tuple[StopCall, ...]:
    stations = _stations_between(origin_km, dest_km)
    if len(stations) < 2:
        origin = get_station_by_code("CSMT" if origin_km <= dest_km else "PUNE")
        dest = get_station_by_code("PUNE" if origin_km <= dest_km else "CSMT")
        stations = (origin, dest)

    moving_legs = max(1, len(stations) - 1)
    total_dwell = dwell_min * sum(
        1 for station in stations[1:-1] if station.code in stopping_codes
    )
    moving_budget = max(1.0, (arrive_min - depart_min) - total_dwell)
    distances = [
        abs(stations[index + 1].km - stations[index].km) for index in range(moving_legs)
    ]
    distance_sum = sum(distances) or 1.0

    calls: list[StopCall] = []
    cursor = float(depart_min)
    for index, station in enumerate(stations):
        is_origin = index == 0
        is_dest = index == len(stations) - 1
        if is_origin:
            arrival = cursor
            departure = cursor
        elif is_dest:
            arrival = float(arrive_min)
            departure = arrival
        else:
            arrival = cursor
            dwell = dwell_min if station.code in stopping_codes else 0.0
            departure = arrival + dwell
        calls.append(
            StopCall(
                station_code=station.code,
                km=station.km,
                arrival_min=arrival,
                departure_min=departure,
            )
        )
        if not is_dest:
            hop = moving_budget * (distances[index] / distance_sum)
            cursor = departure + hop
    return tuple(calls)


def _waypoints_from_calls(
    calls: Sequence[StopCall],
    direction: str,
    train_type: str,
    uses_mid_ghat: bool,
    running_status: str,
) -> tuple[Waypoint, ...]:
    points: list[Waypoint] = []
    for index, call in enumerate(calls):
        line = resolve_running_line(direction, call.km, train_type, uses_mid_ghat)
        is_origin = index == 0
        is_dest = index == len(calls) - 1
        if is_origin:
            points.append(Waypoint(call.departure_min, call.km, running_status, line))
            continue
        if call.dwell_min > 0.0 and not is_dest:
            points.append(Waypoint(call.arrival_min, call.km, STATUS_STOPPED, line))
            points.append(Waypoint(call.departure_min, call.km, running_status, line))
        elif is_dest:
            points.append(Waypoint(call.arrival_min, call.km, running_status, line))
        else:
            points.append(Waypoint(call.arrival_min, call.km, running_status, line))
    return tuple(points)


def _interpolate_at(trajectory: TrainTrajectory, current_min: float) -> tuple[float, str, str, float]:
    waypoints = trajectory.waypoints
    if len(waypoints) == 1:
        wp = waypoints[0]
        return wp.km, wp.line, wp.status, 0.0

    t = _map_to_unwrapped(current_min, trajectory.window.start_min, trajectory.window.end_min)
    if t < waypoints[0].minute:
        wp = waypoints[0]
        return wp.km, wp.line, wp.status, 0.0
    if t >= waypoints[-1].minute:
        wp = waypoints[-1]
        return wp.km, wp.line, wp.status, 0.0

    last_index = len(waypoints) - 2
    for index in range(len(waypoints) - 1):
        left = waypoints[index]
        right = waypoints[index + 1]
        if t < left.minute or t > right.minute:
            continue
        if t == right.minute and index < last_index:
            continue
        dt = right.minute - left.minute
        if dt <= 1e-9:
            km = right.km
            speed = 0.0
        else:
            ratio = (t - left.minute) / dt
            km = left.km + (right.km - left.km) * ratio
            speed = abs(right.km - left.km) / dt * 60.0
        if abs(right.km - left.km) <= 1e-9:
            status = left.status if left.status == STATUS_STOPPED else STATUS_STOPPED
            speed = 0.0
        else:
            status = left.status if left.status != STATUS_STOPPED else right.status
        line = resolve_running_line(
            trajectory.direction, km, trajectory.train_type, trajectory.uses_mid_ghat
        )
        return km, line, status, speed

    wp = waypoints[-1]
    return wp.km, wp.line, wp.status, 0.0


# ---------------------------------------------------------------------------
# Category 1 — full-corridor mainline services (sheet minutes)
# ---------------------------------------------------------------------------

_STOPS_SUPERFAST: frozenset[str] = frozenset({"CSMT", "DR", "TNA", "KYN", "KJT", "LNL", "PUNE"})
_STOPS_EXPRESS: frozenset[str] = frozenset(
    {"CSMT", "DR", "TNA", "KYN", "BUD", "NRL", "KJT", "LNL", "TGN", "CCH", "SVJR", "PUNE"}
)
_STOPS_PREMIUM: frozenset[str] = frozenset({"CSMT", "KYN", "PUNE"})
_STOPS_FREIGHT: frozenset[str] = frozenset({"CSMT", "KYN", "KJT", "LNL", "PUNE"})
_STOPS_LOCAL: frozenset[str] = _LOCAL_STOP_CODES

# Pinned Karjat minutes equal the matching banker push departure (master table).
_KJT_PIN_DN: dict[str, tuple[float, ...]] = {
    "12123": (145.0, 385.0, 625.0, 865.0, 1105.0, 1345.0),
    "FRT-PET-801": (170.0, 410.0, 650.0, 890.0, 1130.0, 1370.0),
    "11007": (190.0, 430.0, 670.0, 910.0, 1150.0, 1390.0),
}

_LNL_AFTER_GHAT: dict[str, float] = {
    "12123": 50.0,
    "22225": 42.0,
    "11007": 55.0,
    "FRT-PET-801": 70.0,
    "12124": 50.0,
    "11010": 55.0,
    "FRT-CNT-802": 70.0,
    "FRT-EMP-803": 75.0,
}

_GHAT_UNASSISTED_MIN: dict[str, float] = {
    "22225": 32.0,
    "12124": 45.0,
    "11010": 45.0,
    "FRT-CNT-802": 40.0,
    "FRT-EMP-803": 45.0,
}

_CSMT_KJT_UNASSISTED_MIN: dict[str, float] = {
    "22225": 70.0,
    "12124": 85.0,
    "11010": 85.0,
    "FRT-CNT-802": 95.0,
    "FRT-EMP-803": 90.0,
}

MAINLINE_SERVICES: Final[tuple[dict[str, object], ...]] = (
    {
        "train_id": "12123",
        "service_name": "Deccan Queen",
        "train_type": TYPE_SUPERFAST,
        "direction": DIR_DN,
        "origin_code": "CSMT",
        "dest_code": "PUNE",
        "departures": (60.0, 300.0, 540.0, 780.0, 1020.0, 1260.0),
        "stopping": _STOPS_SUPERFAST,
        "uses_mid_ghat": True,
    },
    {
        "train_id": "FRT-PET-801",
        "service_name": "Petrol",
        "train_type": TYPE_FREIGHT,
        "direction": DIR_DN,
        "origin_code": "CSMT",
        "dest_code": "PUNE",
        "departures": (75.0, 315.0, 555.0, 795.0, 1035.0, 1275.0),
        "stopping": _STOPS_FREIGHT,
        "uses_mid_ghat": True,
    },
    {
        "train_id": "22225",
        "service_name": "Vande Bharat",
        "train_type": TYPE_PREMIUM,
        "direction": DIR_DN,
        "origin_code": "CSMT",
        "dest_code": "PUNE",
        "departures": (90.0, 330.0, 570.0, 810.0, 1050.0, 1290.0),
        "stopping": _STOPS_PREMIUM,
        "uses_mid_ghat": False,
    },
    {
        "train_id": "11007",
        "service_name": "Deccan Express",
        "train_type": TYPE_EXPRESS,
        "direction": DIR_DN,
        "origin_code": "CSMT",
        "dest_code": "PUNE",
        "departures": (105.0, 345.0, 585.0, 825.0, 1065.0, 1305.0),
        "stopping": _STOPS_EXPRESS,
        "uses_mid_ghat": True,
    },
    {
        "train_id": "FRT-CNT-802",
        "service_name": "Container",
        "train_type": TYPE_FREIGHT,
        "direction": DIR_UP,
        "origin_code": "PUNE",
        "dest_code": "CSMT",
        "departures": (60.0, 300.0, 540.0, 780.0, 1035.0, 1260.0),
        "stopping": _STOPS_FREIGHT,
        "uses_mid_ghat": False,
    },
    {
        "train_id": "12124",
        "service_name": "Deccan Queen Ret",
        "train_type": TYPE_SUPERFAST,
        "direction": DIR_UP,
        "origin_code": "PUNE",
        "dest_code": "CSMT",
        "departures": (75.0, 315.0, 555.0, 795.0, 1035.0, 1275.0),
        "stopping": _STOPS_SUPERFAST,
        "uses_mid_ghat": False,
    },
    {
        "train_id": "11010",
        "service_name": "Sinhagad Exp",
        "train_type": TYPE_EXPRESS,
        "direction": DIR_UP,
        "origin_code": "PUNE",
        "dest_code": "CSMT",
        "departures": (105.0, 345.0, 585.0, 825.0, 1065.0, 1305.0),
        "stopping": _STOPS_EXPRESS,
        "uses_mid_ghat": False,
    },
    {
        "train_id": "FRT-EMP-803",
        "service_name": "Freight",
        "train_type": TYPE_FREIGHT,
        "direction": DIR_UP,
        "origin_code": "PUNE",
        "dest_code": "CSMT",
        "departures": (120.0, 360.0, 600.0, 840.0, 1080.0, 1320.0),
        "stopping": _STOPS_FREIGHT,
        "uses_mid_ghat": False,
    },
)


def _stitched_mainline_calls(
    depart_min: float,
    stopping: frozenset[str],
    kjt_min: float | None,
    ghat_min: float,
    after_ghat_min: float,
    cstmt_kjt_min: float,
    direction: str,
) -> tuple[StopCall, ...]:
    dwell = MAINLINE_DWELL_MIN
    if direction == DIR_DN:
        if kjt_min is None:
            kjt_arr = depart_min + cstmt_kjt_min
        else:
            kjt_arr = kjt_min
        lnl_arr = kjt_arr + ghat_min
        pune_arr = lnl_arr + after_ghat_min
        leg_a = _allocate_stop_calls(KM_CSMT, KM_KJT, depart_min, kjt_arr, stopping, dwell)
        leg_b = _allocate_stop_calls(KM_KJT, KM_LNL, kjt_arr, lnl_arr, stopping, dwell)
        leg_c = _allocate_stop_calls(KM_LNL, KM_PUNE, lnl_arr, pune_arr, stopping, dwell)
        return _merge_legs(leg_a, leg_b, leg_c)

    pune_lnl = after_ghat_min
    lnl_dep = depart_min + pune_lnl
    kjt_arr = lnl_dep + ghat_min
    csmt_arr = kjt_arr + cstmt_kjt_min
    leg_a = _allocate_stop_calls(KM_PUNE, KM_LNL, depart_min, lnl_dep, stopping, dwell)
    leg_b = _allocate_stop_calls(KM_LNL, KM_KJT, lnl_dep, kjt_arr, stopping, dwell)
    leg_c = _allocate_stop_calls(KM_KJT, KM_CSMT, kjt_arr, csmt_arr, stopping, dwell)
    return _merge_legs(leg_a, leg_b, leg_c)


def _merge_legs(*legs: Sequence[StopCall]) -> tuple[StopCall, ...]:
    merged: list[StopCall] = []
    for leg in legs:
        if not leg:
            continue
        if not merged:
            merged.extend(leg)
            continue
        junction = leg[0]
        prev = merged[-1]
        merged[-1] = StopCall(
            station_code=prev.station_code,
            km=prev.km,
            arrival_min=prev.arrival_min,
            departure_min=max(prev.departure_min, junction.departure_min),
        )
        merged.extend(leg[1:])
    return tuple(merged)


def _build_mainline_trajectories() -> list[TrainTrajectory]:
    trajectories: list[TrainTrajectory] = []
    for service in MAINLINE_SERVICES:
        train_id = str(service["train_id"])
        origin_code = str(service["origin_code"])
        dest_code = str(service["dest_code"])
        origin_km = get_station_by_code(origin_code).km
        dest_km = get_station_by_code(dest_code).km
        direction = str(service["direction"])
        train_type = str(service["train_type"])
        uses_mid = bool(service["uses_mid_ghat"])
        stopping = service["stopping"]
        assert isinstance(stopping, frozenset)
        departures = service["departures"]
        assert isinstance(departures, tuple)
        pins = _KJT_PIN_DN.get(train_id)
        for cycle_index, depart_min in enumerate(departures):
            cycle_id = f"C{cycle_index + 1}"
            kjt_min = pins[cycle_index] if pins is not None else None
            ghat_min = BANKER_PUSH_MIN if uses_mid else _GHAT_UNASSISTED_MIN[train_id]
            calls = _stitched_mainline_calls(
                depart_min=float(depart_min),
                stopping=stopping,
                kjt_min=kjt_min,
                ghat_min=ghat_min,
                after_ghat_min=_LNL_AFTER_GHAT[train_id],
                cstmt_kjt_min=_CSMT_KJT_UNASSISTED_MIN.get(train_id, 85.0),
                direction=direction,
            )
            waypoints = _waypoints_from_calls(
                calls, direction, train_type, uses_mid, STATUS_RUNNING
            )
            window = TimeWindow(calls[0].departure_min, calls[-1].arrival_min)
            trajectories.append(
                TrainTrajectory(
                    train_id=f"{train_id}_{cycle_id}",
                    service_name=str(service["service_name"]),
                    train_type=train_type,
                    direction=direction,
                    cycle_id=cycle_id,
                    origin_code=origin_code,
                    dest_code=dest_code,
                    origin_km=origin_km,
                    dest_km=dest_km,
                    stop_calls=calls,
                    waypoints=waypoints,
                    window=window,
                    uses_mid_ghat=uses_mid,
                    assigned_mission=f"{train_id} {cycle_id} {origin_code}->{dest_code}",
                )
            )
    return trajectories


# ---------------------------------------------------------------------------
# Category 2 — suburban shuttle locals (handwritten minutes in parentheses)
# ---------------------------------------------------------------------------

CKL_RAKES: Final[tuple[dict[str, object], ...]] = (
    {
        "train_id": "CKL1",
        "service_name": "CSMT-Karjat Local 1",
        "direction": DIR_DN,
        "origin_code": "CSMT",
        "dest_code": "KJT",
        "departures": (
            240.0, 340.0, 440.0, 540.0, 640.0, 740.0,
            840.0, 940.0, 1040.0, 1140.0, 1240.0, 1340.0,
        ),
    },
    {
        "train_id": "CKL2",
        "service_name": "CSMT-Karjat Local 2",
        "direction": DIR_DN,
        "origin_code": "CSMT",
        "dest_code": "KJT",
        "departures": (
            285.0, 385.0, 485.0, 585.0, 685.0, 785.0,
            885.0, 985.0, 1085.0, 1185.0, 1285.0, 1385.0,
        ),
    },
    {
        "train_id": "CKL3",
        "service_name": "Karjat-CSMT Local 3",
        "direction": DIR_UP,
        "origin_code": "KJT",
        "dest_code": "CSMT",
        "departures": (
            240.0, 340.0, 440.0, 540.0, 640.0, 740.0,
            840.0, 940.0, 1040.0, 1140.0, 1240.0, 1340.0,
        ),
    },
    {
        "train_id": "CKL4",
        "service_name": "Karjat-CSMT Local 4",
        "direction": DIR_UP,
        "origin_code": "KJT",
        "dest_code": "CSMT",
        "departures": (
            285.0, 385.0, 485.0, 585.0, 685.0, 785.0,
            885.0, 985.0, 1085.0, 1185.0, 1285.0, 1385.0,
        ),
    },
)

PLL_RAKES: Final[tuple[dict[str, object], ...]] = (
    {
        "train_id": "PLL1",
        "service_name": "Pune-Lonavala Local 1",
        "direction": DIR_UP,
        "origin_code": "PUNE",
        "dest_code": "LNL",
        "departures": (
            240.0, 325.0, 410.0, 495.0, 580.0, 670.0, 750.0,
            835.0, 920.0, 1005.0, 1090.0, 1175.0, 1260.0, 1345.0,
        ),
    },
    {
        "train_id": "PLL2",
        "service_name": "Pune-Lonavala Local 2",
        "direction": DIR_UP,
        "origin_code": "PUNE",
        "dest_code": "LNL",
        "departures": (
            265.0, 360.0, 445.0, 530.0, 615.0, 700.0, 785.0,
            870.0, 955.0, 1040.0, 1125.0, 1210.0, 1295.0, 1380.0,
        ),
    },
    {
        "train_id": "PLL3",
        "service_name": "Lonavala-Pune Local 3",
        "direction": DIR_DN,
        "origin_code": "LNL",
        "dest_code": "PUNE",
        "departures": (
            240.0, 325.0, 410.0, 495.0, 580.0, 665.0, 750.0,
            835.0, 920.0, 1005.0, 1090.0, 1175.0, 1260.0, 1345.0,
        ),
    },
    {
        "train_id": "PLL4",
        "service_name": "Lonavala-Pune Local 4",
        "direction": DIR_DN,
        "origin_code": "LNL",
        "dest_code": "PUNE",
        "departures": (
            275.0, 360.0, 445.0, 530.0, 615.0, 700.0, 785.0,
            870.0, 955.0, 1040.0, 1125.0, 1210.0, 1295.0, 1380.0,
        ),
    },
)


def _build_local_trajectories(
    rakes: Sequence[dict[str, object]],
    trip_min: float,
) -> list[TrainTrajectory]:
    trajectories: list[TrainTrajectory] = []
    for rake in rakes:
        train_id = str(rake["train_id"])
        origin_code = str(rake["origin_code"])
        dest_code = str(rake["dest_code"])
        origin_km = get_station_by_code(origin_code).km
        dest_km = get_station_by_code(dest_code).km
        direction = str(rake["direction"])
        departures = rake["departures"]
        assert isinstance(departures, tuple)
        for cycle_index, depart_min in enumerate(departures):
            cycle_id = f"C{cycle_index + 1}"
            arrive_min = float(depart_min) + trip_min
            calls = _allocate_stop_calls(
                origin_km,
                dest_km,
                float(depart_min),
                arrive_min,
                _STOPS_LOCAL,
                LOCAL_DWELL_MIN,
            )
            waypoints = _waypoints_from_calls(
                calls, direction, TYPE_LOCAL, False, STATUS_RUNNING
            )
            trajectories.append(
                TrainTrajectory(
                    train_id=f"{train_id}_{cycle_id}",
                    service_name=str(rake["service_name"]),
                    train_type=TYPE_LOCAL,
                    direction=direction,
                    cycle_id=cycle_id,
                    origin_code=origin_code,
                    dest_code=dest_code,
                    origin_km=origin_km,
                    dest_km=dest_km,
                    stop_calls=calls,
                    waypoints=waypoints,
                    window=TimeWindow(float(depart_min), arrive_min),
                    uses_mid_ghat=False,
                    assigned_mission=f"{train_id} {cycle_id} {origin_code}->{dest_code}",
                )
            )
    return trajectories


# ---------------------------------------------------------------------------
# Category 3 — Bhor Ghat bankers BNK1–BNK3 (master timetable C1–C6)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class BankerCyclePlan:
    cycle_id: str
    banker_id: str
    push_dep_min: float
    push_arr_min: float
    return_dep_min: float
    return_arr_min: float
    ready_min: float
    mission: str


def _banker_plan(
    cycle_id: str,
    banker_id: str,
    push_dep: float,
    mission: str,
) -> BankerCyclePlan:
    push_arr = push_dep + BANKER_PUSH_MIN
    return_dep = push_arr + BANKER_UNCOUPLE_MIN
    return_arr = return_dep + BANKER_RETURN_MIN
    ready = return_arr + BANKER_DEPOT_MIN
    return BankerCyclePlan(
        cycle_id=cycle_id,
        banker_id=banker_id,
        push_dep_min=push_dep,
        push_arr_min=push_arr,
        return_dep_min=return_dep,
        return_arr_min=return_arr,
        ready_min=ready,
        mission=mission,
    )


BANKER_MASTER: Final[tuple[BankerCyclePlan, ...]] = (
    # Cycles C1–C3 (Sector 3 master table)
    _banker_plan("C1", "BNK1", 145.0, "Push 12123 Deccan Queen"),
    _banker_plan("C1", "BNK2", 170.0, "Push FRT-PET-801 Freight"),
    _banker_plan("C1", "BNK3", 190.0, "Push 11007 Deccan Express"),
    _banker_plan("C2", "BNK1", 385.0, "Push 12123_C2"),
    _banker_plan("C2", "BNK2", 410.0, "Push FRT-PET-801_C2"),
    _banker_plan("C2", "BNK3", 430.0, "Push 11007_C2"),
    _banker_plan("C3", "BNK1", 625.0, "Push 12123_C3"),
    _banker_plan("C3", "BNK2", 650.0, "Push FRT-PET-801_C3"),
    _banker_plan("C3", "BNK3", 670.0, "Push 11007_C3"),
    # Cycles C4–C6
    _banker_plan("C4", "BNK1", 865.0, "Push 12123_C4"),
    _banker_plan("C4", "BNK2", 890.0, "Push FRT-PET-801_C4"),
    _banker_plan("C4", "BNK3", 910.0, "Push 11007_C4"),
    _banker_plan("C5", "BNK1", 1105.0, "Push 12123_C5"),
    _banker_plan("C5", "BNK2", 1130.0, "Push FRT-PET-801_C5"),
    _banker_plan("C5", "BNK3", 1150.0, "Push 11007_C5"),
    _banker_plan("C6", "BNK1", 1345.0, "Push 12123_C6"),
    _banker_plan("C6", "BNK2", 1370.0, "Push FRT-PET-801_C6"),
    _banker_plan("C6", "BNK3", 1390.0, "Push 11007_C6"),
)


def _build_banker_trajectories() -> list[TrainTrajectory]:
    trajectories: list[TrainTrajectory] = []
    kjt = get_station_by_code("KJT")
    lnl = get_station_by_code("LNL")
    for plan in BANKER_MASTER:
        if abs((plan.ready_min - plan.push_dep_min) - BANKER_CYCLE_MIN) > 1e-6:
            raise ValueError(
                f"{plan.banker_id} {plan.cycle_id} cycle length "
                f"{plan.ready_min - plan.push_dep_min} != {BANKER_CYCLE_MIN}"
            )
        calls = (
            StopCall("KJT", kjt.km, plan.push_dep_min, plan.push_dep_min),
            StopCall("LNL", lnl.km, plan.push_arr_min, plan.return_dep_min),
            StopCall("KJT", kjt.km, plan.return_arr_min, plan.ready_min),
        )
        waypoints = (
            Waypoint(plan.push_dep_min, kjt.km, STATUS_BANKER_PUSH, LINE_MID),
            Waypoint(plan.push_arr_min, lnl.km, STATUS_STOPPED, LINE_MID),
            Waypoint(plan.return_dep_min, lnl.km, STATUS_BANKER_RETURN, LINE_MID),
            Waypoint(plan.return_arr_min, kjt.km, STATUS_STOPPED, LINE_MID),
            Waypoint(plan.ready_min, kjt.km, STATUS_STOPPED, LINE_MID),
        )
        trajectories.append(
            TrainTrajectory(
                train_id=f"{plan.banker_id}_{plan.cycle_id}",
                service_name=f"Bhor Ghat Banker {plan.banker_id}",
                train_type=TYPE_BANKER,
                direction=DIR_DN,
                cycle_id=plan.cycle_id,
                origin_code="KJT",
                dest_code="KJT",
                origin_km=kjt.km,
                dest_km=lnl.km,
                stop_calls=calls,
                waypoints=waypoints,
                window=TimeWindow(plan.push_dep_min, plan.ready_min),
                uses_mid_ghat=True,
                assigned_mission=plan.mission,
            )
        )
    return trajectories


# ---------------------------------------------------------------------------
# Assemble the complete WTT
# ---------------------------------------------------------------------------

def build_timetable() -> tuple[TrainTrajectory, ...]:
    trips: list[TrainTrajectory] = []
    trips.extend(_build_mainline_trajectories())
    trips.extend(_build_local_trajectories(CKL_RAKES, CKL_TRIP_MIN))
    trips.extend(_build_local_trajectories(PLL_RAKES, PLL_TRIP_MIN))
    trips.extend(_build_banker_trajectories())
    trips.sort(key=lambda t: (t.window.start_min, t.train_id))
    return tuple(trips)


ALL_TRAJECTORIES: Final[tuple[TrainTrajectory, ...]] = build_timetable()


def get_all_trajectories() -> tuple[TrainTrajectory, ...]:
    return ALL_TRAJECTORIES


def iter_trajectories_by_type(train_type: str) -> tuple[TrainTrajectory, ...]:
    return tuple(t for t in ALL_TRAJECTORIES if t.train_type == train_type)


def _shift_trajectory(trajectory: TrainTrajectory, offset_min: float) -> TrainTrajectory:
    """Move one trip's clock by ``offset_min`` without changing its path."""
    offset = float(offset_min)
    calls = tuple(
        StopCall(
            station_code=call.station_code,
            km=call.km,
            arrival_min=call.arrival_min + offset,
            departure_min=call.departure_min + offset,
        )
        for call in trajectory.stop_calls
    )
    waypoints = tuple(
        Waypoint(
            minute=point.minute + offset,
            km=point.km,
            status=point.status,
            line=point.line,
        )
        for point in trajectory.waypoints
    )
    window = TimeWindow(
        start_min=trajectory.window.start_min + offset,
        end_min=trajectory.window.end_min + offset,
    )
    return replace(trajectory, stop_calls=calls, waypoints=waypoints, window=window)


def generate_freight_forecast(
    seed: int,
    jitter_min: float = 45.0,
) -> tuple[TrainTrajectory, ...]:
    """
    Forecast copy of the fixed freight WTT.

    Each existing FREIGHT trajectory is shifted by its own offset drawn from
    ``random.Random(seed)`` on ``[-jitter_min, +jitter_min]`` minutes. The
    draw is uniform and reproducible for a given seed. Passenger, local, and
    banker trajectories are not read and are not returned.

    The offset stands for forecast uncertainty: goods trains often run off
    their nominal slot. Chainage, line, and train identity stay on the
    published path; only the clock moves. Stop calls move with the waypoints
    so the shifted trip stays internally consistent.
    """
    span = float(jitter_min)
    if span < 0.0:
        raise ValueError(f"jitter_min must be >= 0, got {jitter_min}")
    rng = random.Random(int(seed))
    forecast: list[TrainTrajectory] = []
    for trajectory in iter_trajectories_by_type(TYPE_FREIGHT):
        offset = rng.uniform(-span, span) if span > 0.0 else 0.0
        forecast.append(_shift_trajectory(trajectory, offset))
    return tuple(forecast)


# ---------------------------------------------------------------------------
# Trajectory interpolation engine
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class LinePossession:
    """A running line that is closed between two chainages for a clock window."""

    possession_id: str
    protected_lines: tuple[str, ...]
    start_km: float
    end_km: float
    start_min: float
    end_min: float
    approach_km: float = 1.2
    stop_approaching: bool = False


@dataclass(frozen=True, slots=True)
class _HoldPlan:
    t_stop: float
    release_min: float
    stop_km: float
    stop_line: str
    increasing: bool
    tslw_speed_kmh: float | None = None
    tslw_lo_km: float | None = None
    tslw_hi_km: float | None = None


@dataclass(frozen=True, slots=True)
class _DivertPlan:
    divert_km: float
    divert_line: str
    rejoin_km: float
    crossover_speed_kmh: float = 25.0


def _interpolate_absolute(trajectory: TrainTrajectory, absolute_min: float) -> tuple[float, str, str, float]:
    """Pose at an absolute waypoint minute, without folding the clock."""
    waypoints = trajectory.waypoints
    if not waypoints:
        return trajectory.origin_km, trajectory.line_at_km(trajectory.origin_km), STATUS_STOPPED, 0.0
    if len(waypoints) == 1 or absolute_min <= waypoints[0].minute:
        wp = waypoints[0]
        return wp.km, wp.line, wp.status, 0.0
    if absolute_min >= waypoints[-1].minute:
        wp = waypoints[-1]
        return wp.km, wp.line, wp.status, 0.0

    last_index = len(waypoints) - 2
    for index in range(len(waypoints) - 1):
        left = waypoints[index]
        right = waypoints[index + 1]
        if absolute_min < left.minute or absolute_min > right.minute:
            continue
        if absolute_min == right.minute and index < last_index:
            continue
        dt = right.minute - left.minute
        if dt <= 1e-9:
            return right.km, right.line, STATUS_STOPPED, 0.0
        ratio = (absolute_min - left.minute) / dt
        km = left.km + (right.km - left.km) * ratio
        if abs(right.km - left.km) <= 1e-9:
            return right.km, right.line, STATUS_STOPPED, 0.0
        speed = abs(right.km - left.km) / dt * 60.0
        status = left.status if left.status != STATUS_STOPPED else right.status
        line = resolve_running_line(
            trajectory.direction, km, trajectory.train_type, trajectory.uses_mid_ghat
        )
        return km, line, status, speed
    wp = waypoints[-1]
    return wp.km, wp.line, wp.status, 0.0


def _time_at_km(left: Waypoint, right: Waypoint, km: float) -> float:
    dkm = right.km - left.km
    if abs(dkm) < 1e-9:
        return left.minute
    ratio = (km - left.km) / dkm
    ratio = min(1.0, max(0.0, ratio))
    return left.minute + ratio * (right.minute - left.minute)


def _protected_pieces(
    left: Waypoint,
    right: Waypoint,
    lines: frozenset[str],
) -> list[tuple[Waypoint, Waypoint]]:
    if left.line == right.line:
        if left.line in lines:
            return [(left, right)]
        return []
    mid_minute = (left.minute + right.minute) / 2.0
    mid_km = (left.km + right.km) / 2.0
    pieces: list[tuple[Waypoint, Waypoint]] = []
    if left.line in lines:
        pieces.append((left, Waypoint(mid_minute, mid_km, left.status, left.line)))
    if right.line in lines:
        pieces.append((Waypoint(mid_minute, mid_km, right.status, right.line), right))
    return pieces


def _occupancy_intervals(
    trajectory: TrainTrajectory,
    lo: float,
    hi: float,
    lines: frozenset[str],
) -> list[tuple[float, float]]:
    """Absolute minutes when the path is inside the open chainage (lo, hi) on a protected line."""
    intervals: list[tuple[float, float]] = []
    waypoints = trajectory.waypoints
    for index in range(len(waypoints) - 1):
        for left, right in _protected_pieces(waypoints[index], waypoints[index + 1], lines):
            if abs(left.km - right.km) < 1e-9:
                if lo < left.km < hi and right.minute > left.minute:
                    intervals.append((left.minute, right.minute))
                continue
            span_lo = min(left.km, right.km)
            span_hi = max(left.km, right.km)
            if span_hi <= lo or span_lo >= hi:
                continue
            inner_lo = max(span_lo, lo)
            inner_hi = min(span_hi, hi)
            if inner_hi - inner_lo <= 1e-6:
                continue
            if left.km <= right.km:
                km_enter, km_leave = inner_lo, inner_hi
            else:
                km_enter, km_leave = inner_hi, inner_lo
            t_enter = _time_at_km(left, right, km_enter)
            t_leave = _time_at_km(left, right, km_leave)
            if t_leave < t_enter:
                t_enter, t_leave = t_leave, t_enter
            if t_leave - t_enter > 1e-6:
                intervals.append((t_enter, t_leave))
    if not intervals:
        return []
    intervals.sort()
    merged: list[tuple[float, float]] = [intervals[0]]
    for start, end in intervals[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 1e-6:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _line_family(line: str) -> str | None:
    """UP/DN running family, read from the line-name prefix. MID has none."""
    name = str(line).upper()
    if name.startswith("UP"):
        return "UP"
    if name.startswith("DN"):
        return "DN"
    return None


def _spare_line_at(possession: LinePossession, km: float) -> str | None:
    """Spare independent line at `km`, or None when this section cannot divert.

    Sections 2 and 4 are double-line only and always return None. A possession
    that already closes every physical line at `km` also returns None.
    """
    section_id = topology.get_section_id_at_km(km)
    if section_id != topology.SECTION_1_ID and section_id != topology.SECTION_3_ID:
        return None
    protected = {str(line).upper() for line in possession.protected_lines}
    candidates = [
        line
        for line in topology.get_available_lines_at_km(km)
        if line.upper() not in protected
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    families = {_line_family(line) for line in protected}
    families.discard(None)
    if len(families) == 1:
        family = next(iter(families))
        same = [line for line in candidates if _line_family(line) == family]
        if same:
            return same[0]
    return candidates[0]


def _spare_closed_by_other(
    spare_line: str,
    km_lo: float,
    km_hi: float,
    possession: LinePossession,
    possessions: Sequence[LinePossession],
) -> bool:
    """True when another simultaneous possession already closes `spare_line` on this chainage."""
    wanted = spare_line.upper()
    for other in possessions:
        if other is possession:
            continue
        if wanted not in {str(line).upper() for line in other.protected_lines}:
            continue
        if not _windows_overlap(
            possession.start_min,
            possession.end_min,
            other.start_min,
            other.end_min,
        ):
            continue
        other_lo = min(other.start_km, other.end_km)
        other_hi = max(other.start_km, other.end_km)
        if _ranges_overlap(km_lo, km_hi, other_lo, other_hi):
            return True
    return False


def _hold_line(trajectory: TrainTrajectory, stop_km: float) -> str:
    """Running line used while a train waits short of a closed section."""
    if trajectory.train_type == TYPE_BANKER:
        if stop_km < KM_KJT:
            return LINE_DN
        if stop_km > KM_LNL:
            return LINE_UP
        return LINE_MID
    return trajectory.line_at_km(stop_km)


def _release_on_axis(t_in: float, t_out: float, start: float, end: float) -> float | None:
    """Possession end on the same absolute axis as a crossing that may wrap midnight."""
    for shift in (0.0, TOTAL_SIMULATION_MINUTES, -TOTAL_SIMULATION_MINUTES):
        if t_in < end + shift and t_out > start + shift:
            return end + shift
    return None


def _is_single_line_section(km: float) -> bool:
    """True on the double-line sections, which have no spare line to divert onto."""
    section_id = topology.get_section_id_at_km(km)
    return section_id in (topology.SECTION_2_ID, topology.SECTION_4_ID)


def _build_tslw_schedule(
    possession: LinePossession,
    trains: Sequence[TrainTrajectory],
) -> dict[str, tuple[float, float]]:
    """Token order for Temporary Single Line Working.

    Sections 1 and 3 return an empty dict; diversion handles those. On
    Section 2 or 4, trains that would enter the closed chainage during the
    possession pass one at a time: first come, either direction, with a
    fixed headway after the previous crossing.
    """
    if not _is_single_line_section(possession.start_km):
        return {}
    lines = frozenset(possession.protected_lines)
    lo = min(possession.start_km, possession.end_km)
    hi = max(possession.start_km, possession.end_km)
    arrivals: list[tuple[float, str]] = []
    for trajectory in trains:
        if not trajectory.waypoints:
            continue
        for t_in, t_out in _occupancy_intervals(trajectory, lo, hi, lines):
            if _release_on_axis(t_in, t_out, possession.start_min, possession.end_min) is None:
                continue
            arrivals.append((float(t_in), trajectory.train_id))
            break
    arrivals.sort(key=lambda item: (item[0], item[1]))
    transit_min = ((hi - lo) / _TSLW_SPEED_KMH) * 60.0
    schedule: dict[str, tuple[float, float]] = {}
    previous_end: float | None = None
    for natural_t_in, train_id in arrivals:
        if previous_end is None:
            crossing_start = natural_t_in
        else:
            crossing_start = max(natural_t_in, previous_end + _TSLW_HEADWAY_MIN)
        if crossing_start < possession.start_min:
            crossing_start = float(possession.start_min)
        crossing_end = crossing_start + transit_min
        schedule[train_id] = (crossing_start, crossing_end)
        previous_end = crossing_end
    return schedule


def _hold_for_possession(
    trajectory: TrainTrajectory,
    possession: LinePossession,
    possessions: Sequence[LinePossession],
    tslw_slot: tuple[float, float] | None = None,
) -> _HoldPlan | _DivertPlan | None:
    lines = frozenset(possession.protected_lines)
    lo = min(possession.start_km, possession.end_km)
    hi = max(possession.start_km, possession.end_km)
    approach = max(0.2, float(possession.approach_km))
    for t_in, t_out in _occupancy_intervals(trajectory, lo, hi, lines):
        release = _release_on_axis(t_in, t_out, possession.start_min, possession.end_min)
        if release is None:
            continue
        sample = max(trajectory.waypoints[0].minute, t_in - 0.05)
        km_before, _, _, _ = _interpolate_absolute(trajectory, sample)
        km_at, _, _, _ = _interpolate_absolute(trajectory, t_in)
        increasing = km_at >= km_before - 1e-6
        if increasing:
            stop_km = max(CORRIDOR_START_KM, lo - approach)
        else:
            stop_km = min(CORRIDOR_END_KM, hi + approach)
        t_stop = trajectory.waypoints[0].minute
        reached = False
        for index in range(len(trajectory.waypoints) - 1):
            left = trajectory.waypoints[index]
            right = trajectory.waypoints[index + 1]
            if left.minute > t_in:
                break
            seg_lo = min(left.km, right.km)
            seg_hi = max(left.km, right.km)
            if seg_lo - 1e-6 <= stop_km <= seg_hi + 1e-6:
                candidate = _time_at_km(left, right, stop_km)
                if candidate <= t_in + 1e-6:
                    t_stop = candidate
                    reached = True
        if not reached:
            t_stop = min(t_in, trajectory.waypoints[0].minute)
        spare = None if possession.stop_approaching else _spare_line_at(possession, stop_km)
        if spare is not None:
            if increasing:
                rejoin_km = min(CORRIDOR_END_KM, hi + approach)
            else:
                rejoin_km = max(CORRIDOR_START_KM, lo - approach)
            if not _spare_closed_by_other(
                spare,
                min(stop_km, rejoin_km),
                max(stop_km, rejoin_km),
                possession,
                possessions,
            ):
                return _DivertPlan(
                    divert_km=stop_km,
                    divert_line=spare,
                    rejoin_km=rejoin_km,
                )
        release_min = float(release)
        tslw_speed: float | None = None
        tslw_lo: float | None = None
        tslw_hi: float | None = None
        if (
            not possession.stop_approaching
            and tslw_slot is not None
            and _is_single_line_section(possession.start_km)
        ):
            crossing_start, _crossing_end = tslw_slot
            release_min = float(crossing_start)
            tslw_speed = _TSLW_SPEED_KMH
            tslw_lo = lo
            tslw_hi = hi
        return _HoldPlan(
            t_stop=t_stop,
            release_min=release_min,
            stop_km=stop_km,
            stop_line=_hold_line(trajectory, stop_km),
            increasing=increasing,
            tslw_speed_kmh=tslw_speed,
            tslw_lo_km=tslw_lo,
            tslw_hi_km=tslw_hi,
        )
    return None


def _plan_engage_min(trajectory: TrainTrajectory, plan: _HoldPlan | _DivertPlan) -> float:
    """Absolute minute the train meets this plan, used to keep the earliest conflict."""
    if isinstance(plan, _HoldPlan):
        return plan.t_stop
    km = plan.divert_km
    for index in range(len(trajectory.waypoints) - 1):
        left = trajectory.waypoints[index]
        right = trajectory.waypoints[index + 1]
        if min(left.km, right.km) - 1e-6 <= km <= max(left.km, right.km) + 1e-6:
            return _time_at_km(left, right, km)
    if trajectory.waypoints:
        return trajectory.waypoints[0].minute
    return 0.0


def _conflict_hold(
    trajectory: TrainTrajectory,
    possessions: Sequence[LinePossession],
    tslw_schedules: dict[int, dict[str, tuple[float, float]]] | None = None,
) -> _HoldPlan | _DivertPlan | None:
    chosen: _HoldPlan | _DivertPlan | None = None
    chosen_at = float("inf")
    schedules = tslw_schedules or {}
    for possession in possessions:
        slot = schedules.get(id(possession), {}).get(trajectory.train_id)
        plan = _hold_for_possession(trajectory, possession, possessions, slot)
        if plan is None:
            continue
        engage = _plan_engage_min(trajectory, plan)
        if engage < chosen_at:
            chosen = plan
            chosen_at = engage
    return chosen


def _absolute_now(trajectory: TrainTrajectory, clock: float, extra_tail: float) -> float | None:
    occupied_end = trajectory.window.end_min + max(0.0, extra_tail)
    if occupied_end <= trajectory.window.start_min:
        return None
    if not _unwrapped_in_window(clock, trajectory.window.start_min, occupied_end):
        return None
    return _map_to_unwrapped(clock, trajectory.window.start_min, occupied_end)


def _direction_for(trajectory: TrainTrajectory, status: str, increasing: bool | None) -> str:
    if status == STATUS_BANKER_PUSH:
        return DIR_DN
    if status == STATUS_BANKER_RETURN:
        return DIR_UP
    if increasing is not None:
        return DIR_DN if increasing else DIR_UP
    return trajectory.direction


def _regulated_train_state(
    trajectory: TrainTrajectory,
    clock: float,
    possessions: Sequence[LinePossession],
    tslw_schedules: dict[int, dict[str, tuple[float, float]]] | None = None,
) -> dict[str, object] | None:
    if not trajectory.waypoints:
        return None
    plan = _conflict_hold(trajectory, possessions, tslw_schedules) if possessions else None
    hold = plan if isinstance(plan, _HoldPlan) else None
    divert = plan if isinstance(plan, _DivertPlan) else None
    extra = 0.0 if hold is None else max(0.0, hold.release_min - hold.t_stop)
    real = _absolute_now(trajectory, clock, extra)
    if real is None:
        return None

    held = False
    diverted = False
    delay = 0.0
    increasing: bool | None = None
    if hold is not None and real >= hold.t_stop:
        increasing = hold.increasing
        if real < hold.release_min:
            held = True
            delay = real - hold.t_stop
            km = hold.stop_km
            line = hold.stop_line
            status = STATUS_HELD
            speed = 0.0
        else:
            delay = extra
            km, line, status, speed = _interpolate_absolute(trajectory, real - extra)
    else:
        km, line, status, speed = _interpolate_absolute(trajectory, real)

    if trajectory.train_type == TYPE_LOCAL and not held:
        km = min(trajectory.max_km, max(trajectory.min_km, km))
    if trajectory.train_type == TYPE_BANKER and not held:
        km = min(KM_LNL, max(KM_KJT, km))

    if divert is not None and not held:
        zone_lo = min(divert.divert_km, divert.rejoin_km)
        zone_hi = max(divert.divert_km, divert.rejoin_km)
        if zone_lo - 1e-9 <= float(km) <= zone_hi + 1e-9:
            diverted = True
            line = divert.divert_line
            status = STATUS_DIVERTED
            if (
                abs(float(km) - divert.divert_km) <= _CROSSOVER_SLOW_KM
                or abs(float(km) - divert.rejoin_km) <= _CROSSOVER_SLOW_KM
            ):
                speed = min(float(speed), divert.crossover_speed_kmh)

    if (
        hold is not None
        and not held
        and hold.tslw_speed_kmh is not None
        and hold.tslw_lo_km is not None
        and hold.tslw_hi_km is not None
        and hold.tslw_lo_km - 1e-9 <= float(km) <= hold.tslw_hi_km + 1e-9
    ):
        speed = min(float(speed), hold.tslw_speed_kmh)

    return {
        "train_id": trajectory.train_id,
        "train_type": trajectory.train_type,
        "name": trajectory.service_name,
        "direction": _direction_for(trajectory, status, increasing if held else None),
        "current_km": round(float(km), 3),
        "current_line": line,
        "speed_kmh": 0.0 if held else round(float(speed), 2),
        "status": status,
        "min_km": trajectory.min_km,
        "max_km": trajectory.max_km,
        "origin_code": trajectory.origin_code,
        "dest_code": trajectory.dest_code,
        "held": held,
        "diverted": diverted,
        "delay_min": round(delay, 2),
        "hold_rank": 0.0 if hold is None else hold.t_stop,
    }


def _stagger_held_trains(
    rows: list[dict[str, object]],
    possessions: Sequence[LinePossession],
) -> None:
    """Queue trains waiting at the same signal so two rakes do not share one chainage."""
    if not possessions:
        return
    possession = possessions[0]
    lo = min(possession.start_km, possession.end_km)
    hi = max(possession.start_km, possession.end_km)
    approach = max(0.2, float(possession.approach_km))
    held = [row for row in rows if row.get("held")]
    west = sorted(
        (row for row in held if float(row["current_km"]) <= lo),
        key=lambda row: (float(row["hold_rank"]), str(row["train_id"])),
    )
    east = sorted(
        (row for row in held if float(row["current_km"]) > lo),
        key=lambda row: (float(row["hold_rank"]), str(row["train_id"])),
    )
    for index, row in enumerate(west):
        row["current_km"] = round(max(CORRIDOR_START_KM, lo - approach - index * QUEUE_GAP_KM), 3)
        row["current_line"] = _queue_line(str(row["train_type"]), float(row["current_km"]), str(row["current_line"]))
    for index, row in enumerate(east):
        row["current_km"] = round(min(CORRIDOR_END_KM, hi + approach + index * QUEUE_GAP_KM), 3)
        row["current_line"] = _queue_line(str(row["train_type"]), float(row["current_km"]), str(row["current_line"]))


def _queue_line(train_type: str, km: float, fallback: str) -> str:
    if train_type == TYPE_BANKER:
        if km < KM_KJT:
            return LINE_DN
        if km > KM_LNL:
            return LINE_UP
        return LINE_MID
    if fallback == LINE_MID and (km < KM_KJT or km > KM_LNL):
        return LINE_DN if km < KM_KJT else LINE_UP
    return fallback


def get_regulated_positions(
    current_min: float,
    possessions: Sequence[LinePossession] = (),
) -> list[dict[str, object]]:
    """
    Live positions with possessions applied.

    On Section 1 (quadruple) and Section 3 (ghat triple) a train that would
    meet a closed line moves onto a spare line until it is past the block.
    Sections 2 and 4 keep a single line open under token working: conflicting
    trains wait for their own crossing slot, then pass at reduced speed.
    A possession with stop_approaching holds those trains short of the site
    on their own line. It does not divert them and it does not token them
    through the closed chainage.
    """
    clock = wrap_minute(current_min)
    tslw_schedules = {
        id(possession): _build_tslw_schedule(possession, ALL_TRAJECTORIES)
        for possession in possessions
    }
    rows: list[dict[str, object]] = []
    for trajectory in ALL_TRAJECTORIES:
        state = _regulated_train_state(trajectory, clock, possessions, tslw_schedules)
        if state is not None:
            rows.append(state)
    if possessions:
        _stagger_held_trains(rows, possessions)
    for row in rows:
        row.pop("hold_rank", None)
    rows.sort(key=lambda row: (float(row["current_km"]), str(row["train_id"])))
    return rows


def get_trains_in_block(
    km_start: float,
    km_end: float,
    t_start: float,
    t_end: float,
    line_name: str,
) -> list[TrainTrajectory]:
    """Return trajectories whose spatial path overlaps [km_start, km_end] on `line_name`."""
    block_lo = min(float(km_start), float(km_end))
    block_hi = max(float(km_start), float(km_end))
    query_start = float(t_start)
    query_end = float(t_end)
    if query_end < query_start:
        query_end += TOTAL_SIMULATION_MINUTES
    wanted = str(line_name).upper()
    hits: list[TrainTrajectory] = []
    for trajectory in ALL_TRAJECTORIES:
        if not _windows_overlap(
            trajectory.window.start_min,
            trajectory.window.end_min,
            query_start,
            query_end,
        ):
            continue
        if not _trajectory_uses_line(trajectory, wanted, block_lo, block_hi):
            continue
        occupied = _km_occupied_during(trajectory, query_start, query_end)
        if occupied is None:
            continue
        occ_lo, occ_hi = occupied
        if _ranges_overlap(occ_lo, occ_hi, block_lo, block_hi):
            hits.append(trajectory)
    return hits


def _trajectory_uses_line(
    trajectory: TrainTrajectory,
    line_name: str,
    block_lo: float,
    block_hi: float,
) -> bool:
    samples = (
        block_lo,
        block_hi,
        (block_lo + block_hi) / 2.0,
        trajectory.origin_km,
        trajectory.dest_km,
        KM_KJT,
        KM_LNL,
        KM_KYN,
    )
    for km in samples:
        if block_lo - 1e-6 <= km <= block_hi + 1e-6:
            if trajectory.line_at_km(km) == line_name:
                return True
    for waypoint in trajectory.waypoints:
        if waypoint.line == line_name and _ranges_overlap(
            waypoint.km, waypoint.km, block_lo, block_hi
        ):
            return True
    return False


def _km_occupied_during(
    trajectory: TrainTrajectory,
    t_start: float,
    t_end: float,
) -> tuple[float, float] | None:
    kms: list[float] = []
    for waypoint in trajectory.waypoints:
        if _unwrapped_in_window(waypoint.minute, t_start, t_end) or _unwrapped_in_window(
            wrap_minute(waypoint.minute), t_start, t_end
        ):
            kms.append(waypoint.km)
        if t_start <= waypoint.minute <= t_end:
            kms.append(waypoint.km)
    for sample in (t_start, t_end, (t_start + t_end) / 2.0):
        if trajectory.window.contains(sample) or trajectory.window.contains(wrap_minute(sample)):
            km, _, _, _ = _interpolate_at(trajectory, wrap_minute(sample))
            kms.append(km)
    if not kms:
        if trajectory.window.contains(wrap_minute(t_start)) or trajectory.window.contains(
            wrap_minute(t_end)
        ):
            for sample in (t_start, t_end):
                km, _, _, _ = _interpolate_at(trajectory, wrap_minute(sample))
                kms.append(km)
    if not kms:
        return None
    return min(kms), max(kms)


__all__ = [
    "ALL_TRAJECTORIES",
    "BANKER_CYCLE_MIN",
    "BANKER_MASTER",
    "CKL_RAKES",
    "MAINLINE_SERVICES",
    "PLL_RAKES",
    "STATUS_BANKER_PUSH",
    "STATUS_BANKER_RETURN",
    "STATUS_DIVERTED",
    "STATUS_HELD",
    "STATUS_RUNNING",
    "STATUS_STOPPED",
    "LinePossession",
    "StopCall",
    "TOTAL_SIMULATION_MINUTES",
    "TimeWindow",
    "TrainTrajectory",
    "Waypoint",
    "build_timetable",
    "get_regulated_positions",
    "generate_freight_forecast",
    "get_all_trajectories",
    "get_trains_in_block",
    "iter_trajectories_by_type",
    "minute_to_hhmm",
    "resolve_running_line",
    "wrap_minute",
]
