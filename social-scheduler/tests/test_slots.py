from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from socialq.slots import next_free_slot, parse_slot

LA = ZoneInfo("America/Los_Angeles")


def test_parse_slot_groups():
    days, at = parse_slot("weekdays 09:30")
    assert days == {0, 1, 2, 3, 4} and (at.hour, at.minute) == (9, 30)
    assert parse_slot("Monday,fri 17:00")[0] == {0, 4}


def test_parse_slot_errors():
    with pytest.raises(ValueError):
        parse_slot("someday 17:00")


def test_next_free_slot_skips_taken_and_past():
    now = datetime(2026, 10, 5, 18, 0, tzinfo=LA)  # Monday, after the Monday slot
    wed = datetime(2026, 10, 7, 17, 0, tzinfo=LA)
    got = next_free_slot(["mon,wed,fri 17:00"], [wed], now, LA)
    assert got == datetime(2026, 10, 9, 17, 0, tzinfo=LA)  # Friday
