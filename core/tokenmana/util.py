"""Helpers for time, formatting, files, executables and the desktop."""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import webbrowser

from .constants import EXTRA_PATH, IS_MAC


def expand(p):
    """Expand ``~`` and environment variables and make a path absolute.

    Parameters
    ----------
    p : str or None
        Path to expand.

    Returns
    -------
    str or None
        The absolute path, or `p` unchanged when it is empty or None.
    """
    return os.path.abspath(os.path.expandvars(os.path.expanduser(p))) if p else p


def utcnow():
    """Return the current time as a timezone-aware UTC datetime.

    Returns
    -------
    datetime.datetime
    """
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    """Format a datetime as an ISO 8601 UTC string (``YYYY-MM-DDTHH:MM:SSZ``).

    Parameters
    ----------
    t : datetime.datetime or None
        Aware datetime (a naive one is taken as local time).

    Returns
    -------
    str or None
        The formatted string, or None when `t` is None.
    """
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def parse_time(v):
    """Parse a timestamp in any of the shapes vendor APIs return.

    Accepts ISO 8601 strings (``Z`` or offset suffix, any fraction length,
    space or ``T`` separator), bare dates, epoch seconds or milliseconds
    (as numbers or numeric strings) and datetimes.

    Parameters
    ----------
    v : str, int, float, datetime.datetime or None
        Value to parse.

    Returns
    -------
    datetime.datetime or None
        Aware datetime (naive inputs are taken as UTC), or None when `v` is
        empty or cannot be parsed.
    """
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v / 1000 if v > 1e12 else v, dt.timezone.utc)
    s = str(v).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return parse_time(float(s))
    s = s.replace("Z", "+00:00").replace(" ", "T", 1)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        s += "T00:00:00+00:00"
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
    if m:
        frac = (m.group(2) or "")[:7]
        if frac:
            frac = frac.ljust(7, "0")
        s = m.group(1) + frac + m.group(3)
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def first_of_next_month(t=None):
    """Return midnight UTC on the first day of the month after `t`.

    Parameters
    ----------
    t : datetime.datetime, optional
        Reference time; defaults to now.

    Returns
    -------
    datetime.datetime
    """
    t = t or utcnow()
    y, m = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
    return dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)


def fmt_money(v, currency="USD"):
    """Format an amount of money for display.

    Amounts of 100 or more are rounded to whole units.

    Parameters
    ----------
    v : float or None
        Amount.
    currency : str, default "USD"
        ISO 4217 code. USD, EUR and GBP get their symbol; other codes are
        appended.

    Returns
    -------
    str
        E.g. ``"$12.50"``, ``"1,200 CHF"``, or ``"—"`` when `v` is None.
    """
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get((currency or "USD").upper(), "")
    if v is None:
        return "—"
    s = f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.2f}"
    return f"{sym}{s}" if sym else f"{s} {currency}"


def fmt_num(v):
    """Format a count with thousands separators, one decimal if not integral.

    Parameters
    ----------
    v : float or None

    Returns
    -------
    str
        E.g. ``"1,500"``, ``"2.5"``, or ``"—"`` when `v` is None.
    """
    if v is None:
        return "—"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.1f}"


def search_path():
    """Return the directories to search for executables.

    ``$PATH``, followed by the ``EXTRA_PATH`` install locations that exist on
    this machine (GUI apps and shell extensions start with a minimal PATH).

    Returns
    -------
    list of str
    """
    dirs = os.environ.get("PATH", "").split(os.pathsep)
    for p in EXTRA_PATH:
        for d in (sorted(glob.glob(expand(p))) if "*" in p else [expand(p)]):
            if d not in dirs and os.path.isdir(d):
                dirs.append(d)
    return dirs


def which(name, override=None):
    """Locate an executable.

    Parameters
    ----------
    name : str
        Command name, looked up on `search_path`, or a path.
    override : str, optional
        Explicit path from the account config, used instead of `name`.

    Returns
    -------
    str or None
        Absolute path of the executable, or None when it is not found or not
        executable.
    """
    if override:
        p = expand(override)
        return p if os.path.isfile(p) and os.access(p, os.X_OK) else None
    if os.sep in name:
        p = expand(name)
        return p if os.access(p, os.X_OK) else None
    return shutil.which(name, path=os.pathsep.join(search_path()))


def tool_env(**overrides):
    """Build the environment for running a vendor CLI.

    Parameters
    ----------
    **overrides : str or None
        Variables to set; a None value removes the variable.

    Returns
    -------
    dict of str to str
        A copy of ``os.environ`` with ``PATH`` extended by `search_path` and
        `overrides` applied.
    """
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(search_path())
    for k, v in overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


def read_json(path, default=None):
    """Load a JSON file.

    Parameters
    ----------
    path : str
    default : object, optional
        Returned when the file is missing, unreadable or not valid JSON.

    Returns
    -------
    object
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, mode=None):
    """Write JSON to a file atomically (temporary file, then rename).

    Parent directories are created as needed.

    Parameters
    ----------
    path : str
    data : object
        JSON-serializable value, written indented.
    mode : int, optional
        File permissions, e.g. ``0o600`` for secrets.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def keychain_read(service):
    """Read a generic password from the macOS login keychain.

    Parameters
    ----------
    service : str
        Service name of the keychain item.

    Returns
    -------
    str or None
        The secret, or None off macOS, when the item does not exist or when
        access fails.
    """
    if not IS_MAC:
        return None
    try:
        p = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None


def copy_to_clipboard(text):
    """Copy text to the clipboard with the first available tool.

    Tries ``pbcopy``, ``wl-copy``, ``xclip`` then ``xsel``.

    Parameters
    ----------
    text : str

    Returns
    -------
    bool
        True when a clipboard tool ran.
    """
    for argv in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]):
        exe = which(argv[0])
        if exe:
            try:
                subprocess.run([exe] + argv[1:], input=text, text=True, timeout=5)
                return True
            except (OSError, subprocess.TimeoutExpired):
                pass
    return False


def open_url(url):
    """Open a URL in the default browser, ignoring failures.

    Parameters
    ----------
    url : str
    """
    try:
        if IS_MAC:
            subprocess.Popen(["/usr/bin/open", url])
        elif which("xdg-open"):
            subprocess.Popen([which("xdg-open"), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            webbrowser.open(url)
    except OSError:
        pass
