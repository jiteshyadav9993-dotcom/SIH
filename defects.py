"""
defects.py
====================================================================
Central Railway maintenance defect backlog for CSMT (KM 0.0) → Pune (KM 192.0).

TMS / TDMS / SMMS defect records, physical 25 kV OHE feeding sub-sectors,
division machinery pooling, Priority Urgency Scoring (Formula 1), and
spatial bundling (Formula 2).

No solver constraints. Bundles are emitted as one concurrent set so a
downstream CP-SAT model can time-slot every possession together.
====================================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, Mapping, Sequence

import timetable
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
    SECTION_2_ID,
    SECTION_3_ID,
    SECTION_4_ID,
    get_available_lines_at_km,
    get_section_id_at_km,
)

# ---------------------------------------------------------------------------
# Operating constants (spatial footprints, cascade weights, Formula 1 / 2)
# ---------------------------------------------------------------------------

SAFETY_CLEARANCE_KM: Final[float] = 0.5
APPROACH_FOOTPRINT_KM: Final[float] = 1.0
DEPARTURE_FOOTPRINT_KM: Final[float] = 1.0
MOBILIZATION_BUFFER_MIN: Final[float] = 15.0
GROUPING_BONUS: Final[float] = 15.0
DEFAULT_SPATIAL_GAP_KM: Final[float] = 3.0
MANDATORY_SEVERITY_THRESHOLD: Final[int] = 80
SLOT_GRANULARITY_MIN: Final[float] = 5.0
DOWNSTREAM_CASCADE_DECAY: Final[float] = 0.65

# Formula 1 weights (P_i ∈ [0, 100]).
W_SAFETY: Final[float] = 10.0
W_OVERDUE: Final[float] = 0.35
W_TSR: Final[float] = 15.0
W_TRAFFIC: Final[float] = 3.0
DEFAULT_TRAFFIC_DENSITY: Final[int] = 5
# Full-day distinct trajectories in a ±1 km window on the default backlog:
# ghat DN 6, suburban line 24, ghat MID 36, Kalyan–Karjat double 48,
# Pune plateau double 52. Dividing by 6 maps that onto 1–9 inside [1, 10].
TRAFFIC_DENSITY_DIVISOR: Final[float] = 6.0
TRAFFIC_WINDOW_KM: Final[float] = 1.0

KNOCK_ON_DELAY_WEIGHTS: Final[dict[str, float]] = {
    "PREMIUM": 5.0,
    "SUPERFAST": 4.0,
    "EXPRESS": 3.0,
    "LOCAL": 2.0,
    "BANKER": 1.5,
    "FREIGHT": 1.0,
}

SOURCE_TMS: Final[str] = "TMS"
SOURCE_TDMS: Final[str] = "TDMS"
SOURCE_SMMS: Final[str] = "SMMS"


# ---------------------------------------------------------------------------
# Defect, machine, and block enumerations
# ---------------------------------------------------------------------------


class DefectType(str, Enum):
    RAIL_FRACTURE = "RAIL_FRACTURE"
    WELD_FAILURE = "WELD_FAILURE"
    OHE_SAG = "OHE_SAG"
    SIGNAL_INTERLOCK = "SIGNAL_INTERLOCK"
    TAMPING_REQUIRED = "TAMPING_REQUIRED"
    DEEP_SCREENING = "DEEP_SCREENING"


class MachineType(str, Enum):
    NONE = "NONE"
    TIE_TAMPER = "TIE_TAMPER"
    BCM_MACHINE = "BCM_MACHINE"
    TOWER_WAGON = "TOWER_WAGON"


class BlockType(str, Enum):
    TRAFFIC_ONLY = "TRAFFIC_ONLY"
    POWER_AND_TRAFFIC = "POWER_AND_TRAFFIC"


DIVISION_MACHINERY_LIMITS: Final[dict[MachineType, int]] = {
    MachineType.TIE_TAMPER: 2,
    MachineType.BCM_MACHINE: 1,
    MachineType.TOWER_WAGON: 2,
}

# Higher rank is more restrictive for a shared possession.
MACHINE_RESTRICTION_RANK: Final[dict[MachineType, int]] = {
    MachineType.NONE: 0,
    MachineType.TOWER_WAGON: 1,
    MachineType.TIE_TAMPER: 2,
    MachineType.BCM_MACHINE: 3,
}

# (required_block_min, required_machine, block_type)
DEFECT_TYPE_SPEC: Final[dict[DefectType, tuple[float, MachineType, BlockType]]] = {
    DefectType.RAIL_FRACTURE: (90.0, MachineType.NONE, BlockType.TRAFFIC_ONLY),
    DefectType.WELD_FAILURE: (60.0, MachineType.NONE, BlockType.TRAFFIC_ONLY),
    DefectType.OHE_SAG: (75.0, MachineType.TOWER_WAGON, BlockType.POWER_AND_TRAFFIC),
    DefectType.SIGNAL_INTERLOCK: (45.0, MachineType.NONE, BlockType.TRAFFIC_ONLY),
    DefectType.TAMPING_REQUIRED: (120.0, MachineType.TIE_TAMPER, BlockType.TRAFFIC_ONLY),
    DefectType.DEEP_SCREENING: (180.0, MachineType.BCM_MACHINE, BlockType.TRAFFIC_ONLY),
}


# ---------------------------------------------------------------------------
# Physical 25 kV OHE traction feeding sub-sectors
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PowerSubSector:
    """A physical OHE feeder zone. Isolation is line-specific, not a time buffer."""

    sector_id: str
    start_km: float
    end_km: float
    isolated_line: str

    def contains(self, km: float, track_line: str) -> bool:
        return (
            self.isolated_line == track_line
            and self.start_km <= float(km) <= self.end_km
        )


def _pss(sector_id: str, start_km: float, end_km: float, isolated_line: str) -> PowerSubSector:
    return PowerSubSector(
        sector_id=sector_id,
        start_km=start_km,
        end_km=end_km,
        isolated_line=isolated_line,
    )


def _build_power_sub_sectors() -> tuple[PowerSubSector, ...]:
    sectors: list[PowerSubSector] = []

    suburban_spans: tuple[tuple[str, float, float], ...] = (
        ("CSMT_DR", 0.0, 9.0),
        ("DR_TNA", 9.0, 33.0),
        ("TNA_KYN", 33.0, 54.0),
    )
    for name, start_km, end_km in suburban_spans:
        for line in (LINE_UP_FAST, LINE_DN_FAST, LINE_UP_SLOW, LINE_DN_SLOW):
            sectors.append(_pss(f"PSS_{name}_{line}", start_km, end_km, line))

    for line in (LINE_UP, LINE_DN):
        sectors.append(_pss(f"PSS_KYN_KJT_{line}", 54.0, 100.0, line))

    for line in (LINE_UP, LINE_MID, LINE_DN):
        sectors.append(_pss(f"PSS_GHAT_{line}" if line != LINE_MID else "PSS_GHAT_MID", 100.0, 128.0, line))

    plateau_spans: tuple[tuple[str, float, float], ...] = (
        ("LNL_TGN", 128.0, 158.0),
        ("TGN_PUNE", 158.0, 192.0),
    )
    for name, start_km, end_km in plateau_spans:
        for line in (LINE_UP, LINE_DN):
            sectors.append(_pss(f"PSS_{name}_{line}", start_km, end_km, line))

    return tuple(sectors)


POWER_SUB_SECTORS: tuple[PowerSubSector, ...] = _build_power_sub_sectors()
POWER_SUB_SECTORS_BY_ID: dict[str, PowerSubSector] = {
    sector.sector_id: sector for sector in POWER_SUB_SECTORS
}


def get_power_subsector_for_location(km: float, track_line: str) -> str:
    """Return the exact electrical feed zone for a chainage on a named running line."""
    chainage = float(km)
    if chainage < CORRIDOR_START_KM or chainage > CORRIDOR_END_KM:
        raise ValueError(
            f"Chainage KM {chainage} is outside the corridor "
            f"[{CORRIDOR_START_KM}, {CORRIDOR_END_KM}]"
        )
    line = str(track_line).upper()
    available = get_available_lines_at_km(chainage)
    if line not in available:
        raise ValueError(
            f"Line {line!r} is not present at KM {chainage}; available lines: {available}"
        )

    matches = [sector for sector in POWER_SUB_SECTORS if sector.contains(chainage, line)]
    if not matches:
        raise ValueError(f"No OHE sub-sector feeds {line} at KM {chainage}")
    # Junction kilometres (54 / 100 / 128 / 158) belong to the upstream feeder.
    chosen = min(matches, key=lambda sector: (sector.start_km, sector.sector_id))
    return chosen.sector_id


def get_power_subsector(sector_id: str) -> PowerSubSector:
    key = str(sector_id)
    if key not in POWER_SUB_SECTORS_BY_ID:
        raise KeyError(f"Unknown power sub-sector {sector_id!r}")
    return POWER_SUB_SECTORS_BY_ID[key]


# ---------------------------------------------------------------------------
# Spatial footprints (no ghost-train static obstacles)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpatialFootprint:
    """
    Exact possession geometry on the chainage axis.

    block_start_km / block_end_km — worksite plus 0.5 km safety clearance.
    entry_km / exit_km — approach and departure envelopes a moving train
    occupies while entering or clearing the block (not a point obstacle).
    """

    block_start_km: float
    block_end_km: float
    entry_km: float
    exit_km: float

    def contains_km(self, km: float) -> bool:
        return self.entry_km <= float(km) <= self.exit_km

    def overlaps(self, other_start_km: float, other_end_km: float) -> bool:
        lo = min(float(other_start_km), float(other_end_km))
        hi = max(float(other_start_km), float(other_end_km))
        return not (hi < self.entry_km or lo > self.exit_km)


def _clamp_km(km: float) -> float:
    return max(CORRIDOR_START_KM, min(CORRIDOR_END_KM, float(km)))


def _make_footprint(work_start_km: float, work_end_km: float) -> SpatialFootprint:
    block_start = _clamp_km(min(work_start_km, work_end_km) - SAFETY_CLEARANCE_KM)
    block_end = _clamp_km(max(work_start_km, work_end_km) + SAFETY_CLEARANCE_KM)
    return SpatialFootprint(
        block_start_km=round(block_start, 3),
        block_end_km=round(block_end, 3),
        entry_km=round(_clamp_km(block_start - APPROACH_FOOTPRINT_KM), 3),
        exit_km=round(_clamp_km(block_end + DEPARTURE_FOOTPRINT_KM), 3),
    )


# ---------------------------------------------------------------------------
# Track defect data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrackDefect:
    defect_id: str
    section_id: str
    km_location: float
    track_line: str
    defect_type: DefectType
    severity: int
    required_block_min: float
    required_machine: MachineType
    block_type: BlockType
    power_sector_id: str
    is_mandatory: bool
    days_overdue: float = 0.0   # calendar days past the scheduled rectification date
    has_tsr: bool = False       # True when a Temporary Speed Restriction is in force

    def spatial_footprint(self) -> SpatialFootprint:
        return _make_footprint(self.km_location, self.km_location)

    def block_boundaries(self) -> tuple[float, float]:
        footprint = self.spatial_footprint()
        return footprint.block_start_km, footprint.block_end_km


def _make_defect(
    defect_id: str,
    km_location: float,
    track_line: str,
    defect_type: DefectType,
    severity: int,
    days_overdue: float = 0.0,
    has_tsr: bool = False,
) -> TrackDefect:
    if severity < 1 or severity > 100:
        raise ValueError(f"Severity must be in [1, 100], got {severity}")
    chainage = float(km_location)
    line = str(track_line).upper()
    available = get_available_lines_at_km(chainage)
    if line not in available:
        raise ValueError(
            f"Line {line!r} is not present at KM {chainage}; available lines: {available}"
        )
    block_min, machine, block_type = DEFECT_TYPE_SPEC[defect_type]
    return TrackDefect(
        defect_id=defect_id,
        section_id=get_section_id_at_km(chainage),
        km_location=chainage,
        track_line=line,
        defect_type=defect_type,
        severity=int(severity),
        required_block_min=block_min,
        required_machine=machine,
        block_type=block_type,
        power_sector_id=get_power_subsector_for_location(chainage, line),
        is_mandatory=int(severity) >= MANDATORY_SEVERITY_THRESHOLD,
        days_overdue=float(days_overdue),
        has_tsr=bool(has_tsr),
    )


def estimate_traffic_density(defect: TrackDefect) -> int:
    """
    Formula 1 traffic index t_i from trains that actually use this line.

    Counts distinct trajectories overlapping ±1 km of the defect on
    ``defect.track_line`` across the full 1,440-minute operating day, then
    maps that count with ``min(10, max(1, round(count / 6)))``.
    """
    km_start = float(defect.km_location) - TRAFFIC_WINDOW_KM
    km_end = float(defect.km_location) + TRAFFIC_WINDOW_KM
    hits = timetable.get_trains_in_block(km_start, km_end, 0, 1440, defect.track_line)
    count = len({trajectory.train_id for trajectory in hits})
    return min(10, max(1, round(count / TRAFFIC_DENSITY_DIVISOR)))


def compute_priority_urgency_score(
    defect,
    days_overdue: float = 0.0,
    has_tsr: bool = False,
    traffic_density: int = DEFAULT_TRAFFIC_DENSITY,
    clip: bool = True,
) -> float:
    """
    Formula 1 — Priority Urgency Scoring.

    P_i = w_s·s_i + w_o·(o_i/30)·100 + w_v·v_i + w_t·t_i.
    Safety criticality s_i is severity mapped onto [0, 5].

    Clipped to [0, 100] by default (clip=True) for the human-readable
    dashboard gauge. Pass clip=False to get the raw, unclipped score —
    used when feeding the CP-SAT objective, so top-tier critical defects
    (which would otherwise all saturate at 100) still rank distinctly
    against each other.
    """
    s_i = min(5.0, float(defect.severity) / 20.0)
    o_i = min(30.0, max(0.0, float(days_overdue)))
    v_i = 1.0 if has_tsr else 0.0
    t_i = max(0, int(traffic_density))
    raw = (W_SAFETY * s_i) + (W_OVERDUE * (o_i / 30.0) * 100.0) + (W_TSR * v_i) + (W_TRAFFIC * t_i)
    return round(min(100.0, raw), 1) if clip else round(raw, 1)


def knock_on_delay_weight(train_type: str) -> float:
    """Explicit cascade weight used when time-slotting a possession against a train class."""
    return KNOCK_ON_DELAY_WEIGHTS.get(str(train_type).upper(), 1.0)


def downstream_cascade_penalty(
    primary_delay_min: float,
    train_type: str,
    downstream_count: int,
    decay: float = DOWNSTREAM_CASCADE_DECAY,
) -> float:
    """
    Weighted knock-on cost of delaying a train and the following `downstream_count`
    services. Decay < 1 prevents treating every later train as a full primary delay.
    """
    hops = max(0, int(downstream_count))
    hop_delay = max(0.0, float(primary_delay_min))
    weight = knock_on_delay_weight(train_type)
    total = 0.0
    fade = max(0.0, min(1.0, float(decay)))
    for _ in range(hops + 1):
        total += weight * hop_delay
        hop_delay *= fade
    return round(total, 2)


def align_slot_minute(minute: float, granularity_min: float = SLOT_GRANULARITY_MIN) -> float:
    """Snap a candidate possession start onto the dynamic time-slot grid."""
    step = float(granularity_min)
    if step <= 0.0:
        raise ValueError("Slot granularity must be positive")
    return round(round(float(minute) / step) * step, 3)


# ---------------------------------------------------------------------------
# Multi-defect spatial clustering (Formula 2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MaintenanceBundle:
    bundle_id: str
    track_line: str
    section_id: str
    start_km: float
    end_km: float
    defects: tuple[TrackDefect, ...]
    required_duration_min: float
    required_machine: MachineType
    block_type: BlockType
    power_sector_id: str
    cumulative_priority: float
    urgency_score: float = 0.0  # max unclipped Formula-1 score among member defects
    is_mandatory: bool = False  # True when any member defect is safety-critical

    def spatial_footprint(self) -> SpatialFootprint:
        return _make_footprint(self.start_km + SAFETY_CLEARANCE_KM, self.end_km - SAFETY_CLEARANCE_KM)

    def block_boundaries(self) -> tuple[float, float]:
        return self.start_km, self.end_km

    def machines_required(self) -> tuple[MachineType, ...]:
        types = {
            defect.required_machine
            for defect in self.defects
            if defect.required_machine is not MachineType.NONE
        }
        return tuple(sorted(types, key=lambda item: MACHINE_RESTRICTION_RANK[item], reverse=True))


def _most_restrictive_machine(defects: Sequence[TrackDefect]) -> MachineType:
    chosen = MachineType.NONE
    rank = MACHINE_RESTRICTION_RANK[chosen]
    for defect in defects:
        candidate_rank = MACHINE_RESTRICTION_RANK[defect.required_machine]
        if candidate_rank > rank:
            chosen = defect.required_machine
            rank = candidate_rank
    return chosen


def _section_token(section_id: str) -> str:
    mapping: dict[str, str] = {
        SECTION_1_ID: "S1",
        SECTION_2_ID: "S2",
        SECTION_3_ID: "S3",
        SECTION_4_ID: "S4",
    }
    return mapping.get(section_id, section_id.replace("SECTION_", "S"))


def _build_bundle(
    members: Sequence[TrackDefect],
    sequence_no: int,
) -> MaintenanceBundle:
    ordered = tuple(sorted(members, key=lambda item: (item.km_location, item.defect_id)))
    lead = ordered[0]
    start_km = round(_clamp_km(min(item.km_location for item in ordered) - SAFETY_CLEARANCE_KM), 3)
    end_km = round(_clamp_km(max(item.km_location for item in ordered) + SAFETY_CLEARANCE_KM), 3)
    duration = max(item.required_block_min for item in ordered) + MOBILIZATION_BUFFER_MIN
    block_type = (
        BlockType.POWER_AND_TRAFFIC
        if any(item.block_type is BlockType.POWER_AND_TRAFFIC for item in ordered)
        else BlockType.TRAFFIC_ONLY
    )
    grouping = GROUPING_BONUS if len(ordered) > 1 else 0.0
    line_token = lead.track_line
    bundle_id = f"BUNDLE_{_section_token(lead.section_id)}_{line_token}_{sequence_no:02d}"
    # Unclipped urgency feeds the CP-SAT objective — clipped scores would
    # collapse every top-tier critical defect to a tied 100.0.
    urgency = max(
        compute_priority_urgency_score(
            item,
            days_overdue=item.days_overdue,
            has_tsr=item.has_tsr,
            traffic_density=estimate_traffic_density(item),
            clip=False,
        )
        for item in ordered
    )
    return MaintenanceBundle(
        bundle_id=bundle_id,
        track_line=lead.track_line,
        section_id=lead.section_id,
        start_km=start_km,
        end_km=end_km,
        defects=ordered,
        required_duration_min=duration,
        required_machine=_most_restrictive_machine(ordered),
        block_type=block_type,
        power_sector_id=lead.power_sector_id,
        cumulative_priority=float(sum(item.severity for item in ordered) + grouping),
        urgency_score=urgency,
        is_mandatory=any(item.is_mandatory for item in ordered),
    )


def cluster_defects_into_bundles(
    defects: Sequence[TrackDefect],
    max_spatial_gap_km: float = DEFAULT_SPATIAL_GAP_KM,
) -> tuple[MaintenanceBundle, ...]:
    """
    Formula 2 — Spatial Bundling.

    Defects on the same running line and the same physical OHE sub-sector are
    chained when successive chainages are within `max_spatial_gap_km`.
    All bundles are returned together for concurrent corridor solving.
    """
    if not defects:
        return ()

    grouped: dict[tuple[str, str], list[TrackDefect]] = {}
    for defect in defects:
        grouped.setdefault((defect.track_line, defect.power_sector_id), []).append(defect)

    bundles: list[MaintenanceBundle] = []
    counters: dict[tuple[str, str], int] = {}

    for group_key in sorted(grouped.keys(), key=lambda item: (item[0], item[1])):
        cohort = sorted(grouped[group_key], key=lambda item: (item.km_location, item.defect_id))
        cluster: list[TrackDefect] = [cohort[0]]
        for defect in cohort[1:]:
            span_gap = defect.km_location - cluster[-1].km_location
            if span_gap <= max_spatial_gap_km:
                cluster.append(defect)
                continue
            id_key = (cluster[0].section_id, cluster[0].track_line)
            counters[id_key] = counters.get(id_key, 0) + 1
            bundles.append(_build_bundle(cluster, counters[id_key]))
            cluster = [defect]
        id_key = (cluster[0].section_id, cluster[0].track_line)
        counters[id_key] = counters.get(id_key, 0) + 1
        bundles.append(_build_bundle(cluster, counters[id_key]))

    bundles.sort(key=lambda item: (item.start_km, item.track_line, item.bundle_id))
    return tuple(bundles)


# ---------------------------------------------------------------------------
# Shared machinery pooling (no duplicate simultaneous bookings)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MachineAllocation:
    machine_type: MachineType
    unit_index: int
    bundle_id: str
    start_min: float
    end_min: float

    def overlaps(self, start_min: float, end_min: float) -> bool:
        return not (float(end_min) <= self.start_min or float(start_min) >= self.end_min)


def machinery_contention_groups(
    bundles: Sequence[MaintenanceBundle],
) -> dict[MachineType, tuple[str, ...]]:
    """Bundle IDs that share a scarce machine type and cannot occupy it in parallel."""
    groups: dict[MachineType, list[str]] = {}
    for bundle in bundles:
        for machine in bundle.machines_required():
            groups.setdefault(machine, []).append(bundle.bundle_id)
    return {machine: tuple(ids) for machine, ids in groups.items()}


def allocate_machinery(
    bookings: Sequence[tuple[MaintenanceBundle, float, float]],
    limits: Mapping[MachineType, int] | None = None,
) -> tuple[MachineAllocation, ...]:
    """
    Assign division plant to concurrent possessions.

    `bookings` is (bundle, start_min, end_min). Raises if a machine type would
    be double-booked beyond DIVISION_MACHINERY_LIMITS.
    """
    capacity = dict(limits if limits is not None else DIVISION_MACHINERY_LIMITS)
    calendar: list[MachineAllocation] = []

    ordered = sorted(bookings, key=lambda item: (item[1], item[2], item[0].bundle_id))
    for bundle, start_min, end_min in ordered:
        if end_min <= start_min:
            raise ValueError(
                f"Invalid possession window for {bundle.bundle_id}: "
                f"{start_min}–{end_min}"
            )
        for machine in bundle.machines_required():
            limit = capacity.get(machine, 0)
            if limit <= 0:
                raise ValueError(f"No division units of {machine.value} are available")
            busy = [
                item
                for item in calendar
                if item.machine_type is machine and item.overlaps(start_min, end_min)
            ]
            if len(busy) >= limit:
                raise ValueError(
                    f"Machine pool exhausted for {machine.value}: "
                    f"{bundle.bundle_id} overlaps "
                    f"{', '.join(item.bundle_id for item in busy)}"
                )
            used_units = {item.unit_index for item in busy}
            unit_index = next(index for index in range(1, limit + 1) if index not in used_units)
            calendar.append(
                MachineAllocation(
                    machine_type=machine,
                    unit_index=unit_index,
                    bundle_id=bundle.bundle_id,
                    start_min=float(start_min),
                    end_min=float(end_min),
                )
            )
    return tuple(calendar)


# ---------------------------------------------------------------------------
# 24-hour corridor defect baseline
# ---------------------------------------------------------------------------


def get_default_defects() -> tuple[TrackDefect, ...]:
    """Twelve-to-fourteen field defects across all four CSMT–Pune sectors.

    `days_overdue` and `has_tsr` are representative synthetic values chosen
    so urgency scores differentiate clearly in demo runs:
      • Ghat rail fracture (DEF_S3_01): worst case — 28 d overdue + TSR
      • Kalyan OHE sag (DEF_S2_02): 12 d overdue + TSR
      • Low-severity tamping items: 0 d, no TSR
    """
    return (
        # Sector 1 — suburban quadruple (KM 0–54)
        # Rail fracture at severity 92; 18 days past rectification schedule.
        _make_defect("DEF_S1_01", 15.2, LINE_DN_FAST, DefectType.RAIL_FRACTURE, 92,
                     days_overdue=18.0, has_tsr=False),
        # Routine tamping; well within maintenance window.
        _make_defect("DEF_S1_02", 33.8, LINE_DN_SLOW, DefectType.TAMPING_REQUIRED, 58,
                     days_overdue=0.0, has_tsr=False),
        # Signal interlock; 6 days overdue — moderate urgency.
        _make_defect("DEF_S1_03", 9.4, LINE_UP_FAST, DefectType.SIGNAL_INTERLOCK, 74,
                     days_overdue=6.0, has_tsr=False),
        # Weld failure; 2 days overdue — near baseline.
        _make_defect("DEF_S1_04", 16.8, LINE_DN_FAST, DefectType.WELD_FAILURE, 76,
                     days_overdue=2.0, has_tsr=False),
        # Sector 2 — Kalyan–Karjat double (KM 54–100)
        # Weld failure; 22 days overdue — high urgency.
        _make_defect("DEF_S2_01", 67.5, LINE_DN, DefectType.WELD_FAILURE, 84,
                     days_overdue=22.0, has_tsr=False),
        # OHE sag causes active 30 km/h TSR on UP; 12 days overdue — high urgency.
        _make_defect("DEF_S2_02", 86.4, LINE_UP, DefectType.OHE_SAG, 87,
                     days_overdue=12.0, has_tsr=True),
        # Tamping logged; within acceptable window, no TSR.
        _make_defect("DEF_S2_03", 68.2, LINE_DN, DefectType.TAMPING_REQUIRED, 54,
                     days_overdue=0.0, has_tsr=False),
        # Sector 3 — Bhor Ghat triple (KM 100–128)
        # Critical rail fracture at severity 96; 28 days overdue + 25 km/h TSR imposed.
        _make_defect("DEF_S3_01", 102.3, LINE_DN, DefectType.RAIL_FRACTURE, 96,
                     days_overdue=28.0, has_tsr=True),
        # Signal interlock on mid-ghat line; 9 days overdue.
        _make_defect("DEF_S3_MID_02", 110.5, LINE_MID, DefectType.SIGNAL_INTERLOCK, 86,
                     days_overdue=9.0, has_tsr=False),
        # OHE sag on mid-ghat with active TSR; 14 days overdue.
        _make_defect("DEF_S3_MID_03", 111.8, LINE_MID, DefectType.OHE_SAG, 81,
                     days_overdue=14.0, has_tsr=True),
        # Weld failure; recent detection, 1 day overdue.
        _make_defect("DEF_S3_04", 121.5, LINE_UP, DefectType.WELD_FAILURE, 73,
                     days_overdue=1.0, has_tsr=False),
        # Sector 4 — Pune plateau double (KM 128–192)
        # Deep screening; 7 days overdue — moderate urgency.
        _make_defect("DEF_S4_01", 156.0, LINE_DN, DefectType.DEEP_SCREENING, 64,
                     days_overdue=7.0, has_tsr=False),
        # Tamping; fresh entry in SMMS, well within window.
        _make_defect("DEF_S4_02", 189.5, LINE_UP, DefectType.TAMPING_REQUIRED, 52,
                     days_overdue=0.0, has_tsr=False),
        # Weld failure; 3 days overdue — near baseline.
        _make_defect("DEF_S4_03", 174.6, LINE_DN, DefectType.WELD_FAILURE, 71,
                     days_overdue=3.0, has_tsr=False),
    )


def get_active_maintenance_bundles() -> tuple[MaintenanceBundle, ...]:
    """All spatial clusters for concurrent (not sequential) corridor planning."""
    return cluster_defects_into_bundles(get_default_defects())


DEFAULT_DEFECTS: tuple[TrackDefect, ...] = get_default_defects()
DEFAULT_BUNDLES: tuple[MaintenanceBundle, ...] = cluster_defects_into_bundles(DEFAULT_DEFECTS)

__all__ = [
    "APPROACH_FOOTPRINT_KM",
    "BlockType",
    "DEFAULT_BUNDLES",
    "DEFAULT_DEFECTS",
    "DEFAULT_SPATIAL_GAP_KM",
    "DEFECT_TYPE_SPEC",
    "DEPARTURE_FOOTPRINT_KM",
    "DIVISION_MACHINERY_LIMITS",
    "DOWNSTREAM_CASCADE_DECAY",
    "DefectType",
    "GROUPING_BONUS",
    "KNOCK_ON_DELAY_WEIGHTS",
    "MACHINE_RESTRICTION_RANK",
    "MANDATORY_SEVERITY_THRESHOLD",
    "MOBILIZATION_BUFFER_MIN",
    "MachineAllocation",
    "MachineType",
    "MaintenanceBundle",
    "POWER_SUB_SECTORS",
    "POWER_SUB_SECTORS_BY_ID",
    "PowerSubSector",
    "SAFETY_CLEARANCE_KM",
    "SLOT_GRANULARITY_MIN",
    "SOURCE_SMMS",
    "SOURCE_TDMS",
    "SOURCE_TMS",
    "SpatialFootprint",
    "TrackDefect",
    "allocate_machinery",
    "align_slot_minute",
    "cluster_defects_into_bundles",
    "compute_priority_urgency_score",
    "estimate_traffic_density",
    "downstream_cascade_penalty",
    "get_active_maintenance_bundles",
    "get_default_defects",
    "get_power_subsector",
    "get_power_subsector_for_location",
    "knock_on_delay_weight",
    "machinery_contention_groups",
]