"""Exceptions raised while fetching account data."""
from __future__ import annotations


class ProviderError(Exception):
    """Error a provider reports for one account, shown to the user.

    Parameters
    ----------
    message : str
        Human-readable description of the problem.
    hint : str, optional
        Suggested next step, e.g. ``"mana login claude"``.
    setup : bool, default False
        True when the account is not set up on this device (not signed in,
        CLI missing...) rather than temporarily failing.

    Attributes
    ----------
    hint : str or None
    setup : bool
    """

    def __init__(self, message, hint=None, setup=False):
        super().__init__(message)
        self.hint = hint
        self.setup = setup


class ApiError(Exception):
    """Non-2xx HTTP response from a vendor API.

    Parameters
    ----------
    status : int
        HTTP status code.
    detail : str
        Start of the response body, decoded.

    Attributes
    ----------
    status : int
    detail : str
    """

    def __init__(self, status, detail):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail
