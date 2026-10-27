"""
sim_clock.py
====================================================================
The one clock for the whole project.

Elapsed real time comes from time.monotonic (replaceable in tests).
It is never taken from the laptop's wall clock. A sleeping laptop
cannot jump the clock: each advance counts at most MAX_TICK_SEC.

SIM_START_DATE is internal. Weekday rules in the existing date-based
code keep working, and nothing on screen shows that calendar date
or a month name. Stored timestamps built from now_datetime() are
simulated times.
====================================================================
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

SIM_START_DATE: date = date(2026, 11, 2)  # Monday. Internal only.
START_MINUTE: int = 1320  # 22:00
BASE_SIM_MIN_PER_REAL_SEC: float = 0.8  # 30 real minutes = 24 simulated hours
SPEED_OPTIONS: tuple[int, ...] = (1, 5, 10)  # 30, 6, and 3 real minutes per day
DAYS_PER_MONTH: int = 30
AUTO_PAUSE_AFTER_SEC: float = 15.0
MAX_TICK_SEC: float = 2.0
_MINUTES_PER_DAY: float = 1440.0
_WEEKDAY_NAMES: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_LOCK = threading.RLock()
_monotonic_source: Callable[[], float] = time.monotonic

_absolute_day: int = 0
_minute: float = float(START_MINUTE)
_multiplier: int = 1
_last_poll: float | None = None
_last_tick: float | None = None
_last_save_mono: float | None = None


def set_monotonic_source(source: Callable[[], float]) -> None:
    """Replace time.monotonic. Tests use this so a fake clock can drive advance()."""
    global _monotonic_source
    _monotonic_source = source


def _mono() -> float:
    return float(_monotonic_source())


def _is_paused_locked(now: float) -> bool:
    if _last_poll is None:
        return True
    return (now - _last_poll) >= AUTO_PAUSE_AFTER_SEC


def _normalize(absolute_day: int, minute: float) -> tuple[int, float]:
    day = int(absolute_day)
    value = round(float(minute), 6)
    if value < 0.0:
        value = 0.0
    while value >= _MINUTES_PER_DAY:
        value = round(value - _MINUTES_PER_DAY, 6)
        day += 1
    return day, value


def _add_locked(amount: float) -> bool:
    """Add simulated minutes. Caller holds the lock. True when the day rolls."""
    global _absolute_day, _minute
    if amount <= 0.0:
        return False
    _minute = round(_minute + amount, 6)
    wrapped = False
    while _minute >= _MINUTES_PER_DAY:
        _minute = round(_minute - _MINUTES_PER_DAY, 6)
        _absolute_day += 1
        wrapped = True
    return wrapped


def advance() -> None:
    """Move the clock by the real time since the last tick, capped at MAX_TICK_SEC.

    A paused clock does not move and does not keep the missed time for later.
    """
    now = _mono()
    wrapped = False
    with _LOCK:
        global _last_tick
        if _last_tick is None:
            _last_tick = now
            return
        if _is_paused_locked(now):
            _last_tick = now
            return
        elapsed = now - _last_tick
        if elapsed < 0.0:
            elapsed = 0.0
        elif elapsed > MAX_TICK_SEC:
            elapsed = MAX_TICK_SEC
        _last_tick = now
        wrapped = _add_locked(elapsed * BASE_SIM_MIN_PER_REAL_SEC * _multiplier)
    if wrapped:
        save()


def note_poll() -> None:
    """A page is watching. Called only by GET /api/clock.

    If the clock had auto-paused, it resumes from the current minute and
    does not catch up the time spent paused.
    """
    now = _mono()
    with _LOCK:
        global _last_poll, _last_tick
        was_paused = _is_paused_locked(now)
        _last_poll = now
        if was_paused or _last_tick is None:
            _last_tick = now


def absolute_day() -> int:
    with _LOCK:
        return _absolute_day


def minute_of_day() -> float:
    """Simulated minute since midnight, in [0, 1440)."""
    with _LOCK:
        return _minute


def absolute_minute() -> float:
    """Simulated minutes since the start of absolute day 0."""
    with _LOCK:
        return _absolute_day * _MINUTES_PER_DAY + _minute


def multiplier() -> int:
    with _LOCK:
        return _multiplier


def set_multiplier(multiplier: int) -> None:
    """Switch speed. Elapsed time up to this call uses the previous speed."""
    if multiplier not in SPEED_OPTIONS:
        raise ValueError("multiplier must be 1, 5, or 10")
    advance()
    with _LOCK:
        global _multiplier
        _multiplier = int(multiplier)
    save()


def reset() -> None:
    """Back to absolute day 0 at START_MINUTE. The speed is left as it is."""
    now = _mono()
    with _LOCK:
        global _absolute_day, _minute, _last_tick
        _absolute_day = 0
        _minute = float(START_MINUTE)
        _last_tick = now
    save()


def sim_date() -> date:
    """Internal calendar date. Display code must not show this."""
    with _LOCK:
        day = _absolute_day
    return SIM_START_DATE + timedelta(days=day)


def date_for_absolute_day(day: int) -> date:
    return SIM_START_DATE + timedelta(days=int(day))


def now_datetime() -> datetime:
    """Timezone-aware simulated datetime for every stored timestamp."""
    with _LOCK:
        day = _absolute_day
        minute = _minute
    calendar_day = SIM_START_DATE + timedelta(days=day)
    whole = int(minute)
    fraction = minute - whole
    hours, mins = divmod(whole, 60)
    if hours >= 24:
        hours = 23
        mins = 59
        fraction = 0.0
    seconds_float = fraction * 60.0
    seconds = int(seconds_float)
    microseconds = int(round((seconds_float - seconds) * 1_000_000))
    if microseconds >= 1_000_000:
        seconds += 1
        microseconds -= 1_000_000
    if seconds >= 60:
        seconds = 59
        microseconds = 999999
    return datetime(
        calendar_day.year,
        calendar_day.month,
        calendar_day.day,
        hours,
        mins,
        seconds,
        microseconds,
        tzinfo=timezone.utc,
    )


def _coerce_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "T" in text or " " in text:
        return datetime.fromisoformat(text).date()
    return date.fromisoformat(text[:10])


def absolute_day_of(value: date | datetime | str) -> int:
    return (_coerce_date(value) - SIM_START_DATE).days


def day_number_of(value: date | datetime | str) -> int:
    """Day-in-month (1..30) for a stored simulated date or timestamp."""
    return absolute_day_of(value) % DAYS_PER_MONTH + 1


def month_number_of(value: date | datetime | str) -> int:
    return absolute_day_of(value) // DAYS_PER_MONTH + 1


def day_in_month() -> int:
    with _LOCK:
        day = _absolute_day
    return day % DAYS_PER_MONTH + 1


def month_number() -> int:
    with _LOCK:
        day = _absolute_day
    return day // DAYS_PER_MONTH + 1


def week_in_month_for(day_number: int) -> int:
    """Days 1-7, 8-14, 15-21, and 22-30."""
    number = int(day_number)
    if number <= 7:
        return 1
    if number <= 14:
        return 2
    if number <= 21:
        return 3
    return 4


def week_in_month() -> int:
    return week_in_month_for(day_in_month())


def weekday_name_for_absolute_day(day: int) -> str:
    return _WEEKDAY_NAMES[int(day) % 7]


def weekday_name() -> str:
    with _LOCK:
        day = _absolute_day
    return weekday_name_for_absolute_day(day)


def weekday_of(value: date | datetime | str) -> str:
    return weekday_name_for_absolute_day(absolute_day_of(value))


def day_label(value: date | datetime | str | None = None) -> str:
    """For example 'Day 12 - Week 2 - Thu'. No calendar date and no month name."""
    if value is None:
        number = day_in_month()
        week = week_in_month()
        weekday = weekday_name()
    else:
        number = day_number_of(value)
        week = week_in_month_for(number)
        weekday = weekday_of(value)
    return f"Day {number} - Week {week} - {weekday}"


def short_label(value: date | datetime | str | None = None) -> str:
    """For example 'Day 12'."""
    number = day_in_month() if value is None else day_number_of(value)
    return f"Day {number}"


def compact_label(value: date | datetime | str | None = None) -> str:
    """For example 'Day 14 - Sun'."""
    if value is None:
        return f"Day {day_in_month()} - {weekday_name()}"
    return f"Day {day_number_of(value)} - {weekday_of(value)}"


def day_fields(value: date | datetime | str | None = None) -> dict[str, object]:
    """Fields placed next to an ISO date by every API that returns one."""
    if value is None:
        number = day_in_month()
        month = month_number()
        week = week_in_month()
        weekday = weekday_name()
        label = day_label()
        short = short_label()
        compact = compact_label()
    else:
        number = day_number_of(value)
        month = month_number_of(value)
        week = week_in_month_for(number)
        weekday = weekday_of(value)
        label = day_label(value)
        short = short_label(value)
        compact = compact_label(value)
    return {
        "day_number": number,
        "month_number": month,
        "day_label": label,
        "week_in_month": week,
        "weekday": weekday,
        "short_label": short,
        "compact_label": compact,
    }


def _hhmm(minute: float) -> str:
    folded = minute % _MINUTES_PER_DAY
    hours = int(folded) // 60
    mins = int(folded) % 60
    return f"{hours:02d}:{mins:02d}"


def _real_minutes_per_day(speed: int) -> int:
    return int(round(30.0 / speed))


def snapshot() -> dict[str, object]:
    now = _mono()
    with _LOCK:
        day = _absolute_day
        minute = _minute
        speed = _multiplier
        is_paused = _is_paused_locked(now)
    number = day % DAYS_PER_MONTH + 1
    weekday = weekday_name_for_absolute_day(day)
    week = week_in_month_for(number)
    return {
        "absolute_day": day,
        "day_in_month": number,
        "month_number": day // DAYS_PER_MONTH + 1,
        "week_in_month": week,
        "weekday": weekday,
        "minute": minute,
        "hhmm": _hhmm(minute),
        "multiplier": speed,
        "minutes_per_second": round(BASE_SIM_MIN_PER_REAL_SEC * speed, 6),
        "real_minutes_per_day": _real_minutes_per_day(speed),
        "paused": is_paused,
        "day_label": f"Day {number} - Week {week} - {weekday}",
    }


def save() -> None:
    """Write the single clock row. Safe to call from a request or the background tick."""
    import persistence

    now = _mono()
    with _LOCK:
        global _last_save_mono
        day = _absolute_day
        minute = _minute
        speed = _multiplier
        _last_save_mono = now
    persistence.save_sim_clock_state(day, minute, speed)


def save_if_due() -> None:
    """Save at most once a real second, and only while the clock is running."""
    now = _mono()
    with _LOCK:
        if _is_paused_locked(now):
            return
        if _last_save_mono is not None and (now - _last_save_mono) < 1.0:
            return
    save()


def restore() -> None:
    """Load the saved day, minute, and speed. Time spent while the process was off is ignored."""
    import persistence

    loaded = persistence.load_sim_clock_state()
    now = _mono()
    with _LOCK:
        global _absolute_day, _minute, _multiplier, _last_poll, _last_tick, _last_save_mono
        if loaded is None:
            _absolute_day = 0
            _minute = float(START_MINUTE)
            _multiplier = 1
        else:
            day, minute, speed = loaded
            _absolute_day, _minute = _normalize(day, minute)
            _multiplier = speed if speed in SPEED_OPTIONS else 1
        _last_poll = None
        _last_tick = now
        _last_save_mono = now
