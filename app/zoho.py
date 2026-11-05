"""Sign in with Zoho (OAuth 2.0 authorization-code flow with OpenID Connect).

Only accounts whose verified email is on an allowed domain (ZOHO_ALLOWED_DOMAINS, default
trafficomm.com) can sign in. Roles stay in this app's database: Zoho only proves who the
person is. The ID token is received directly from Zoho's token endpoint over TLS in exchange
for the client secret, so its claims are trusted without a separate signature check
(OpenID Connect Core 3.1.3.7); issuer, audience, nonce and expiry are still checked.
"""
from __future__ import annotations

import base64
import json
import re
import secrets
import time
from urllib.parse import urlencode

import httpx

from .config import settings

SCOPES = "openid email profile"
# Zoho data-centre account servers (zoho.com, zoho.eu, zoho.in, zoho.com.au, zoho.jp, zoho.sa, zohocloud.ca, ...)
_ACCOUNTS_RE = re.compile(r"^https://accounts\.(zoho\.[a-z]{2,3}(\.[a-z]{2})?|zohocloud\.ca)$")


class ZohoError(Exception):
    """A sign-in failure whose message is safe to show the user."""


def redirect_uri(request_base: str) -> str:
    base = settings.public_base_url or request_base.rstrip("/")
    if settings.cookie_secure and base.startswith("http://"):
        base = "https://" + base[len("http://"):]  # behind Render's TLS proxy
    return base + "/auth/zoho/callback"


def authorize_url(session: dict, request_base: str) -> str:
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    session["zoho_state"], session["zoho_nonce"] = state, nonce
    q = {"response_type": "code", "client_id": settings.zoho_client_id, "scope": SCOPES,
         "redirect_uri": redirect_uri(request_base), "state": state, "nonce": nonce, "prompt": "select_account"}
    return settings.zoho_accounts_url + "/oauth/v2/auth?" + urlencode(q)


def _b64json(part: str) -> dict:
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part.encode()))


def accounts_server(param: str | None) -> str:
    """Zoho adds `accounts-server` to the callback for users on another data centre."""
    if not param:
        return settings.zoho_accounts_url
    param = param.rstrip("/")
    if not _ACCOUNTS_RE.match(param):
        raise ZohoError("Zoho returned an unexpected sign-in server.")
    return param


def exchange_code(code: str, server: str, request_base: str, client: httpx.Client | None = None) -> dict:
    data = {"grant_type": "authorization_code", "code": code, "client_id": settings.zoho_client_id,
            "client_secret": settings.zoho_client_secret, "redirect_uri": redirect_uri(request_base)}
    c = client or httpx.Client(timeout=15)
    try:
        r = c.post(server + "/oauth/v2/token", data=data)
        body = r.json() if r.content else {}
    except (httpx.HTTPError, ValueError):
        raise ZohoError("Couldn't reach Zoho to finish signing in. Please try again.")
    finally:
        if client is None:
            c.close()
    if r.status_code != 200 or "error" in body or not body.get("id_token"):
        raise ZohoError("Zoho didn't accept the sign-in. Please try again.")
    return body


def identity(token_body: dict, nonce: str | None, server: str) -> dict:
    """Validate the ID token claims and return {email, name}."""
    try:
        claims = _b64json(token_body["id_token"].split(".")[1])
    except Exception:
        raise ZohoError("Zoho returned an unreadable sign-in token.")
    iss = str(claims.get("iss", "")).rstrip("/")
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if not _ACCOUNTS_RE.match(iss) or settings.zoho_client_id not in auds:
        raise ZohoError("The Zoho sign-in token wasn't issued for this app.")
    if not nonce or claims.get("nonce") != nonce:
        raise ZohoError("The sign-in session expired. Please try again.")
    if float(claims.get("exp", 0)) < time.time() - 60:
        raise ZohoError("The sign-in session expired. Please try again.")
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise ZohoError("Your Zoho account didn't share an email address.")
    if claims.get("email_verified") in (False, "false"):
        raise ZohoError("Your Zoho email address isn't verified.")
    domain = email.rsplit("@", 1)[-1]
    if settings.zoho_allowed_domains and domain not in settings.zoho_allowed_domains:
        raise ZohoError("Only Trafficomm Zoho accounts can sign in.")
    name = str(claims.get("name") or " ".join(x for x in (claims.get("given_name"), claims.get("family_name")) if x) or email.split("@")[0])
    return {"email": email, "name": name[:120]}
