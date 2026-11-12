"""Auto-ping schedules and their run state.

Per account in config.json (``mana edit ID --auto-ping 5h|08:00,13:01|off``)::

    "auto_ping": {"mode": "interval"}                            5 h 1 min after the last ping
    "auto_ping": {"mode": "times", "times": ["08:00", "13:01"]}  at these local times

The UIs run ``mana auto-ping`` every minute (cron works too). A scheduled time
the device was asleep or off for is not caught up on its own: it becomes a
question, "ping now, or wait for the next time?". Run state (last ping,
handled times, pending question) is kept in autoping.json.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import re

from .constants import CACHE_DIR, SCHEDULE_PATH, SESSION_WINDOW
from .providers import provider
from .util import iso, parse_time, read_json, utcnow, write_json


AUTO_PING_GAP = SESSION_WINDOW + 60   # every-5h mode: 5 h + 1 min after the last ping, so the window has ended
AUTO_PING_GRACE = 5 * 60              # a scheduled time not reached within this is "missed" (device asleep / off)
AUTO_PING_RETRY = 15 * 60             # every-5h mode: wait this long after a failed auto ping


def parse_ping_times(value):
    """Normalize a list of scheduled ping times.

    Parameters
    ----------
    value : str or list of str
        E.g. ``"8:00, 13:01"`` or ``["08:00", "13h01"]``; ``8``, ``8h`` and
        ``8.30`` are accepted too.

    Returns
    -------
    list of str
        ``HH:MM`` times, sorted, without duplicates.

    Raises
    ------
    ValueError
        When a time is not a valid 24 h time.
    """
    items = value if isinstance(value, list) else re.split(r"[\s,;]+", str(value or ""))
    out = set()
    for raw in items:
        s = str(raw).strip()
        if not s:
            continue
        m = re.fullmatch(r"(\d{1,2})(?:[:hH.](\d{2})?)?", s)
        h, mi = (int(m.group(1)), int(m.group(2) or 0)) if m else (99, 0)
        if h > 23 or mi > 59:
            raise ValueError(f"invalid time {s!r} (use HH:MM, 24 h)")
        out.add(f"{h:02d}:{mi:02d}")
    return sorted(out)


def parse_auto_ping(value):
    """Convert a ``--auto-ping`` CLI value into the account's config value.

    Parameters
    ----------
    value : str or None
        ``"off"`` (or empty), ``"5h"``, or times such as ``"08:00,13:01"``.

    Returns
    -------
    dict or None
        None for off, ``{"mode": "interval"}``, or
        ``{"mode": "times", "times": [...]}``.

    Raises
    ------
    ValueError
        When a time is invalid.
    """
    v = (value or "").strip().lower()
    if v in ("", "off", "none", "no", "false"):
        return None
    if v in ("5h", "interval", "on"):
        return {"mode": "interval"}
    times = parse_ping_times(v)
    return {"mode": "times", "times": times} if times else None


def auto_ping_conf(acc):
    """Return the account's valid auto-ping schedule.

    Parameters
    ----------
    acc : dict
        Account config.

    Returns
    -------
    dict or None
        ``{"mode": "interval"}`` or ``{"mode": "times", "times": [...]}``;
        None when auto ping is off or invalid, or the account cannot ping.
    """
    c = acc.get("auto_ping")
    if not isinstance(c, dict) or not provider(acc)["ping"] or acc.get("ping") is False:
        return None
    if c.get("mode") == "interval":
        return {"mode": "interval"}
    if c.get("mode") == "times":
        try:
            times = parse_ping_times(c.get("times") or [])
        except ValueError:
            return None
        return {"mode": "times", "times": times} if times else None
    return None


def auto_ping_sig(conf):
    """Return a stable signature of a schedule, to detect changes.

    Parameters
    ----------
    conf : dict
        Schedule from `auto_ping_conf`.

    Returns
    -------
    str
    """
    return json.dumps(conf, sort_keys=True)


def local_slots(times, start, end):
    """List the scheduled times that fall in an interval.

    Parameters
    ----------
    times : list of str
        ``HH:MM`` times on the local clock.
    start, end : datetime.datetime
        Aware bounds of the interval ``(start, end]``.

    Returns
    -------
    list of datetime.datetime
        Aware local datetimes, oldest first.
    """
    out = []
    day, last = start.astimezone().date(), end.astimezone().date()
    while day <= last:
        for t in times:
            h, m = map(int, t.split(":"))
            slot = dt.datetime(day.year, day.month, day.day, h, m).astimezone()  # naive = local time
            if start < slot <= end:
                out.append(slot)
        day += dt.timedelta(days=1)
    return out


def next_slot(times, after):
    """Return the first scheduled time after a given moment.

    Parameters
    ----------
    times : list of str
        ``HH:MM`` times on the local clock.
    after : datetime.datetime

    Returns
    -------
    datetime.datetime or None
        None when `times` is empty.
    """
    slots = local_slots(times, after, after + dt.timedelta(days=2))
    return slots[0] if slots else None


def session_busy_until(entry):
    """Return when the account's 5 h session ends, plus one minute.

    Parameters
    ----------
    entry : dict or None
        The account's cached fetch from ``state.json``.

    Returns
    -------
    datetime.datetime or None
        May be in the past if the cache is old. None when no session is known
        (idle, no reset time, or the account is not set up).
    """
    if (entry or {}).get("status") == "setup":
        return None
    for m in ((entry or {}).get("data") or {}).get("metrics") or []:
        if m.get("key") == "session" and not m.get("idle"):
            reset = parse_time(m.get("resets_at"))
            return reset + dt.timedelta(seconds=60) if reset else None
    return None


@contextlib.contextmanager
def schedule_state():
    """Lock ``autoping.json`` for a read-modify-write.

    The UI timers, manual pings and cron can run at the same time, so access
    is serialized with an exclusive ``flock``.

    Yields
    ------
    dict
        Schedule records by account id; changes are written back on exit.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(SCHEDULE_PATH + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read_json(SCHEDULE_PATH, {}) or {}
        yield data
        write_json(SCHEDULE_PATH, data)


def auto_ping_step(conf, entry, e, now):
    """Decide whether an auto ping is due for one account.

    Also updates the account's schedule record: resets it when the schedule
    changed, and turns a scheduled time missed while the device was asleep
    or off into a pending question, unless a session is already running.

    Parameters
    ----------
    conf : dict
        Schedule from `auto_ping_conf`.
    entry : dict
        The account's cached fetch from ``state.json``.
    e : dict
        The account's record in ``autoping.json``, updated in place.
    now : datetime.datetime

    Returns
    -------
    datetime.datetime or None
        The time the ping is for, or None when none is due.
    """
    sig = auto_ping_sig(conf)
    if e.get("sig") != sig:  # new or changed schedule: start from now, ask nothing about the past
        e.update(sig=sig, handled_until=iso(now), missed=None, error=None, retry_at=None)
    if entry.get("status") == "setup":  # not signed in on this device
        return None
    busy = session_busy_until(entry)
    running = busy is not None and now < busy
    if conf["mode"] == "interval":
        last, retry = parse_time(e.get("last_ping")), parse_time(e.get("retry_at"))
        if running or (retry and now < retry) or (last and now < last + dt.timedelta(seconds=AUTO_PING_GAP)):
            return None
        return now
    handled = parse_time(e.get("handled_until")) or now
    slots = local_slots(conf["times"], max(handled, now - dt.timedelta(days=7)), now)
    e["handled_until"] = iso(now)
    if slots and (now - slots[-1]).total_seconds() <= AUTO_PING_GRACE:
        e.update(missed=None, error=None)
        return slots[-1]
    if slots:  # slept through it (several missed times make one question)
        e.update(missed=iso(slots[-1]), error=None)
    if running:  # a session is already going: nothing to start, nothing to ask
        e.update(missed=None, error=None)
    return None


def auto_ping_info(acc, e, entry, now):
    """Describe an account's auto ping for the UIs.

    Parameters
    ----------
    acc : dict
        Account config.
    e : dict or None
        The account's record in ``autoping.json``.
    entry : dict or None
        The account's cached fetch from ``state.json``.
    now : datetime.datetime

    Returns
    -------
    dict or None
        ``mode``, ``times``, ``last_ping``, ``last_auto``, ``missed`` (pending
        question), ``error`` and ``next_at``; None when auto ping is off.
    """
    conf = auto_ping_conf(acc)
    if not conf:
        return None
    e = e or {}
    current = e.get("sig") == auto_ping_sig(conf)  # a record from an older schedule: only its pings count
    info = {"mode": conf["mode"], "times": conf.get("times", []), "last_ping": e.get("last_ping"),
            "last_auto": e.get("last_auto"), "missed": e.get("missed") if current else None,
            "error": e.get("error") if current else None, "next_at": None}
    if (entry or {}).get("status") == "setup":
        return info
    if conf["mode"] == "times":
        info["next_at"] = iso(next_slot(conf["times"], now))
    else:
        last = parse_time(e.get("last_ping"))
        when = [now, session_busy_until(entry), parse_time(e.get("retry_at")) if current else None,
                last + dt.timedelta(seconds=AUTO_PING_GAP) if last else None]
        info["next_at"] = iso(max(t for t in when if t))
    return info


def record_pings(results):
    """Record successful pings in the auto-ping schedule.

    A successful ping restarts the every-5h chain and answers a pending
    missed-ping question.

    Parameters
    ----------
    results : list of dict
        Results of `ping_account`.
    """
    now = iso(utcnow())
    if not any(r["ok"] for r in results):
        return
    with schedule_state() as s:
        for r in results:
            if r["ok"]:
                s.setdefault(r["id"], {}).update(last_ping=now, missed=None, error=None, retry_at=None)
