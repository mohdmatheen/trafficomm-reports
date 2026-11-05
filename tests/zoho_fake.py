"""Sign in through the real /auth/zoho -> /auth/zoho/callback routes with Zoho's token endpoint
replaced by a fake (SYNTHETIC: no network, no real Zoho account)."""
import base64
import json
import time
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app import zoho as Z
from app.config import settings

CID = "1000.TESTCLIENT"


def jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return enc({"alg": "RS256"}) + "." + enc(claims) + ".sig"


def configure(mp, domains=("trafficomm.com",)):
    mp.setattr(settings, "zoho_client_id", CID)
    mp.setattr(settings, "zoho_client_secret", "test-secret")
    mp.setattr(settings, "zoho_allowed_domains", list(domains))
    mp.setattr(settings, "zoho_admin_emails", ["matheen.m@trafficomm.com"])
    mp.setattr(settings, "public_base_url", "https://reports.example.test")


def sign_in(app, mp, email, *, client=None, aud=CID, verified=True, nonce_ok=True, server_param=None, name="Test Person", next="/tasks"):
    c = client or TestClient(app)
    r = c.get("/auth/zoho?next=" + next, follow_redirects=False)
    assert r.status_code == 303
    loc = urlparse(r.headers["location"])
    q = parse_qs(loc.query)
    assert loc.netloc == "accounts.zoho.in" and loc.path == "/oauth/v2/auth"
    nonce = q["nonce"][0] if nonce_ok else "other"
    claims = {"iss": "https://accounts.zoho.in", "aud": aud, "nonce": nonce, "exp": time.time() + 600,
              "email": email, "email_verified": verified, "name": name}
    seen = {}

    def fake_exchange(code, server, base, client=None):
        seen["server"] = server
        return {"access_token": "x", "id_token": jwt(claims)}

    mp.setattr(Z, "exchange_code", fake_exchange)
    url = f"/auth/zoho/callback?code=abc&state={q['state'][0]}"
    if server_param:
        url += "&accounts-server=" + server_param
    r = c.get(url, follow_redirects=False)
    return c, r, seen
