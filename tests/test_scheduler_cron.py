"""Cron-kind schedules: parse_cron field semantics (steps, lists, ranges,
dom/dow OR-semantics, 7=Sunday) and the store's cron advancement +
set_enabled/set_status helpers."""
import time
from datetime import datetime

import pytest

from runtime.scheduler import ScheduleStore, parse_cron


def _local(y, mo, d, h, mi):
    """Epoch seconds for a local wall-clock moment (parse_cron is local-time)."""
    return datetime(y, mo, d, h, mi).astimezone().timestamp()


def _cron_dow(ts):
    return (time.localtime(ts).tm_wday + 1) % 7   # cron: Sunday=0


# ---- parse_cron ----
def test_every_minute_fires_at_next_minute_boundary():
    now = _local(2026, 10, 14, 12, 34) + 17.5    # mid-minute
    assert parse_cron("* * * * *", now) == float(int(now // 60) * 60 + 60)


def test_step_field():
    now = _local(2026, 10, 14, 12, 34)
    nxt = parse_cron("*/15 * * * *", now)
    lt = time.localtime(nxt)
    assert lt.tm_min % 15 == 0 and lt.tm_sec == 0
    assert 0 < nxt - now <= 900


def test_lists_and_fixed_time():
    now = _local(2026, 10, 14, 8, 0)
    nxt = parse_cron("0,30 9 * * *", now)
    lt = time.localtime(nxt)
    assert (lt.tm_hour, lt.tm_min) == (9, 0)
    # past 09:30 the same day -> tomorrow 09:00
    now2 = _local(2026, 10, 14, 9, 45)
    lt2 = time.localtime(parse_cron("0,30 9 * * *", now2))
    assert (lt2.tm_mday, lt2.tm_hour, lt2.tm_min) == (15, 9, 0)


def test_range_and_range_step():
    now = _local(2026, 10, 14, 20, 0)
    lt = time.localtime(parse_cron("0 9-17 * * *", now))
    assert (lt.tm_mday, lt.tm_hour) == (15, 9)
    # range/step: 9,11,13,15,17 — from 10:00 the next is 11:00
    now2 = _local(2026, 10, 14, 10, 0)
    lt2 = time.localtime(parse_cron("0 9-17/2 * * *", now2))
    assert (lt2.tm_hour, lt2.tm_min) == (11, 0)
    # ...and 10:00 is NOT a match even though it's inside 9-17
    now3 = _local(2026, 10, 14, 9, 59)
    lt3 = time.localtime(parse_cron("0 9-17/2 * * *", now3))
    assert (lt3.tm_hour, lt3.tm_min) == (11, 0)


def test_month_and_dom_fields():
    now = _local(2026, 10, 14, 0, 0)
    lt = time.localtime(parse_cron("0 0 1 1 *", now))
    assert (lt.tm_year, lt.tm_mon, lt.tm_mday) == (2027, 1, 1)


def test_dom_dow_or_semantics():
    """Both dom and dow restricted -> a day matching EITHER fires."""
    now = _local(2026, 10, 14, 0, 0)
    dom_only = parse_cron("0 0 13 * *", now)
    assert time.localtime(dom_only).tm_mday == 13
    dow_only = parse_cron("0 0 * * 5", now)
    assert _cron_dow(dow_only) == 5
    assert parse_cron("0 0 13 * 5", now) == min(dom_only, dow_only)


def test_dow_7_is_sunday():
    now = _local(2026, 10, 14, 0, 0)
    nxt7, nxt0 = parse_cron("0 0 * * 7", now), parse_cron("0 0 * * 0", now)
    assert nxt7 == nxt0
    assert _cron_dow(nxt0) == 0


def test_invalid_expressions_raise():
    now = _local(2026, 10, 14, 0, 0)
    for bad in ("", "not cron at all", "* * * *", "61 * * * *", "* 25 * * *",
                "0 0 32 * *", "*/0 * * * *", "1-2-3 * * * *", "0 0 * 13 *"):
        with pytest.raises(ValueError):
            parse_cron(bad, now)


def test_impossible_date_raises():
    # February 30th never comes — the bounded scan gives up instead of hanging.
    with pytest.raises(ValueError):
        parse_cron("0 0 30 2 *", _local(2026, 10, 14, 0, 0))


def test_stepped_value_without_range_means_to_max():
    """'5/10' is 5-59/10 (standard cron semantics), not 'minute 5 only'."""
    lt = time.localtime(parse_cron("5/10 * * * *", _local(2026, 10, 14, 12, 0)))
    assert (lt.tm_hour, lt.tm_min) == (12, 5)
    lt2 = time.localtime(parse_cron("5/10 * * * *", _local(2026, 10, 14, 12, 6)))
    assert (lt2.tm_hour, lt2.tm_min) == (12, 15)


def test_impossible_dates_raise_fast():
    """Feb 30 / Apr 31 & co. never come — the day-by-day scan rejects them in
    milliseconds (the old minute-by-minute scan took ~2s and ran under the
    store lock in mark_fired, stalling the web server; code audit 2026-10-14)."""
    for bad in ("0 0 30 2 *", "0 0 31 2 *", "0 0 31 4 *", "0 0 31 6 *"):
        t0 = time.monotonic()
        with pytest.raises(ValueError):
            parse_cron(bad, _local(2026, 10, 14, 0, 0))
        assert time.monotonic() - t0 < 0.5


def test_leap_day_found():
    lt = time.localtime(parse_cron("0 0 29 2 *", _local(2026, 10, 14, 0, 0)))
    assert (lt.tm_year, lt.tm_mon, lt.tm_mday) == (2028, 2, 29)


# ---- store: cron advancement + enable/status helpers ----
def test_mark_fired_cron_advances(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.json"))
    now = _local(2026, 10, 14, 12, 34)
    e = s.add({"owner": "a", "prompt": "x", "kind": "cron",
               "cron": "*/5 * * * *", "next_fire": now - 60})
    s.mark_fired(e["id"], now)
    after = s.list()[0]
    assert after["enabled"] is True and after["fire_count"] == 1
    assert after["next_fire"] > now
    assert time.localtime(after["next_fire"]).tm_min % 5 == 0


def test_mark_fired_broken_cron_disables(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.json"))
    e = s.add({"owner": "a", "prompt": "x", "kind": "cron",
               "cron": "not a cron", "next_fire": time.time() - 1})
    s.mark_fired(e["id"])
    after = s.list()[0]
    assert after["enabled"] is False and after["fire_count"] == 1


def test_set_enabled_and_set_status(tmp_path):
    s = ScheduleStore(str(tmp_path / "s.json"))
    e = s.add({"owner": "a", "prompt": "x", "kind": "once",
               "next_fire": time.time() + 3600})
    assert s.set_enabled(e["id"], False) is True
    assert s.list()[0]["enabled"] is False
    assert s.due() == []                       # disabled is never due
    assert s.set_enabled(e["id"], True) is True
    assert s.set_status(e["id"], "ok") is True
    assert s.list()[0]["last_status"] == "ok"
    assert s.set_enabled("nope", True) is False
    assert s.set_status("nope", "ok") is False
