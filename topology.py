"""
topology.py
====================================================================
Central Railway physical corridor graph: CSMT (KM 0.0) → Pune Jn (KM 192.0)

Section 1  KM 0.0–54.0    Suburban quadruple   UP_FAST, DN_FAST, UP_SLOW, DN_SLOW
Section 2  KM 54.0–100.0  Kalyan–Karjat double UP, DN
Section 3  KM 100.0–128.0 Bhor Ghat triple     UP, MID, DN
Section 4  KM 128.0–192.0 Pune plateau double  UP, DN

Contains station nodes, track segments, and crossover switch graphs only.
No solver constraints or timetable parsing.
====================================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Iterable, Mapping, Sequence

CORRIDOR_START_KM: Final[float] = 0.0
CORRIDOR_END_KM: Final[float] = 192.0

SECTION_1_ID: Final[str] = "SECTION_1"
SECTION_2_ID: Final[str] = "SECTION_2"
SECTION_3_ID: Final[str] = "SECTION_3"
SECTION_4_ID: Final[str] = "SECTION_4"

# Inclusive bounds: left endpoint belongs to the downstream section except KM 0.
SECTION_BOUNDS: Final[dict[str, tuple[float, float]]] = {
    SECTION_1_ID: (0.0, 54.0),
    SECTION_2_ID: (54.0, 100.0),
    SECTION_3_ID: (100.0, 128.0),
    SECTION_4_ID: (128.0, 192.0),
}

LINE_UP_FAST: Final[str] = "UP_FAST"
LINE_DN_FAST: Final[str] = "DN_FAST"
LINE_UP_SLOW: Final[str] = "UP_SLOW"
LINE_DN_SLOW: Final[str] = "DN_SLOW"
LINE_UP: Final[str] = "UP"
LINE_DN: Final[str] = "DN"
LINE_MID: Final[str] = "MID"

LINES_BY_SECTION: Final[dict[str, tuple[str, ...]]] = {
    SECTION_1_ID: (LINE_UP_FAST, LINE_DN_FAST, LINE_UP_SLOW, LINE_DN_SLOW),
    SECTION_2_ID: (LINE_UP, LINE_DN),
    SECTION_3_ID: (LINE_UP, LINE_MID, LINE_DN),
    SECTION_4_ID: (LINE_UP, LINE_DN),
}

# Contiguous chainage over which a named running line is physically present.
LINE_EXTENT_KM: Final[dict[str, tuple[float, float]]] = {
    LINE_UP_FAST: (0.0, 54.0),
    LINE_DN_FAST: (0.0, 54.0),
    LINE_UP_SLOW: (0.0, 54.0),
    LINE_DN_SLOW: (0.0, 54.0),
    LINE_UP: (54.0, 192.0),
    LINE_DN: (54.0, 192.0),
    LINE_MID: (100.0, 128.0),
}

KIND_SCISSORS: Final[str] = "scissors"
KIND_LADDER: Final[str] = "ladder"
KIND_JUNCTION: Final[str] = "junction"


@dataclass(frozen=True, slots=True)
class StationNode:
    """A chainage node on the CSMT–Pune main line."""

    id: int
    code: str
    name: str
    km: float
    zone: str
    elevation_m: float
    lat: float
    lng: float
    section_id: str
    has_crossover: bool


@dataclass(frozen=True, slots=True)
class TrackSegment:
    """A named running line between two successive station chainages."""

    id: str
    line: str
    start_km: float
    end_km: float
    start_station: str
    end_station: str
    section_id: str
    tracks_in_section: int


@dataclass(frozen=True, slots=True)
class CrossoverSwitch:
    """A physical turnout connecting two running lines at a station km."""

    id: str
    station_code: str
    km: float
    from_line: str
    to_line: str
    kind: str
    bidirectional: bool = True

    def connects(self, line: str) -> bool:
        return line == self.from_line or line == self.to_line

    def other_line(self, line: str) -> str:
        if line == self.from_line:
            return self.to_line
        if line == self.to_line:
            return self.from_line
        raise ValueError(f"Line {line!r} is not incident to switch {self.id}")


def _require_corridor_km(km: float) -> float:
    chainage = float(km)
    if chainage < CORRIDOR_START_KM or chainage > CORRIDOR_END_KM:
        raise ValueError(
            f"Chainage KM {chainage} is outside the corridor "
            f"[{CORRIDOR_START_KM}, {CORRIDOR_END_KM}]"
        )
    return chainage


def get_section_id_at_km(km: float) -> str:
    """
    Map chainage to the owning section.

    Junction kilometres stay with the upstream formation:
    KM 54.0 Kalyan (quadruple), KM 100.0 Karjat (double), KM 128.0 Lonavala (triple).
    """
    chainage = _require_corridor_km(km)
    if chainage <= SECTION_BOUNDS[SECTION_1_ID][1]:
        return SECTION_1_ID
    if chainage <= SECTION_BOUNDS[SECTION_2_ID][1]:
        return SECTION_2_ID
    if chainage <= SECTION_BOUNDS[SECTION_3_ID][1]:
        return SECTION_3_ID
    return SECTION_4_ID


def get_available_lines_at_km(km: float) -> list[str]:
    """Return the running lines physically present at chainage `km`."""
    section_id = get_section_id_at_km(km)
    return list(LINES_BY_SECTION[section_id])


def _line_exists_at_km(line: str, km: float) -> bool:
    extent = LINE_EXTENT_KM.get(line)
    if extent is None:
        return False
    start_km, end_km = extent
    return start_km <= km <= end_km


def get_reachable_crossovers(line: str, km: float) -> list[CrossoverSwitch]:
    """
    Return every crossover incident to `line` that a movement at `km` can
    reach by remaining on that named running line (no implied line change).

    Results are ordered by increasing absolute distance from `km`, then by km.
    """
    chainage = _require_corridor_km(km)
    running_line = str(line).upper()
    if running_line not in LINE_EXTENT_KM:
        raise ValueError(f"Unknown running line {line!r}")
    if not _line_exists_at_km(running_line, chainage):
        present = get_available_lines_at_km(chainage)
        raise ValueError(
            f"Line {running_line!r} is not present at KM {chainage}; "
            f"available lines: {present}"
        )

    start_km, end_km = LINE_EXTENT_KM[running_line]
    reachable = [
        switch
        for switch in CROSSOVERS
        if switch.connects(running_line) and start_km <= switch.km <= end_km
    ]
    reachable.sort(key=lambda s: (abs(s.km - chainage), s.km, s.id))
    return reachable


def get_station_by_code(code: str) -> StationNode:
    key = str(code).upper()
    for station in STATIONS:
        if station.code == key:
            return station
    raise KeyError(f"Unknown station code {code!r}")


def get_crossovers_at_station(code: str) -> list[CrossoverSwitch]:
    key = str(code).upper()
    return [switch for switch in CROSSOVERS if switch.station_code == key]


def get_track_segments_for_line(line: str) -> list[TrackSegment]:
    running_line = str(line).upper()
    return [segment for segment in TRACK_SEGMENTS if segment.line == running_line]


def _station(
    node_id: int,
    code: str,
    name: str,
    km: float,
    zone: str,
    elevation_m: float,
    lat: float,
    lng: float,
    section_id: str,
    has_crossover: bool,
) -> StationNode:
    return StationNode(
        id=node_id,
        code=code,
        name=name,
        km=km,
        zone=zone,
        elevation_m=elevation_m,
        lat=lat,
        lng=lng,
        section_id=section_id,
        has_crossover=has_crossover,
    )


def _build_stations() -> tuple[StationNode, ...]:
    return (
        _station(0, "CSMT", "Mumbai CSMT", 0.0, "SUBURBAN", 10.0, 18.9402, 72.8356, SECTION_1_ID, False),
        _station(1, "DR", "Dadar", 9.0, "SUBURBAN", 12.0, 19.0178, 72.8478, SECTION_1_ID, True),
        _station(2, "TNA", "Thane", 33.0, "SUBURBAN", 15.0, 19.1860, 72.9756, SECTION_1_ID, True),
        _station(3, "KYN", "Kalyan Jn", 54.0, "SUBURBAN", 20.0, 19.2364, 73.1306, SECTION_1_ID, True),
        _station(4, "BUD", "Badlapur", 67.0, "SUBURBAN", 30.0, 19.1541, 73.2625, SECTION_2_ID, True),
        _station(5, "NRL", "Neral Jn", 87.0, "SUBURBAN", 40.0, 19.0270, 73.3190, SECTION_2_ID, True),
        _station(6, "KJT", "Karjat Jn", 100.0, "SUBURBAN", 100.0, 18.9110, 73.3240, SECTION_2_ID, True),
        _station(7, "PDI", "Palasdari", 103.0, "GHAT", 120.0, 18.8870, 73.3420, SECTION_3_ID, True),
        _station(8, "JBC", "Jambrung Cabin", 108.0, "GHAT", 250.0, 18.8450, 73.3600, SECTION_3_ID, True),
        _station(9, "TKW", "Thakurvadi", 113.0, "GHAT", 360.0, 18.8050, 73.3750, SECTION_3_ID, True),
        _station(10, "MHC", "Monkey Hill Cabin", 121.0, "GHAT", 480.0, 18.7750, 73.3980, SECTION_3_ID, True),
        _station(11, "KND", "Khandala", 124.0, "GHAT", 580.0, 18.7610, 73.3730, SECTION_3_ID, True),
        _station(12, "LNL", "Lonavala", 128.0, "GHAT", 620.0, 18.7520, 73.4070, SECTION_3_ID, True),
        _station(13, "TGN", "Talegaon", 158.0, "PUNE", 600.0, 18.7340, 73.6820, SECTION_4_ID, True),
        _station(14, "DEHR", "Dehu Road", 167.0, "PUNE", 590.0, 18.7180, 73.7660, SECTION_4_ID, False),
        _station(15, "CCH", "Chinchwad", 175.0, "PUNE", 570.0, 18.6360, 73.7910, SECTION_4_ID, True),
        _station(16, "PMP", "Pimpri", 178.0, "PUNE", 565.0, 18.6250, 73.8010, SECTION_4_ID, False),
        _station(17, "SVJR", "Shivajinagar", 189.0, "PUNE", 560.0, 18.5320, 73.8520, SECTION_4_ID, False),
        _station(18, "PUNE", "Pune Jn", 192.0, "PUNE", 560.0, 18.5284, 73.8739, SECTION_4_ID, True),
    )


def _build_track_segments(stations: Sequence[StationNode]) -> tuple[TrackSegment, ...]:
    segments: list[TrackSegment] = []
    ordered = tuple(sorted(stations, key=lambda s: s.km))
    for index in range(len(ordered) - 1):
        start = ordered[index]
        end = ordered[index + 1]
        mid_km = (start.km + end.km) / 2.0
        section_id = get_section_id_at_km(mid_km)
        lines = LINES_BY_SECTION[section_id]
        track_count = len(lines)
        for line in lines:
            segments.append(
                TrackSegment(
                    id=f"{line}:{start.code}-{end.code}",
                    line=line,
                    start_km=start.km,
                    end_km=end.km,
                    start_station=start.code,
                    end_station=end.code,
                    section_id=section_id,
                    tracks_in_section=track_count,
                )
            )
    return tuple(segments)


def _switch(
    station: StationNode,
    from_line: str,
    to_line: str,
    kind: str,
) -> CrossoverSwitch:
    return CrossoverSwitch(
        id=f"XO-{station.code}-{from_line}-{to_line}",
        station_code=station.code,
        km=station.km,
        from_line=from_line,
        to_line=to_line,
        kind=kind,
        bidirectional=True,
    )


def _pair_switches(
    station: StationNode,
    pairs: Iterable[tuple[str, str]],
    kind: str,
) -> list[CrossoverSwitch]:
    return [_switch(station, a, b, kind) for a, b in pairs]


def _build_crossovers(by_code: Mapping[str, StationNode]) -> tuple[CrossoverSwitch, ...]:
    switches: list[CrossoverSwitch] = []

    # Section 1 — suburban quadruple. Track order: UP_FAST, DN_FAST, UP_SLOW, DN_SLOW.
    # Dadar: two independent scissors (fast pair and slow pair).
    switches.extend(
        _pair_switches(
            by_code["DR"],
            (
                (LINE_UP_FAST, LINE_DN_FAST),
                (LINE_UP_SLOW, LINE_DN_SLOW),
            ),
            KIND_SCISSORS,
        )
    )
    # Thane: scissors on each pair plus the fast/slow interface (orange bar across four roads).
    switches.extend(
        _pair_switches(
            by_code["TNA"],
            (
                (LINE_UP_FAST, LINE_DN_FAST),
                (LINE_DN_FAST, LINE_UP_SLOW),
                (LINE_UP_SLOW, LINE_DN_SLOW),
            ),
            KIND_SCISSORS,
        )
    )
    # Kalyan Jn: intra-quad crossovers and 4-to-2 merge onto the main UP/DN.
    switches.extend(
        _pair_switches(
            by_code["KYN"],
            (
                (LINE_UP_FAST, LINE_DN_FAST),
                (LINE_UP_SLOW, LINE_DN_SLOW),
                (LINE_UP_FAST, LINE_UP_SLOW),
                (LINE_DN_FAST, LINE_DN_SLOW),
                (LINE_UP_FAST, LINE_UP),
                (LINE_UP_SLOW, LINE_UP),
                (LINE_DN_FAST, LINE_DN),
                (LINE_DN_SLOW, LINE_DN),
            ),
            KIND_JUNCTION,
        )
    )

    # Section 2 — Kalyan–Karjat double. Scissors UP ↔ DN at Badlapur, Neral, Karjat.
    for code in ("BUD", "NRL", "KJT"):
        switches.extend(
            _pair_switches(by_code[code], ((LINE_UP, LINE_DN),), KIND_SCISSORS)
        )

    # Section 3 — Bhor Ghat three-track ladder: DN ↔ MID and MID ↔ UP at every cabin.
    ghat_codes = ("PDI", "JBC", "TKW", "MHC", "KND", "LNL")
    for code in ghat_codes:
        switches.extend(
            _pair_switches(
                by_code[code],
                (
                    (LINE_DN, LINE_MID),
                    (LINE_MID, LINE_UP),
                ),
                KIND_LADDER,
            )
        )

    # Section 4 — Pune plateau double. Crossovers at Talegaon, Chinchwad, Pune Jn.
    for code in ("TGN", "CCH", "PUNE"):
        switches.extend(
            _pair_switches(by_code[code], ((LINE_UP, LINE_DN),), KIND_SCISSORS)
        )

    return tuple(switches)


STATIONS: tuple[StationNode, ...] = _build_stations()
STATIONS_BY_CODE: dict[str, StationNode] = {station.code: station for station in STATIONS}
TRACK_SEGMENTS: tuple[TrackSegment, ...] = _build_track_segments(STATIONS)
CROSSOVERS: tuple[CrossoverSwitch, ...] = _build_crossovers(STATIONS_BY_CODE)

__all__ = [
    "CORRIDOR_END_KM",
    "CORRIDOR_START_KM",
    "CROSSOVERS",
    "CrossoverSwitch",
    "KIND_JUNCTION",
    "KIND_LADDER",
    "KIND_SCISSORS",
    "LINE_DN",
    "LINE_DN_FAST",
    "LINE_DN_SLOW",
    "LINE_EXTENT_KM",
    "LINE_MID",
    "LINE_UP",
    "LINE_UP_FAST",
    "LINE_UP_SLOW",
    "LINES_BY_SECTION",
    "SECTION_1_ID",
    "SECTION_2_ID",
    "SECTION_3_ID",
    "SECTION_4_ID",
    "SECTION_BOUNDS",
    "STATIONS",
    "STATIONS_BY_CODE",
    "StationNode",
    "TRACK_SEGMENTS",
    "TrackSegment",
    "get_available_lines_at_km",
    "get_crossovers_at_station",
    "get_reachable_crossovers",
    "get_section_id_at_km",
    "get_station_by_code",
    "get_track_segments_for_line",
]
