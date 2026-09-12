"""Turn failures from the network and from login servers into plain sentences.

Both API clients use these, so a dead connection or a wrong client secret
reads the same whichever service noticed it first. Every message says what
went wrong and what to do about it, because the person reading it is usually
setting the app up for the first time and has no traceback to look at.
"""

from __future__ import annotations

import httpx


def describe_transport_error(exc: Exception, service: str) -> str:
    """Why a request never got a usable answer, in words a user can act on.

    ``service`` is the name to blame: "Spotify", "YouTube" or "Google".
    """
    if isinstance(exc, httpx.TimeoutException):
        return (
            f"{service} took too long to answer. It may be slow right now, or "
            "this connection may be dropping packets. Wait a moment and try again."
        )
    if isinstance(exc, httpx.ConnectError):
        return (
            f"Could not connect to {service}. Check that this computer is online "
            "and that no firewall, VPN or proxy is blocking it."
        )
    if isinstance(exc, httpx.RemoteProtocolError):
        return f"{service} closed the connection part-way through the reply. Try again."
    if isinstance(exc, httpx.ProxyError):
        return f"A proxy refused the connection to {service}. Check the network settings on this computer."
    if isinstance(exc, httpx.TransportError):
        return (
            f"Could not reach {service} ({exc.__class__.__name__}). "
            "Check the connection and try again."
        )
    if isinstance(exc, ValueError):
        # response.json() failed: the body was not JSON at all.
        return (
            f"{service} sent back something that is not JSON. It may be down, or a "
            "proxy or a hotel-style login page may have answered instead. Try again "
            "in a minute."
        )
    return f"{service} failed unexpectedly ({exc.__class__.__name__}): {exc}"


def error_fields(response: httpx.Response) -> tuple[str, str]:
    """The ``(code, message)`` pair out of an error body, whatever its shape.

    Three shapes turn up in this app:

    * Spotify's API:    ``{"error": {"status": 401, "message": "...", "reason": "..."}}``
    * OAuth token URLs: ``{"error": "invalid_grant", "error_description": "..."}``
    * Google's API:     ``{"error": {"code": 403, "message": "...", "errors": [{"reason": "quotaExceeded"}]}}``

    Anything unreadable gives empty strings, so callers can fall back to the
    status code without a second try/except.
    """
    try:
        body = response.json()
    except ValueError:
        return "", response.text.strip()[:200]
    if not isinstance(body, dict):
        return "", ""

    error = body.get("error")
    if isinstance(error, str):
        return error, str(body.get("error_description") or "")
    if isinstance(error, dict):
        code = error.get("reason") or ""
        errors = error.get("errors")
        if not code and isinstance(errors, list) and errors and isinstance(errors[0], dict):
            code = errors[0].get("reason") or ""
        return str(code), str(error.get("message") or "")
    return "", str(body.get("detail") or body.get("message") or "")


def describe_oauth_error(
    service: str,
    status: int,
    code: str,
    description: str,
    redirect_uri: str,
) -> str:
    """What a login server's refusal means, and what to change.

    ``code`` and ``description`` come from :func:`error_fields`. The redirect
    URI is included in the message because a mismatch there is the single
    most common failure, and the fix is to compare two strings character by
    character.
    """
    said = f' {service} said: "{description}".' if description else ""
    lowered = f"{code} {description}".lower()

    if code == "redirect_uri_mismatch" or "redirect" in lowered:
        return (
            f"{service} rejected the redirect URI. The one registered on the "
            f"developer dashboard must be exactly {redirect_uri} - look for "
            "localhost instead of 127.0.0.1, a missing /api path, or a trailing "
            f"slash.{said}"
        )
    if code in {"invalid_client", "unauthorized_client"}:
        return (
            f"{service} does not recognise the client ID or client secret in .env. "
            "Copy both again from the developer dashboard, save .env, and restart "
            f"the app.{said}"
        )
    if code == "invalid_grant":
        return (
            f"{service} would not accept the login code or the saved token. It has "
            "expired, was already used, or was revoked. Start the login again from "
            f"the app.{said}"
        )
    if code == "access_denied":
        return (
            f"{service} refused access. You may have pressed Cancel, or this "
            f"account is not allowed to use the app.{said}"
        )
    if code == "invalid_scope":
        return (
            f"{service} rejected the permissions the app asked for. Check that the "
            f"app on the developer dashboard has the right API enabled.{said}"
        )
    where = f" {code}" if code else ""
    return f"{service} refused the login with HTTP {status}{where}.{said or ' No reason was given.'}"


# What the consent screen sends back in ``?error=`` when it will not continue.
_CONSENT_ERRORS = {
    "access_denied": (
        "You pressed Cancel on the {service} permission screen, or this account "
        "is not on the app's allowlist. Press Connect again and choose Allow."
    ),
    "admin_policy_enforced": (
        "Your Google Workspace administrator blocks unverified apps for this "
        "account. Use a personal Google account instead."
    ),
    "org_internal": (
        "The Google app is set to Internal, so only accounts in its organisation "
        "can log in. Set the audience to External on the Google Auth Platform page."
    ),
    "invalid_client": (
        "{service} does not recognise the client ID in .env. Copy it again from "
        "the developer dashboard and restart the app."
    ),
    "redirect_uri_mismatch": (
        "The redirect URI registered on the {service} dashboard does not match "
        "the one in .env. Copy it from the setup guide below, character for character."
    ),
    "invalid_scope": (
        "{service} rejected the permissions the app asked for. Check that the "
        "right API is enabled on the developer dashboard."
    ),
}


def describe_consent_error(service: str, error: str) -> str:
    """Translate the ``error`` a consent screen sends back on the redirect."""
    template = _CONSENT_ERRORS.get(error)
    if template:
        return template.format(service=service)
    return (
        f"{service} reported '{error}' during login. Press Connect again; if it "
        "happens twice, check the app's settings on the developer dashboard."
    )
