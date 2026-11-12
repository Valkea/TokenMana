"""Account types and what each one supports.

Each provider module has a ``fetch_<type>(acc)`` function that returns the
account's ``plan``, ``identity``, ``metrics`` and ``notes``, or raises
`ProviderError`.
"""
from __future__ import annotations

from ..errors import ProviderError
from .claude import fetch_claude
from .codex import fetch_codex
from .command import fetch_command
from .copilot import fetch_copilot


PROVIDERS = {
    "claude": {"fetch": fetch_claude, "vendor": "anthropic", "ping": True, "login": True,
               "dashboard": "https://claude.ai/settings/usage"},
    "codex": {"fetch": fetch_codex, "vendor": "openai", "ping": True, "login": True,
              "dashboard": "https://chatgpt.com/codex/settings/usage"},
    "copilot": {"fetch": fetch_copilot, "vendor": "github", "ping": False, "login": True,
                "dashboard": "https://github.com/settings/copilot"},
    "command": {"fetch": fetch_command, "vendor": "command", "ping": False, "login": False,
                "dashboard": None, "refresh_minutes": 15},
}


def fetch_unknown(acc):
    """Fetch function for account types this version does not know.

    Parameters
    ----------
    acc : dict
        Account config.

    Raises
    ------
    ProviderError
        Always, with ``setup=True``.
    """
    raise ProviderError(f"Unknown account type {acc.get('type')!r}",
                        hint="Update TokenMana, or remove this account", setup=True)


UNKNOWN_PROVIDER = {"fetch": fetch_unknown, "vendor": "unknown", "ping": False, "login": False, "dashboard": None}


def provider(acc):
    """Return the provider entry for an account's type.

    A config written by a newer or extended version may name other types.

    Parameters
    ----------
    acc : dict
        Account config.

    Returns
    -------
    dict
        Entry of `PROVIDERS`, or `UNKNOWN_PROVIDER`.
    """
    return PROVIDERS.get(acc.get("type"), UNKNOWN_PROVIDER)
