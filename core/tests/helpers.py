"""Helpers shared by the core tests."""
import datetime as dt
import json
import os

from tokenmana.constants import HOME


def utc(s):
    """Return an aware UTC datetime from ``"YYYY-MM-DDTHH:MM:SSZ"``."""
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def local(y, mo, d, h=0, mi=0):
    """Return an aware datetime on the (sandboxed, Europe/Paris) local clock."""
    return dt.datetime(y, mo, d, h, mi).astimezone()


def write(path, data):
    """Write `data` as JSON (or text, if a string) to `path`, creating its directory."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))


def read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fake_cli(name, script="exit 0"):
    """Create an executable shell script in the sandboxed home and return its path."""
    path = os.path.join(HOME, "bin", name)
    write(path, "#!/bin/sh\n" + script + "\n")
    os.chmod(path, 0o755)
    return path
