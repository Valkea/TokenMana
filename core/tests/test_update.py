"""Update check: version comparison, check throttling and token safety."""
import datetime as dt
import urllib.request
from unittest import mock

import pytest

from tests.helpers import utc
from tokenmana import update

T0 = utc("2026-01-15T00:00:00Z")
DIST = {"name": "TokenMana", "version": "0.2.9", "update_url": "https://example.com/tags.atom",
        "update_page": "https://example.com/#install"}


def hours(h):
    return T0 + dt.timedelta(hours=h)


def test_versions_compare_numerically():
    assert update.version_key("v0.2.10") > update.version_key("0.2.9")
    assert update.version_key("nightly") == ()


def test_latest_version_in_a_tags_feed():
    feed = ('<feed><entry><title>v0.2.9</title></entry><entry><title>v0.2.10</title></entry>'
            '<link href="/releases/tag/v0.2.8"/> dev0.9.0 v1</feed>')
    assert update.latest_in(feed) == "0.2.10"
    assert update.latest_in("<html>Sign in to continue</html>") is None


@pytest.fixture
def app_info(monkeypatch):
    """Run `update.app_info` at a given time; returns its result and whether it hit the network."""
    def run(at, latest="0.3.0", cfg=None, check=True, dist=DIST):
        fetch = mock.Mock(side_effect=latest) if isinstance(latest, Exception) else mock.Mock(return_value=latest)
        monkeypatch.setattr(update, "distribution", lambda: dict(dist))
        monkeypatch.setattr(update, "fetch_latest", fetch)
        monkeypatch.setattr(update, "utcnow", lambda: at)
        return update.app_info(cfg or {}, check=check), fetch.called
    return run


def test_checks_every_12h_retries_failures_after_1h_and_keeps_the_last_known_version(app_info):
    info, fetched = app_info(T0)
    assert fetched
    assert (info["version"], info["latest"], info["update_available"]) == ("0.2.9", "0.3.0", True)
    assert not app_info(hours(11.9))[1]

    info, fetched = app_info(hours(12), latest=RuntimeError("offline"))
    assert fetched
    assert info["latest"] == "0.3.0"
    assert "offline" in info["error"]
    assert not app_info(hours(12.9))[1]

    info, fetched = app_info(hours(13), latest="0.2.9")
    assert fetched
    assert (info["update_available"], info["error"]) == (False, None)


def test_new_update_source_is_checked_at_once(app_info):
    app_info(T0)
    info, fetched = app_info(hours(1), latest="1.0.0", dist=dict(DIST, update_url="https://ghe.example.com/v"))
    assert fetched
    assert info["latest"] == "1.0.0"


def test_no_network_when_turned_off_or_cache_only(app_info):
    info, fetched = app_info(T0, cfg={"check_updates": False})
    assert not fetched
    assert (info["latest"], info["update_available"]) == (None, False)
    assert not app_info(T0, check=False)[1]


def test_token_is_only_sent_to_the_original_host():
    handler = update._SameHostAuth()
    req = urllib.request.Request("https://ghe.example.com/api/v3/x", headers={"Authorization": "token secret"})
    same = handler.redirect_request(req, None, 302, "Found", {}, "https://ghe.example.com/raw/x")
    other = handler.redirect_request(req, None, 302, "Found", {}, "https://objects.example-cdn.com/x")
    assert same.get_header("Authorization") == "token secret"
    assert other.get_header("Authorization") is None
