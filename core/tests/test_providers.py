"""Providers: turning each vendor's response into metrics, and their errors into statuses."""
import base64
import hashlib
import json
import os
import shlex
import sys
from unittest import mock

import pytest

from tests.helpers import utc, write
from tokenmana.constants import HOME
from tokenmana.errors import ApiError, ProviderError
from tokenmana.providers import claude, codex, copilot
from tokenmana.providers.command import fetch_command

NOW = utc("2026-01-15T12:00:00Z")


def by_key(result):
    return {m["key"]: m for m in result["metrics"]}


def expires(s):
    """Claude Code stores ``expiresAt`` in epoch milliseconds."""
    return int(utc(s).timestamp() * 1000)


def http(monkeypatch, module, response, error):
    """Replace the module's ``http_json`` and return the mock."""
    m = mock.Mock(return_value=response, side_effect=error)
    monkeypatch.setattr(module, "http_json", m)
    return m


# Claude

VALID = {"accessToken": "tok", "expiresAt": expires("2026-01-15T20:00:00Z"), "subscriptionType": "max"}
EXPIRED = dict(VALID, accessToken="old", expiresAt=expires("2026-01-15T11:00:00Z"))


@pytest.fixture
def fetch_claude(monkeypatch):
    def fetch(usage=None, creds=(VALID,), renewed=False, error=None):
        monkeypatch.setattr(claude, "utcnow", lambda: NOW)
        monkeypatch.setattr(claude, "claude_credentials", mock.Mock(side_effect=list(creds)))
        monkeypatch.setattr(claude, "claude_renew_login", lambda acc: renewed)
        monkeypatch.setattr(claude, "claude_identity", lambda acc: "me@example.com")
        api = http(monkeypatch, claude, usage, error)
        return claude.fetch_claude({"id": "claude", "type": "claude"}), api
    return fetch


def test_claude_limits_list(fetch_claude):
    res, _ = fetch_claude({"limits": [
        {"kind": "session", "group": "session", "percent": 0, "resets_at": None},
        {"kind": "weekly_all", "group": "weekly", "percent": 41.26, "resets_at": "2026-01-20T10:00:00.123456Z"},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 12, "resets_at": "2026-01-20T10:00:00Z",
         "scope": {"model": {"display_name": "Opus 4", "id": "opus"}}},
        {"kind": "daily_tokens", "group": "daily", "percent": 5},
    ]})
    m = by_key(res)
    # Keys are the contract: the UIs find "session" / "weekly_all" by key, and keys must stay unique.
    assert list(m) == ["session", "weekly_all", "weekly_scoped_opus_4", "daily_tokens"]
    assert m["session"]["idle"], "no usage and no reset time: the 5 h window has not started"
    assert (m["weekly_all"]["percent"], m["weekly_all"]["resets_at"]) == (41.3, "2026-01-20T10:00:00Z")
    assert m["weekly_scoped_opus_4"]["window_seconds"] == 7 * 86400
    assert (res["plan"], res["identity"]) == ("Max", "me@example.com")


def test_claude_legacy_fields(fetch_claude):
    res, _ = fetch_claude({"five_hour": {"utilization": 30, "resets_at": "2026-01-15T14:00:00Z"},
                           "seven_day": {"utilization": 50, "resets_at": "2026-01-20T10:00:00Z"},
                           "seven_day_opus": None})
    m = by_key(res)
    assert list(m) == ["session", "weekly_all"]
    assert (m["session"]["percent"], m["session"]["idle"]) == (30.0, False)


def test_claude_extra_usage_spend(fetch_claude):
    res, _ = fetch_claude({"limits": [], "spend": {
        "enabled": True, "percent": 24.68,
        "used": {"amount_minor": 1234, "exponent": 2, "currency": "EUR"}, "limit": {"amount_minor": 5000, "exponent": 2}}})
    m = by_key(res)["extra_usage"]
    assert (m["text"], m["percent"]) == ("€12.34 / €50.00", 24.7)


def test_claude_expired_token_is_renewed_by_claude_code_first(fetch_claude):
    _, api = fetch_claude({"limits": []}, creds=(EXPIRED, VALID), renewed=True)
    assert api.call_args.kwargs["headers"]["Authorization"] == "Bearer tok"

    with pytest.raises(ProviderError, match="expired") as exc:
        fetch_claude({}, creds=(EXPIRED, EXPIRED), renewed=False)
    assert not exc.value.setup


def test_claude_errors(fetch_claude):
    with pytest.raises(ProviderError) as exc:
        fetch_claude(creds=({"subscriptionType": "max"},))  # API-key login: no OAuth token
    assert exc.value.setup

    with pytest.raises(ProviderError, match=r"^Token rejected \(401\)$") as exc:
        fetch_claude(error=ApiError(401, "invalid token"))
    assert exc.value.hint

    with pytest.raises(ProviderError) as exc:
        fetch_claude(error=ApiError(529, "overloaded"))
    assert not exc.value.setup


def test_claude_credentials_from_the_account_config_dir(monkeypatch):
    acc = {"id": "w", "type": "claude", "config_dir": "~/.claude-w"}
    path = os.path.join(HOME, ".claude-w", ".credentials.json")
    monkeypatch.setattr(claude, "IS_MAC", False)
    with pytest.raises(ProviderError) as exc:
        claude.claude_credentials(acc)
    assert exc.value.setup

    write(path, {"claudeAiOauth": {"accessToken": "tok"}})
    assert claude.claude_credentials(acc) == {"accessToken": "tok"}

    write(path, "{not json")
    with pytest.raises(ProviderError) as exc:
        claude.claude_credentials(acc)
    assert not exc.value.setup


def test_claude_macos_keychain_item_names_follow_claude_code(monkeypatch):
    monkeypatch.setattr(claude, "IS_MAC", True)

    def tried(acc):
        kr = mock.Mock(return_value=None)
        monkeypatch.setattr(claude, "keychain_read", kr)
        with pytest.raises(ProviderError):
            claude.claude_credentials(acc)
        return [c.args[0] for c in kr.call_args_list]

    def item(config_dir):  # Claude Code suffixes the name with a hash of CLAUDE_CONFIG_DIR
        return "Claude Code-credentials-" + hashlib.sha256(config_dir.encode()).hexdigest()[:8]

    assert tried({"id": "c", "type": "claude"}) == ["Claude Code-credentials"]
    assert tried({"id": "c", "type": "claude", "keychain_service": "custom"}) == ["custom"]
    assert tried({"id": "w", "type": "claude", "config_dir": "~/.claude-w"}) == [
        item("~/.claude-w"), item(os.path.join(HOME, ".claude-w"))]


# Codex

CODEX_HOME = os.path.join(HOME, ".codex")


def jwt(claims):
    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{part}.sig"


@pytest.fixture
def fetch_codex(monkeypatch):
    write(os.path.join(CODEX_HOME, "auth.json"), {"tokens": {"access_token": "tok", "id_token": jwt({
        "email": "me@example.com",
        "https://api.openai.com/auth": {"chatgpt_plan_type": "plus", "chatgpt_account_id": "acct-1"}})}})

    def fetch(usage=None, error=None):
        monkeypatch.setattr(codex, "utcnow", lambda: NOW)
        api = http(monkeypatch, codex, usage, error)
        return codex.fetch_codex({"id": "codex", "type": "codex"}), api
    return fetch


def test_codex_usage_windows_credits_and_login_claims(fetch_codex):
    res, api = fetch_codex({"plan_type": "pro", "rate_limit": {
        "limit_reached": True,
        "primary_window": {"used_percent": 42, "limit_window_seconds": 18000, "reset_after_seconds": 3600},
        "secondary_window": {"used_percent": 7, "limit_window_seconds": 604800,
                             "reset_at": int(utc("2026-01-19T00:00:00Z").timestamp())}},
        "credits": {"has_credits": True, "balance": "1500"}})
    m = by_key(res)
    assert (m["session"]["percent"], m["session"]["resets_at"]) == (42.0, "2026-01-15T13:00:00Z")
    assert m["weekly"]["resets_at"] == "2026-01-19T00:00:00Z"
    assert res["notes"] == ["Rate limit reached", "Credits balance: 1,500"]
    assert (res["plan"], res["identity"]) == ("Pro", "me@example.com")
    assert api.call_args.kwargs["headers"]["ChatGPT-Account-Id"] == "acct-1"


def test_codex_business_spend_control_budget(fetch_codex):
    res, _ = fetch_codex({"spend_control": {"individual_limit": {
        "used_percent": 25, "used": 250, "limit": 1000, "reset_at": "2026-02-01T00:00:00Z"}}})
    assert by_key(res)["budget"]["text"] == "250 / 1,000"


def test_codex_falls_back_to_the_latest_session_log_when_the_api_fails(fetch_codex):
    def snapshot(ts, pct):
        return json.dumps({"timestamp": ts, "payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": pct, "window_minutes": 300, "resets_in_seconds": 600}}}})
    write(os.path.join(CODEX_HOME, "sessions", "2026", "01", "15", "rollout-1.jsonl"), "\n".join([
        snapshot("2026-01-15T10:00:00Z", 10), snapshot("2026-01-15T11:00:00Z", 30), '{"type": "message"}']))

    res, _ = fetch_codex(error=ApiError(401, "expired"))
    assert res["_partial_error"] == "Login expired (401)"
    session = res["metrics"][0]
    assert (session["key"], session["percent"], session["resets_at"]) == ("session", 30.0, "2026-01-15T11:10:00Z")


def test_codex_errors(fetch_codex):
    with pytest.raises(ProviderError) as exc:
        fetch_codex(error=ApiError(401, "expired"))  # no session log to fall back on
    assert "mana login codex" in exc.value.hint

    write(os.path.join(CODEX_HOME, "auth.json"), {"OPENAI_API_KEY": "sk-x", "tokens": None})
    with pytest.raises(ProviderError) as exc:
        fetch_codex({})
    assert exc.value.setup

    os.remove(os.path.join(CODEX_HOME, "auth.json"))
    with pytest.raises(ProviderError) as exc:
        fetch_codex({})
    assert exc.value.setup


# Copilot

@pytest.fixture
def fetch_copilot(monkeypatch):
    def fetch(user=None, error=None, **acc):
        monkeypatch.setattr(copilot, "copilot_token", lambda acc: "gho_x")
        api = http(monkeypatch, copilot, user, error)
        return copilot.fetch_copilot({"id": "gh", "type": "copilot", **acc}), api
    return fetch


def test_copilot_premium_requests(fetch_copilot):
    res, _ = fetch_copilot({"copilot_plan": "individual_pro", "login": "octo",
                            "quota_reset_date_utc": "2026-02-01T00:00:00Z", "quota_snapshots": {
                                "premium_interactions": {"entitlement": 300, "remaining": 120, "percent_remaining": 40.0},
                                "chat": {"unlimited": True},
                                "completions": {"entitlement": 2000, "remaining": 2000, "percent_remaining": 100.0}}})
    assert list(by_key(res)) == ["premium_interactions"], "unused or unlimited extras are not shown"
    m = by_key(res)["premium_interactions"]
    assert (m["percent"], m["text"], m["resets_at"]) == (60.0, "180 / 300", "2026-02-01T00:00:00Z")
    assert (res["plan"], res["identity"]) == ("Individual Pro", "octo")


def test_copilot_used_extras_overage_and_unlimited(fetch_copilot):
    res, _ = fetch_copilot({"quota_snapshots": {
        "premium_interactions": {"entitlement": 50, "remaining": -20, "overage_count": 20, "overage_permitted": True},
        "chat": {"entitlement": 50, "remaining": 30}}})
    m = by_key(res)
    assert m["premium_interactions"]["percent"] == 140.0, "over quota is not capped at 100"
    assert m["chat"]["text"] == "20 / 50"
    assert res["notes"] == ["Premium requests: 20 over quota (billed)"]

    res, _ = fetch_copilot({"quota_snapshots": {"premium_interactions": {"unlimited": True}}})
    assert (res["metrics"][0]["percent"], res["metrics"][0]["text"]) == (None, "unlimited")


def test_copilot_legacy_free_plan(fetch_copilot):
    res, _ = fetch_copilot({"access_type_sku": "free_limited_copilot", "limited_user_reset_date": "2026-02-01",
                            "monthly_quotas": {"chat": 50, "completions": 2000},
                            "limited_user_quotas": {"chat": 20, "completions": 2000}})
    m = by_key(res)
    assert (m["chat"]["percent"], m["chat"]["text"]) == (60.0, "30 / 50")
    assert m["completions"]["percent"] == 0.0


def test_copilot_enterprise_host_and_refused_token(fetch_copilot):
    _, api = fetch_copilot({}, host="https://MyCorp.ghe.com/")
    assert api.call_args.args[0] == "https://api.mycorp.ghe.com/copilot_internal/user"

    with pytest.raises(ProviderError) as exc:
        fetch_copilot(error=ApiError(401, "Bad credentials"))
    assert exc.value.hint == "mana login gh"


# Command (your own provider)

def command_account(body, **extra):
    """A command account running a Python script with `body`."""
    script = os.path.join(HOME, "provider.py")
    write(script, "import json, sys\n" + body + "\n")
    return {"id": "cmd", "type": "command", "command": f"{shlex.quote(sys.executable)} {shlex.quote(script)}", **extra}


def test_command_json_object_printed_after_log_lines():
    payload = {"plan": "Team", "vendor": "bedrock", "notes": ["Spent today: $3", 7], "metrics": [
        {"key": "budget", "label": "Budget", "percent": 42.04, "text": "$42 / $100", "resets_at": "2026-12-01T00:00:00Z"},
        {"percent": -3}, "junk"]}
    res = fetch_command(command_account(f"print('loading...'); print(json.dumps({payload!r}, indent=2))"))
    assert res["metrics"][0] == {"key": "budget", "label": "Budget", "percent": 42.0, "text": "$42 / $100",
                                 "resets_at": "2026-12-01T00:00:00Z", "window_seconds": None, "idle": False}
    assert (res["metrics"][1]["key"], res["metrics"][1]["percent"]) == ("metric1", 0.0)
    assert len(res["metrics"]) == 2
    assert (res["vendor"], res["notes"]) == ("bedrock", ["Spent today: $3", "7"])


def test_command_reported_error():
    body = "print(json.dumps({'error': 'Credentials expired', 'hint': 'run sso login', 'setup': True})); sys.exit(1)"
    with pytest.raises(ProviderError, match="^Credentials expired$") as exc:
        fetch_command(command_account(body))
    assert (exc.value.hint, exc.value.setup) == ("run sso login", True)

    with pytest.raises(ProviderError) as exc:
        fetch_command(command_account(body, login_hint="aws sso login"))
    assert exc.value.hint == "aws sso login"


def test_command_failures():
    with pytest.raises(ProviderError, match="failed: no credentials"):
        fetch_command(command_account("print('no credentials', file=sys.stderr); sys.exit(2)"))
    with pytest.raises(ProviderError, match="did not print a JSON object"):
        fetch_command(command_account("print('hello')"))


@pytest.mark.parametrize("acc", [{"id": "cmd", "type": "command"},
                                 {"id": "cmd", "type": "command", "command": "no-such-tool-xyz"}],
                         ids=["no-command", "not-installed"])
def test_command_not_set_up_on_this_device(acc):
    with pytest.raises(ProviderError) as exc:
        fetch_command(acc)
    assert exc.value.setup
