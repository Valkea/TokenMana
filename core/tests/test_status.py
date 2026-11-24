"""Status: fetching into the cache, building accounts and the document the UIs read."""
import datetime as dt
from unittest import mock

import pytest

from tests.helpers import read, utc, write
from tokenmana import status
from tokenmana.constants import STATE_PATH
from tokenmana.errors import ProviderError
from tokenmana.metrics import metric
from tokenmana.providers import PROVIDERS
from tokenmana.util import iso

NOW = utc("2026-01-15T12:00:00Z")
ACC = {"id": "a", "label": "A", "type": "claude"}


@pytest.fixture
def fake_fetch(monkeypatch):
    """Make Claude accounts fetch `outcome` (data, or an exception to raise); returns the mock."""
    def install(outcome):
        fetch = mock.Mock(side_effect=outcome) if isinstance(outcome, Exception) else mock.Mock(return_value=outcome)
        monkeypatch.setitem(PROVIDERS["claude"], "fetch", fetch)
        monkeypatch.setattr(status, "utcnow", lambda: NOW)
        return fetch
    return install


@pytest.mark.parametrize("acc, cfg, seconds", [
    ({"type": "claude"}, {}, 300),
    ({"type": "claude"}, {"refresh_minutes": 2}, 120),
    ({"type": "command"}, {"refresh_minutes": 2}, 900),
    ({"type": "command", "refresh_minutes": 3}, {}, 180),
    ({"type": "claude", "refresh_minutes": 0.1}, {}, 60),
], ids=["default", "config", "provider-over-config", "account-over-provider", "floor"])
def test_refresh_seconds(acc, cfg, seconds):
    assert status.refresh_seconds(acc, cfg) == seconds


# Fetching into the cache

PREVIOUS = {"status": "ok", "data": {"metrics": ["old"]}, "ok_at": "2026-01-15T11:00:00Z"}


def fetch_into_state(fake_fetch, outcome):
    fake_fetch(outcome)
    state = {"a": dict(PREVIOUS)}
    status.fetch_into_state(ACC, state)
    return state["a"]


def test_fetch_success(fake_fetch):
    e = fetch_into_state(fake_fetch, {"metrics": []})
    assert (e["status"], e["error"], e["data"], e["ok_at"]) == ("ok", None, {"metrics": []}, iso(NOW))


def test_partial_data_is_stale_and_keeps_the_last_success_time(fake_fetch):
    e = fetch_into_state(fake_fetch, {"metrics": [], "_partial_error": "Login expired (401)"})
    assert (e["status"], e["error"], e["ok_at"]) == ("stale", "Login expired (401)", PREVIOUS["ok_at"])
    assert "_partial_error" not in e["data"]


def test_fetch_errors_keep_the_previous_data_and_never_raise(fake_fetch):
    e = fetch_into_state(fake_fetch, ProviderError("Not signed in", hint="mana login a", setup=True))
    assert (e["status"], e["hint"], e["data"]) == ("setup", "mana login a", PREVIOUS["data"])

    e = fetch_into_state(fake_fetch, KeyError("rate_limit"))
    assert (e["status"], e["error"], e["data"]) == ("error", "KeyError: 'rate_limit'", PREVIOUS["data"])
    assert e["fetched_at"] == iso(NOW), "fetched_at moves on, so a failing account is not retried at once"


# Building an account

DATA = {"metrics": [metric("session", "Session", 80, resets_at="2026-01-15T11:00:00Z"),
                    metric("weekly", "Weekly", 30, resets_at="2026-01-20T00:00:00Z")], "notes": ["n"]}


def test_account_status_reflects_whether_there_is_data_to_show():
    assert status.build_account(ACC, {}, NOW)["status"] == "pending"
    a = status.build_account(ACC, {"status": "error", "error": "HTTP 500", "data": DATA}, NOW)
    assert (a["status"], a["error"], len(a["metrics"])) == ("stale", "HTTP 500", 2)
    a = status.build_account(ACC, {"status": "setup", "data": DATA}, NOW)
    assert (a["status"], a["metrics"], a["notes"]) == ("setup", [], [])


def test_metrics_whose_window_ended_are_reset():
    m = {x["key"]: x for x in status.build_account(ACC, {"status": "ok", "data": DATA}, NOW)["metrics"]}
    assert (m["session"]["percent"], m["session"]["idle"]) == (0.0, True)
    assert m["weekly"]["percent"] == 30.0


def test_enterprise_copilot_links_to_its_own_host():
    a = status.build_account({"id": "g", "type": "copilot", "host": "mycorp.ghe.com"}, {}, NOW)
    assert (a["host"], a["dashboard_url"]) == ("mycorp.ghe.com", "https://mycorp.ghe.com/settings/copilot")
    assert a["can_ping"] is False


def test_menu_bar_shows_the_highest_usable_metric_or_the_chosen_one():
    accounts = [
        {"id": "a", "label": "A", "status": "ok", "metrics": [{"key": "session", "label": "S", "percent": 40},
                                                              {"key": "weekly", "label": "W", "percent": 70}]},
        {"id": "b", "label": "B", "status": "stale", "metrics": [{"key": "x", "label": "X", "percent": None}]},
        {"id": "c", "label": "C", "status": "error", "metrics": [{"key": "session", "label": "S", "percent": 99}]},
    ]
    best = status.summarize({}, accounts)
    assert (best["percent"], best["account"], best["metric"]) == (70, "a", "weekly")
    assert status.summarize({"menubar": "a:session"}, accounts)["percent"] == 40
    assert status.summarize({"menubar": "b:x"}, accounts) is None


# The whole document

CFG = {"check_updates": False, "groups": ["Work"], "accounts": [
    {"id": "fresh", "label": "b", "type": "claude", "group": "Work"},
    {"id": "old", "label": "A", "type": "claude", "group": "Perso"},
    {"id": "new", "label": "c", "type": "claude"},
    {"id": "elsewhere", "label": "d", "type": "claude", "platforms": ["win32"]},
]}


@pytest.fixture
def collect(fake_fetch):
    write(STATE_PATH, {"fresh": {"status": "ok", "fetched_at": iso(NOW - dt.timedelta(minutes=1)),
                                 "data": {"metrics": [metric("session", "Session", 10)]}},
                       "old": {"status": "ok", "fetched_at": iso(NOW - dt.timedelta(minutes=10)), "data": {}}})

    def run(**kwargs):
        fetch = fake_fetch({"metrics": [metric("session", "Session", 50)]})
        res = status.collect(CFG, **kwargs)
        return res, sorted(c.args[0]["id"] for c in fetch.call_args_list)
    return run


def test_collect_fetches_only_due_accounts_and_caches_them(collect):
    res, fetched = collect()
    assert fetched == ["new", "old"]
    assert read(STATE_PATH)["new"]["status"] == "ok"
    assert res["summary"]["percent"] == 50

    assert collect(force=True)[1] == ["fresh", "new", "old"]
    assert collect(cached=True)[1] == []


def test_document_read_by_the_uis(collect):
    res, _ = collect(cached=True)
    # Configured groups first, then the others, then ungrouped; accounts of other devices are left out.
    assert [a["id"] for a in res["accounts"]] == ["fresh", "old", "new"]
    assert res["groups"] == ["Work", "Perso"]
    assert res["group_colors"] == {"Work": "blue", "Perso": "green"}
    # Fields decoded by macos/Sources/TokenMana/Models.swift (and the GNOME extension).
    assert {"generated_at", "refresh_minutes", "config_path", "summary", "groups", "group_colors",
            "accounts", "app"} <= set(res)
    assert {"name", "version", "latest", "update_available", "update_page"} <= set(res["app"])
    assert {"id", "label", "type", "vendor", "plan", "identity", "status", "error", "hint", "fetched_at", "ok_at",
            "can_ping", "can_login", "cli", "host", "group", "dashboard_url", "metrics", "notes",
            "auto_ping"} <= set(res["accounts"][0])
    assert {"key", "label", "percent", "text", "resets_at", "window_seconds", "idle"} <= set(
        res["accounts"][0]["metrics"][0])
