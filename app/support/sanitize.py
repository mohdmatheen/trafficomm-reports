"""Remove identifiers and secrets before anything leaves the server (Claude prompts, engineering payloads).

Employee messages and uploaded content are untrusted. Numerical reconciliation is done locally; only
aggregates, column names and redacted text are ever sent to an external AI service.
"""
from __future__ import annotations

import hashlib
import re

# order matters: secrets and structured ids first, then generic long numbers
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("secret", re.compile(r"\b(?:sk-ant-[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
                          r"xox[abpr]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16})")),
    ("secret", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}")),  # JWT
    ("secret", re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_ -]?key|bearer)\s*[:=]\s*\S+")),
    ("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("device_id", re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b")),  # IDFA/IDFV/GAID/UUID
    ("appsflyer_id", re.compile(r"\b\d{13}-\d{6,20}\b")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    ("card", re.compile(r"\b\d{4}(?:[ \-]?\d{4}){2}[ \-]?\d{1,7}\b")),
    ("phone", re.compile(r"(?<![\w.])(?:\+\d[\d \-()]{7,16}\d|0\d{8,11})\b")),
    ("hex_id", re.compile(r"\b[0-9a-fA-F]{24,}\b")),
    ("ip", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]
# AppsFlyer columns that identify a person or device: never forwarded
SENSITIVE_COLUMNS = {
    "appsflyer id", "advertising id", "idfa", "idfv", "android id", "imei", "oaid", "amazon fire id", "customer user id",
    "ip", "ip address", "device model", "user agent", "event value", "device download time", "contributor 1 match type",
    "deviceid", "device id", "customer id", "email", "phone", "msisdn", "emirates id", "account number", "card number",
}


def redact(text: str | None, limit: int | None = None) -> tuple[str, dict]:
    """Returns (redacted text, counts per kind)."""
    s = str(text or "")
    counts: dict[str, int] = {}
    for kind, pat in _PATTERNS:
        def _sub(m, kind=kind):
            counts[kind] = counts.get(kind, 0) + 1
            return f"[{kind} removed]"
        s = pat.sub(_sub, s)
    # embedded JSON payloads (AppsFlyer Event Value) carry device ids: drop whole objects
    s, n = re.subn(r"\{[^{}]{0,2000}\"[A-Za-z_]*(?:device_?id|deviceId|user_id|customer|af_id|idfa|idfv|gaid|advertising|email|phone|msisdn)[A-Za-z_]*\"[^{}]{0,2000}\}", "[event value removed]", s)
    if n:
        counts["event_value"] = counts.get("event_value", 0) + n
    if limit and len(s) > limit:
        s = s[:limit] + " …[truncated]"
    return s, counts


def clean(text: str | None, limit: int | None = None) -> str:
    return redact(text, limit)[0]


def safe_columns(headers: list[str]) -> list[str]:
    """Column names are fine to share, but flag which ones hold identifiers."""
    return [h + (" (identifier, values never shared)" if h.strip().lower() in SENSITIVE_COLUMNS else "") for h in headers]


def pseudonym(value: str, salt: str) -> str:
    """Stable, non-reversible token for an identifier so local diagnostics can still group by device."""
    return "dev_" + hashlib.sha256((salt + "|" + str(value)).encode()).hexdigest()[:10]


def scrub(obj, limit: int = 4000):
    """Recursively redact strings in a JSON-like structure and drop sensitive keys."""
    if isinstance(obj, dict):
        return {k: ("[removed]" if str(k).strip().lower() in SENSITIVE_COLUMNS else scrub(v, limit)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub(v, limit) for v in obj]
    if isinstance(obj, str):
        return clean(obj, limit)
    return obj
