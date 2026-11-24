"""Timestamps and the normalized metric shape every provider produces."""
import pytest

from tests.helpers import utc
from tokenmana.metrics import decay, metric
from tokenmana.util import first_of_next_month, iso, parse_time

NOW = utc("2026-01-15T12:00:00Z")
EPOCH = int(utc("2026-01-15T10:00:00Z").timestamp())


@pytest.mark.parametrize("value", [
    "2026-01-15T10:00:00Z", "2026-01-15T12:00:00+02:00", "2026-01-15T10:00:00.123456789Z", "2026-01-15T10:00:00.5Z",
    "2026-01-15 10:00:00", EPOCH, EPOCH * 1000, str(EPOCH), EPOCH + 0.25,
], ids=["zulu", "offset", "nanoseconds", "short-fraction", "space-naive", "epoch-s", "epoch-ms", "epoch-str",
        "epoch-float"])
def test_vendor_timestamp_shapes(value):
    assert iso(parse_time(value)) == "2026-01-15T10:00:00Z"


def test_bare_date_is_midnight_utc():
    assert iso(parse_time("2026-01-15")) == "2026-01-15T00:00:00Z"


@pytest.mark.parametrize("value", [None, "", "soon", "2026-13-01"])
def test_unparseable_time_is_none(value):
    assert parse_time(value) is None


def test_first_of_next_month_rolls_over_the_year():
    assert iso(first_of_next_month(utc("2026-12-31T23:00:00Z"))) == "2027-01-01T00:00:00Z"


def test_metric_percent_and_default_text():
    assert metric("k", "L", -2)["percent"] == 0.0
    over = metric("k", "L", 123.44)
    assert (over["percent"], over["text"]) == (123.4, "123%"), "over the limit is not capped"
    assert metric("k", "L", 0, idle=True)["text"] == "idle"
    assert metric("k", "L")["text"] == "—"
    assert metric("k", "L", 5, resets_at=EPOCH)["resets_at"] == "2026-01-15T10:00:00Z"


def test_ended_window_drops_to_zero_without_touching_the_cache():
    weekly = metric("weekly", "Weekly", 80, resets_at="2026-01-15T11:59:59Z")
    d = decay(weekly, NOW)
    assert (d["percent"], d["text"], d["resets_at"], d["idle"]) == (0.0, "0% (reset)", None, False)
    assert weekly["percent"] == 80.0

    session = decay(metric("session", "Session", 80, resets_at="2026-01-15T11:00:00Z"), NOW)
    assert (session["percent"], session["idle"], session["text"]) == (0.0, True, "idle")

    no_percent = decay(metric("p", "Premium", None, text="unlimited", resets_at="2026-01-01"), NOW)
    assert (no_percent["percent"], no_percent["text"]) == (None, "unlimited")


def test_running_window_is_unchanged():
    m = metric("session", "Session", 80, resets_at="2026-01-15T12:00:01Z")
    assert decay(m, NOW) is m
