"""HTTP JSON requests and JWT decoding."""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

from .constants import VERSION
from .errors import ApiError, ProviderError


def http_json(url, headers=None, data=None, timeout=20, form=False):
    """Send an HTTP request and decode the JSON response.

    The request is a GET, or a POST when `data` is given.

    Parameters
    ----------
    url : str
    headers : dict, optional
        Extra headers, merged over the defaults (JSON ``Accept`` and the
        TokenMana ``User-Agent``).
    data : dict, optional
        Request body, sent as JSON or as a form.
    timeout : float, default 20
        Seconds.
    form : bool, default False
        Send `data` as ``application/x-www-form-urlencoded``.

    Returns
    -------
    object
        The decoded JSON body (None for an empty body).

    Raises
    ------
    ApiError
        On an HTTP error status.
    ProviderError
        On a network error or timeout.
    """
    body = None
    hdrs = {"Accept": "application/json", "User-Agent": f"tokenmana/{VERSION}"}
    hdrs.update(headers or {})
    if data is not None:
        if form:
            from urllib.parse import urlencode
            body = urlencode(data).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            body = json.dumps(data).encode()
            hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace").strip()
        raise ApiError(e.code, detail) from None
    except urllib.error.URLError as e:
        raise ProviderError(f"Network error: {e.reason}") from None
    except (TimeoutError, OSError) as e:
        raise ProviderError(f"Network error: {e}") from None
    return json.loads(raw.decode("utf-8") or "null")


def jwt_claims(token):
    """Decode the payload of a JWT without verifying its signature.

    Parameters
    ----------
    token : str

    Returns
    -------
    dict
        The claims, or an empty dict when `token` is not a decodable JWT.
    """
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}
