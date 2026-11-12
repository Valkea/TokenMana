"""TokenMana core: usage, limits and spend of coding-assistant accounts.

Run through ``core/mana.py``. Stdlib only, Python >= 3.9, macOS + Linux.

Modules
-------
constants
    Version, platform, file locations, limit window lengths.
errors
    `ProviderError` and `ApiError`.
util
    Time, formatting, file, executable and desktop helpers.
net
    HTTP JSON requests and JWT decoding.
metrics
    The normalized metric shape.
config
    ``config.json``, ``secrets.json`` and account lookup.
providers
    One module per account type, and the registry.
schedule
    Auto-ping schedules and their state file.
status
    Fetching, caching and the status document the UIs read.
ping
    Manual and scheduled pings.
login
    Sign-in flows.
cli
    The ``mana`` command line.
"""
