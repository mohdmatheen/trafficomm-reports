"""Safe low-level readers for CSV/XLSX plus number/date parsing.

Nothing here executes workbook content: openpyxl is used in read-only/data-only mode, macros are
never loaded, and extraction limits guard against oversized or zip-bomb inputs.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
import zipfile
from dataclasses import dataclass, field

from ..config import settings

MISSING_TOKENS = {"", "-", "--", "—", "–", "n/a", "na", "#n/a", "null", "none", "nan", "(not set)"}


class FileRejected(Exception):
    """Raised for malformed, unsupported or unsafe files. Message is user-facing."""


@dataclass
class RawSheet:
    name: str
    rows: list[list]
    truncated: bool = False
    has_formulas: bool = False
    formula_cells: int = 0


# ---------------------------------------------------------------------------
# Zip / OOXML safety
# ---------------------------------------------------------------------------

def check_ooxml(path: str, expected: str) -> dict:
    """Validate an OOXML container. Returns info dict; raises FileRejected."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise FileRejected("File is not a valid Office document (not a ZIP container).")
    names = zf.namelist()
    total = 0
    for zi in zf.infolist():
        total += zi.file_size
        if zi.compress_size and zi.file_size / max(zi.compress_size, 1) > 200 and zi.file_size > 20_000_000:
            raise FileRejected("File rejected: suspicious compression ratio (possible zip bomb).")
    if total > settings.max_uncompressed_bytes:
        raise FileRejected(f"File rejected: uncompressed size {total/1e6:.0f} MB exceeds the extraction limit.")
    if "[Content_Types].xml" not in names:
        raise FileRejected("File is not a valid Office document (missing content types).")
    marker = {"xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}[expected]
    if marker not in names:
        raise FileRejected(f"File does not look like a valid .{expected} file.")
    info = {"has_macros": any(n.lower().endswith("vbaproject.bin") for n in names),
            "entries": len(names), "uncompressed_bytes": total}
    if expected == "xlsx":
        info["charts"] = len([n for n in names if n.startswith("xl/charts/chart")])
        info["drawings"] = len([n for n in names if n.startswith("xl/drawings/drawing")])
        info["images"] = len([n for n in names if n.startswith("xl/media/")])
        info["pivots"] = len([n for n in names if n.startswith("xl/pivotTables/")])
        info["external_links"] = len([n for n in names if n.startswith("xl/externalLinks/")])
        info["tables"] = len([n for n in names if n.startswith("xl/tables/")])
    return info


def check_image(path: str) -> dict:
    """Validate an uploaded screenshot (PNG/JPG) without trusting its extension."""
    from PIL import Image
    try:
        with Image.open(path) as im:
            fmt = im.format
            w, h = im.size
            im.verify()
    except Exception:  # noqa: BLE001
        raise FileRejected("Not a readable PNG/JPG image.")
    if fmt not in ("PNG", "JPEG"):
        raise FileRejected(f"Images must be PNG or JPG (this file is {fmt}).")
    if w * h > 60_000_000:
        raise FileRejected("Image is too large (over 60 megapixels).")
    return {"format": fmt, "width": w, "height": h}


def check_zip_of_images(path: str) -> list[str]:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise FileRejected("Not a valid ZIP archive.")
    total = sum(z.file_size for z in zf.infolist())
    if total > settings.max_uncompressed_bytes:
        raise FileRejected("ZIP rejected: uncompressed size exceeds the extraction limit.")
    imgs = []
    for z in zf.infolist():
        n = z.filename
        if z.is_dir() or "__MACOSX" in n or n.split("/")[-1].startswith("."):
            continue
        if ".." in n or n.startswith("/"):
            raise FileRejected("ZIP rejected: unsafe path inside archive.")
        if n.lower().endswith((".png", ".jpg", ".jpeg")):
            imgs.append(n)
    if not imgs:
        raise FileRejected("ZIP contains no PNG/JPG images.")
    return imgs


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def _decode(data: bytes) -> str:
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16")
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise FileRejected("Could not decode CSV text.")


def read_csv(path: str) -> list[RawSheet]:
    with open(path, "rb") as fh:
        data = fh.read()
    if b"\x00" in data[:4096] and not (data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff")):
        raise FileRejected("File looks binary, not CSV.")
    text = _decode(data)
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        delim = ","
    rows = []
    truncated = False
    for i, r in enumerate(csv.reader(io.StringIO(text), delimiter=delim)):
        if i >= settings.max_rows:
            truncated = True
            break
        rows.append(r[: settings.max_cols])
    if not rows:
        raise FileRejected("CSV file is empty.")
    return [RawSheet(name="CSV", rows=rows, truncated=truncated)]


def read_xlsx(path: str, include_formulas: bool = False) -> list[RawSheet]:
    import openpyxl  # defusedxml is installed, openpyxl uses it automatically
    check_ooxml(path, "xlsx")
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True, keep_vba=False)
    except Exception as e:  # noqa: BLE001
        raise FileRejected(f"Workbook could not be opened: {type(e).__name__}")
    sheets = []
    for ws in wb.worksheets:
        rows = []
        truncated = False
        # Some exporters (e.g. TikTok Ads Manager) write a wrong <dimension ref="A1"/>; read-only mode trusts it
        # and would return only cell A1, silently dropping the whole sheet. Re-scan the real extent instead.
        if hasattr(ws, "reset_dimensions"):
            ws.reset_dimensions()
        width = 0
        for i, r in enumerate(ws.iter_rows(values_only=True)):
            if i >= settings.max_rows:
                truncated = True
                break
            rows.append(list(r[: settings.max_cols]))
            width = max(width, len(rows[-1]))
        for r in rows:  # without a dimension, rows can come back ragged
            if len(r) < width:
                r.extend([None] * (width - len(r)))
        # trim trailing empty rows
        while rows and all(v is None or str(v).strip() == "" for v in rows[-1]):
            rows.pop()
        sheets.append(RawSheet(name=ws.title, rows=rows, truncated=truncated))
    wb.close()
    if include_formulas:
        wbf = openpyxl.load_workbook(path, read_only=True, data_only=False)
        for s, ws in zip(sheets, wbf.worksheets):
            if hasattr(ws, "reset_dimensions"):
                ws.reset_dimensions()
            n = 0
            for r in ws.iter_rows(values_only=True):
                n += sum(1 for v in r if isinstance(v, str) and v.startswith("="))
            s.formula_cells = n
            s.has_formulas = n > 0
        wbf.close()
    return sheets


# ---------------------------------------------------------------------------
# Number parsing
# ---------------------------------------------------------------------------
_NUM_CLEAN = re.compile(r"[^\d,.\-eE]")


def is_missing(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and v != v:
        return True
    return isinstance(v, str) and v.replace("\xa0", " ").strip().lower() in MISSING_TOKENS


def classify_numeric_strings(values: list) -> str:
    """plain | us | eu | ambiguous | none"""
    strs = [str(v).strip() for v in values if isinstance(v, str) and not is_missing(v)]
    if not strs:
        return "plain"
    us = eu = amb = 0
    for s in strs:
        s2 = _NUM_CLEAN.sub("", s)
        if "," in s2 and "." in s2:
            if s2.rfind(".") > s2.rfind(","):
                us += 1
            else:
                eu += 1
        elif "," in s2:
            parts = s2.split(",")
            if all(len(p) == 3 for p in parts[1:]):
                amb += 1  # 1,234 could be 1234 (US) or 1.234 (EU decimal)
            else:
                eu += 1  # 12,5 -> decimal comma
        elif "." in s2:
            parts = s2.split(".")
            if len(parts) > 2:
                eu += 1  # 1.234.567
            elif len(parts[1]) == 3 and len(parts[0]) <= 3 and parts[0] not in ("0", "-0"):
                amb += 0  # 1.234 is usually decimal in exports; treat as plain
    if us and not eu:
        return "us"
    if eu and not us:
        return "eu"
    if us and eu:
        return "ambiguous"
    if amb:
        return "us_or_eu_thousands"  # only commas with 3 digits -> needs context
    return "plain"


def parse_number(v, fmt: str = "plain"):
    """Return float or None (missing). Raises ValueError on garbage."""
    if is_missing(v):
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        raise ValueError("date in numeric column")
    s = str(v).replace("\xa0", " ").strip()
    neg = s.startswith("(") and s.endswith(")")
    pct = s.endswith("%")
    s = _NUM_CLEAN.sub("", s)
    if fmt == "eu":
        s = s.replace(".", "").replace(",", ".")
    elif fmt in ("us", "us_or_eu_thousands", "plain"):
        s = s.replace(",", "")
    if s in ("", "-", "."):
        return None
    x = float(s)
    if neg:
        x = -x
    return x


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------
DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y", "%d.%m.%Y",
                "%b %d, %Y", "%d %b %Y", "%d %B %Y", "%B %d, %Y", "%Y%m%d"]


def _strip_time(s: str) -> str:
    s = s.strip()
    s = re.sub(r"[T ]\d{1,2}:\d{2}(:\d{2})?(\.\d+)?([+-]\d{2}:?\d{2}|Z)?$", "", s)
    return s.strip()


def detect_date_format(values: list) -> dict:
    """Detect a consistent date format for a column. Returns dict(format, ambiguous, candidates, native)."""
    vals = [v for v in values if not is_missing(v)]
    if not vals:
        return {"format": None, "ambiguous": False, "candidates": [], "native": False}
    if all(isinstance(v, (dt.date, dt.datetime)) for v in vals):
        return {"format": "native", "ambiguous": False, "candidates": ["native"], "native": True}
    strs = [_strip_time(str(v)) for v in vals]
    ok = []
    for f in DATE_FORMATS:
        try:
            for s in strs:
                dt.datetime.strptime(s, f)
            ok.append(f)
        except ValueError:
            continue
    amb = ("%d/%m/%Y" in ok and "%m/%d/%Y" in ok) or ("%d-%m-%Y" in ok and "%m-%d-%Y" in ok)
    return {"format": ok[0] if ok else None, "ambiguous": amb, "candidates": ok, "native": False}


def parse_date(v, fmt: str | None):
    if is_missing(v):
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if not fmt or fmt == "native":
        raise ValueError(f"unparseable date {v!r}")
    return dt.datetime.strptime(_strip_time(str(v)), fmt).date()
