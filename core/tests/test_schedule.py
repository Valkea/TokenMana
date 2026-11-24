"""Auto-ping schedules: parsing, local slots and the per-run decision."""
import datetime as dt

import pytest

from tests.helpers import local, utc
from tokenmana.schedule import (
    AUTO_PING_GAP, auto_ping_conf, auto_ping_info, auto_ping_sig, auto_ping_step, local_slots, next_slot,
    parse_auto_ping, parse_ping_times,
)
from tokenmana.util import iso

INTERVAL = {"mode": "interval"}
TIMES = {"mode": "times", "times": ["08:00", "13:00"]}
NOW = utc("2026-01-15T12:00:00Z")


def session(resets_at, idle=False):
    """A cached state.json entry with a 5 h session metric."""
    return {"status": "ok", "data": {"metrics": [{"key": "session", "idle": idle, "resets_at": resets_at}]}}


def started(at):
    """A record for `TIMES` created by a run at `at`."""
    e = {}
    assert auto_ping_step(TIMES, {}, e, at) is None
    return e


# Parsing

def test_ping_times_accept_loose_spellings_and_normalize():
    assert parse_ping_times("13h01, 8 8:00;8.30  23") == ["08:00", "08:30", "13:01", "23:00"]
    assert parse_ping_times(["9:05", "09:05"]) == ["09:05"]


@pytest.mark.parametrize("bad", ["24:00", "8:60", "noon", "8:5"])
def test_ping_times_reject_invalid(bad):
    with pytest.raises(ValueError):
        parse_ping_times(bad)


@pytest.mark.parametrize("value, conf", [("", None), ("off", None), ("None", None), ("no", None),
                                         ("5h", INTERVAL), ("13:00,8", TIMES)])
def test_auto_ping_cli_value(value, conf):
    assert parse_auto_ping(value) == conf


def test_conf_normalizes_times():
    acc = {"type": "claude", "auto_ping": {"mode": "times", "times": ["8h"]}}
    assert auto_ping_conf(acc) == {"mode": "times", "times": ["08:00"]}


@pytest.mark.parametrize("acc", [
    {"type": "copilot", "auto_ping": INTERVAL},
    {"type": "claude", "ping": False, "auto_ping": INTERVAL},
    {"type": "claude", "auto_ping": {"mode": "times", "times": ["25:00"]}},
    {"type": "claude", "auto_ping": {"mode": "times", "times": []}},
], ids=["no-session-to-start", "ping-disabled", "invalid-time", "no-times"])
def test_conf_is_off_when_the_account_cannot_ping_or_times_are_invalid(acc):
    assert auto_ping_conf(acc) is None


# Scheduled times

def test_slots_are_local_times_in_a_half_open_interval():
    slots = local_slots(["08:00", "13:01"], local(2026, 1, 15, 8, 0), local(2026, 1, 16, 8, 0))
    # (start, end]: 08:00 on the 15th is excluded, 08:00 on the 16th included.
    assert [iso(s) for s in slots] == ["2026-01-15T12:01:00Z", "2026-01-16T07:00:00Z"]


def test_slots_follow_the_local_clock_across_dst():
    slots = local_slots(["08:00"], local(2026, 3, 28), local(2026, 3, 30))  # Paris goes to UTC+2 on the 29th
    assert [iso(s) for s in slots] == ["2026-03-28T07:00:00Z", "2026-03-29T06:00:00Z"]


def test_next_slot_rolls_over_to_the_next_day():
    assert next_slot(TIMES["times"], local(2026, 1, 15, 13, 0)) == local(2026, 1, 16, 8, 0)
    assert next_slot([], local(2026, 1, 15)) is None


# Every-5h mode

def test_interval_first_run_pings_now():
    e = {}
    assert auto_ping_step(INTERVAL, {}, e, NOW) == NOW
    assert e["sig"] == auto_ping_sig(INTERVAL)


def test_interval_waits_5h_1min_after_the_last_ping():
    last = NOW - dt.timedelta(hours=5)
    e = {"sig": auto_ping_sig(INTERVAL), "last_ping": iso(last)}
    assert auto_ping_step(INTERVAL, {}, e, NOW) is None
    due = last + dt.timedelta(seconds=AUTO_PING_GAP)
    assert auto_ping_step(INTERVAL, {}, e, due) == due


def test_interval_running_session_blocks_and_clears_an_old_failure():
    e = {"sig": auto_ping_sig(INTERVAL), "error": "boom", "retry_at": iso(NOW + dt.timedelta(minutes=5))}
    assert auto_ping_step(INTERVAL, session("2026-01-15T14:00:00Z"), e, NOW) is None
    assert e["error"] is None
    assert e["retry_at"] is None


@pytest.mark.parametrize("entry", [session("2026-01-15T14:00:00Z", idle=True), session("2026-01-15T11:00:00Z")],
                         ids=["idle", "ended"])
def test_interval_idle_or_ended_session_does_not_block(entry):
    assert auto_ping_step(INTERVAL, entry, {}, NOW) == NOW


def test_interval_waits_for_retry_after_a_failure():
    e = {"sig": auto_ping_sig(INTERVAL), "retry_at": iso(NOW + dt.timedelta(minutes=1))}
    assert auto_ping_step(INTERVAL, {}, e, NOW) is None
    assert auto_ping_step(INTERVAL, {}, e, NOW + dt.timedelta(minutes=1)) is not None


def test_account_not_signed_in_here_is_never_pinged():
    assert auto_ping_step(INTERVAL, {"status": "setup"}, {}, NOW) is None
    e = {"sig": auto_ping_sig(TIMES), "handled_until": iso(local(2026, 1, 15, 7, 0))}
    assert auto_ping_step(TIMES, {"status": "setup"}, e, local(2026, 1, 15, 8, 1)) is None


# Set-times mode

def test_new_schedule_asks_nothing_about_the_past():
    e = started(local(2026, 1, 15, 8, 2))  # 08:00 already passed when the schedule was set
    assert e["missed"] is None
    assert e["handled_until"] == iso(local(2026, 1, 15, 8, 2))


def test_pings_a_slot_once_within_the_grace_period():
    e = started(local(2026, 1, 15, 7, 59))
    assert auto_ping_step(TIMES, {}, e, local(2026, 1, 15, 8, 5)) == local(2026, 1, 15, 8, 0)
    assert auto_ping_step(TIMES, {}, e, local(2026, 1, 15, 8, 6)) is None  # handled: no double ping


def test_slept_through_slots_become_one_pending_question():
    e = started(local(2026, 1, 15, 7, 0))
    assert auto_ping_step(TIMES, {}, e, local(2026, 1, 15, 14, 0)) is None  # missed 08:00 and 13:00
    assert e["missed"] == iso(local(2026, 1, 15, 13, 0))
    assert auto_ping_step(TIMES, {}, e, local(2026, 1, 15, 14, 1)) is None
    assert e["missed"] == iso(local(2026, 1, 15, 13, 0)), "the question stays until answered"


def test_running_session_drops_the_question():
    e = started(local(2026, 1, 15, 7, 0))
    busy = session(iso(local(2026, 1, 15, 11, 0)))
    assert auto_ping_step(TIMES, busy, e, local(2026, 1, 15, 8, 30)) is None
    assert e["missed"] is None


def test_changing_the_schedule_resets_the_record():
    e = started(local(2026, 1, 15, 7, 0))
    auto_ping_step(TIMES, {}, e, local(2026, 1, 15, 9, 0))
    assert e["missed"] is not None
    other = {"mode": "times", "times": ["18:00"]}
    assert auto_ping_step(other, {}, e, local(2026, 1, 15, 9, 1)) is None
    assert e["missed"] is None
    assert e["sig"] == auto_ping_sig(other)


# What the UIs show

ACC = {"id": "c", "type": "claude", "auto_ping": INTERVAL}


def test_interval_next_at_is_the_latest_constraint():
    e = {"sig": auto_ping_sig(INTERVAL), "last_ping": "2026-01-15T11:00:00Z", "retry_at": "2026-01-15T12:10:00Z"}
    info = auto_ping_info(ACC, e, session("2026-01-15T14:00:00Z"), NOW)
    assert info["next_at"] == "2026-01-15T16:01:00Z"  # last ping + 5 h 1 min
    info = auto_ping_info(ACC, e, session("2026-01-15T16:30:00Z"), NOW)
    assert info["next_at"] == "2026-01-15T16:31:00Z"  # session end + 1 min


def test_record_of_an_older_schedule_shows_no_question_or_error():
    e = {"sig": "old", "missed": "2026-01-15T07:00:00Z", "error": "boom", "retry_at": "2026-01-15T15:00:00Z"}
    info = auto_ping_info(ACC, e, {}, NOW)
    assert (info["missed"], info["error"], info["next_at"]) == (None, None, iso(NOW))


def test_times_next_at_is_the_next_local_slot():
    info = auto_ping_info(dict(ACC, auto_ping=TIMES), None, {}, local(2026, 1, 15, 10, 0))
    assert info["next_at"] == iso(local(2026, 1, 15, 13, 0))


def test_off_gives_no_info():
    assert auto_ping_info({"id": "c", "type": "claude"}, {}, {}, NOW) is None
