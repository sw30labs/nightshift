from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from nightshift.graph import next_halt


def test_next_halt_candidate_before_halt():
    """Candidate before halt time: halt is today at halt_at."""
    now = datetime(2026, 9, 2, 3, 0, 0)  # 03:00, halt_at 06:00
    halt = next_halt("06:00", now)
    assert halt == now.replace(hour=6, minute=0, second=0, microsecond=0)
    assert halt > now


def test_next_halt_candidate_after_halt():
    """Candidate after halt time: halt is tomorrow at halt_at."""
    now = datetime(2026, 9, 2, 8, 0, 0)  # 08:00, halt_at 06:00
    halt = next_halt("06:00", now)
    expected = now.replace(hour=6, minute=0, second=0, microsecond=0) + timedelta(days=1)
    assert halt == expected
    assert halt > now


def test_next_halt_exact_halt_time():
    """Now exactly at halt time: halt is tomorrow (not today)."""
    now = datetime(2026, 9, 2, 6, 0, 0)
    halt = next_halt("06:00", now)
    expected = now.replace(hour=6, minute=0, second=0, microsecond=0) + timedelta(days=1)
    assert halt == expected


def test_next_halt_invalid_string_falls_back():
    """Invalid halt_at string falls back to 06:00."""
    now = datetime(2026, 9, 2, 3, 0, 0)
    halt = next_halt("not-a-time", now)
    assert halt == now.replace(hour=6, minute=0, second=0, microsecond=0)


def test_next_halt_empty_string_falls_back():
    """Empty halt_at string falls back to 06:00."""
    now = datetime(2026, 9, 2, 3, 0, 0)
    halt = next_halt("", now)
    assert halt == now.replace(hour=6, minute=0, second=0, microsecond=0)


def test_next_halt_none_falls_back():
    """None halt_at falls back to 06:00."""
    now = datetime(2026, 9, 2, 3, 0, 0)
    halt = next_halt(None, now)  # type: ignore[arg-type]
    assert halt == now.replace(hour=6, minute=0, second=0, microsecond=0)


def test_next_halt_out_of_range_hh_mm_falls_back():
    """Out-of-range HH:MM like 25:99 falls back to 06:00 instead of raising ValueError."""
    now = datetime(2026, 9, 2, 3, 0, 0)
    halt = next_halt("25:99", now)
    assert halt == now.replace(hour=6, minute=0, second=0, microsecond=0)


def test_next_halt_negative_hour_falls_back():
    """Negative hour falls back to 06:00."""
    now = datetime(2026, 9, 2, 3, 0, 0)
    halt = next_halt("-1:00", now)
    assert halt == now.replace(hour=6, minute=0, second=0, microsecond=0)


def test_next_halt_midnight():
    """Halt at 00:00, now at 23:00: halt is tomorrow 00:00."""
    now = datetime(2026, 9, 2, 23, 0, 0)
    halt = next_halt("00:00", now)
    expected = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    assert halt == expected


def test_next_halt_midnight_before():
    """Halt at 00:00, now at 22:00: halt is today 00:00? No, now > 00:00 so tomorrow."""
    now = datetime(2026, 9, 2, 22, 0, 0)
    halt = next_halt("00:00", now)
    expected = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    assert halt == expected


def test_next_halt_23_59():
    """Halt at 23:59, now at 22:00: halt is today 23:59."""
    now = datetime(2026, 9, 2, 22, 0, 0)
    halt = next_halt("23:59", now)
    assert halt == now.replace(hour=23, minute=59, second=0, microsecond=0)


def test_next_halt_23_59_after():
    """Halt at 23:59, now at 23:59: halt is tomorrow 23:59."""
    now = datetime(2026, 9, 2, 23, 59, 0)
    halt = next_halt("23:59", now)
    expected = now.replace(hour=23, minute=59, second=0, microsecond=0) + timedelta(days=1)
    assert halt == expected


def test_next_halt_seconds_ignored():
    """Seconds in now are zeroed by replace; halt time ignores seconds."""
    now = datetime(2026, 9, 2, 3, 15, 30, 123456)
    halt = next_halt("06:00", now)
    assert halt == now.replace(hour=6, minute=0, second=0, microsecond=0)
    assert halt.second == 0
    assert halt.microsecond == 0


def test_next_halt_extra_colon_falls_back():
    """Extra colon like 06:00:00 falls back to 06:00."""
    now = datetime(2026, 9, 2, 3, 0, 0)
    halt = next_halt("06:00:00", now)
    assert halt == now.replace(hour=6, minute=0, second=0, microsecond=0)


def test_next_halt_non_numeric_falls_back():
    """Non-numeric like ab:cd falls back to 06:00."""
    now = datetime(2026, 9, 2, 3, 0, 0)
    halt = next_halt("ab:cd", now)
    assert halt == now.replace(hour=6, minute=0, second=0, microsecond=0)
