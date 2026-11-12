"""The normalized metric shape shared by all providers."""
from __future__ import annotations

from .constants import SESSION_WINDOW, WEEK_WINDOW
from .util import iso, parse_time


def metric(key, label, percent=None, text=None, resets_at=None, window=None, idle=False):
    """Build one metric in the normalized output shape.

    Parameters
    ----------
    key : str
        Stable identifier, e.g. ``"session"`` or ``"weekly_all"``.
    label : str
        Display name.
    percent : float, optional
        Share of the limit used, 0-100 (may exceed 100); clamped at 0 and
        rounded to 0.1.
    text : str, optional
        Display value; defaults to ``"idle"``, the rounded percent, or ``"—"``.
    resets_at : str, int, float or datetime.datetime, optional
        When the limit resets; anything `parse_time` accepts.
    window : int, optional
        Length of the limit's window, in seconds.
    idle : bool, default False
        True when the window has not started (no session running).

    Returns
    -------
    dict
        ``key``, ``label``, ``percent``, ``text``, ``resets_at`` (ISO string or
        None), ``window_seconds`` and ``idle``.
    """
    if percent is not None:
        percent = max(0.0, round(float(percent), 1))
    if text is None:
        text = "idle" if idle else (f"{percent:.0f}%" if percent is not None else "—")
    return {"key": key, "label": label, "percent": percent, "text": text,
            "resets_at": iso(parse_time(resets_at)), "window_seconds": window, "idle": bool(idle)}


def window_label(seconds, fallback):
    """Name a rate-limit window from its length.

    Parameters
    ----------
    seconds : int or None
        Window length.
    fallback : str
        Returned when `seconds` is unknown.

    Returns
    -------
    str
        ``"Session · 5h"``, ``"Weekly"``, ``"N-day window"`` or ``"Nh window"``.
    """
    if not seconds:
        return fallback
    if abs(seconds - SESSION_WINDOW) < 60:
        return "Session · 5h"
    if abs(seconds - WEEK_WINDOW) < 3600:
        return "Weekly"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}-day window"
    return f"{round(seconds / 3600)}h window"


def decay(m, now):
    """Reset a metric whose window has ended, even if the data is old.

    Parameters
    ----------
    m : dict
        Metric from `metric`.
    now : datetime.datetime

    Returns
    -------
    dict
        `m` itself, or a copy at 0 % (idle for the session) with no reset
        time.
    """
    reset = parse_time(m.get("resets_at"))
    if reset and reset <= now:
        m = dict(m, percent=0.0 if m.get("percent") is not None else None, resets_at=None,
                 idle=m.get("key") == "session")
        m["text"] = "idle" if m["idle"] else ("0% (reset)" if m["percent"] is not None else m["text"])
    return m
