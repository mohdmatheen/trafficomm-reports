"""LibreOffice-based rendering (PPTX/XLSX -> PDF -> PNG previews), formula recalculation, and
heuristic slide layout checks (overflow, clipping, missing fonts)."""
from __future__ import annotations

import glob
import math
import os
import shutil
import subprocess
import tempfile

from pptx import Presentation
from pptx.util import Emu

from ..config import settings

XCU = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>0</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="ODFRecalcMode" oor:op="fuse"><value>0</value></prop></item>
</oor:items>"""


class RenderError(Exception):
    pass


def _profile_dir() -> str:
    d = tempfile.mkdtemp(prefix="lo_profile_")
    os.makedirs(os.path.join(d, "user"), exist_ok=True)
    with open(os.path.join(d, "user", "registrymodifications.xcu"), "w") as fh:
        fh.write(XCU)
    return d


def soffice_convert(src: str, fmt: str, outdir: str, timeout: int = 180) -> str:
    prof = _profile_dir()
    try:
        cmd = [settings.soffice, f"-env:UserInstallation=file://{prof}", "--headless", "--norestore", "--convert-to", fmt, "--outdir", outdir, src]
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, env={**os.environ, "HOME": prof})
        base = os.path.splitext(os.path.basename(src))[0]
        ext = fmt.split(":")[0]
        out = os.path.join(outdir, f"{base}.{ext}")
        if not os.path.exists(out):
            raise RenderError(f"LibreOffice conversion to {fmt} failed: {p.stderr.decode(errors='ignore')[:300]}")
        return out
    except subprocess.TimeoutExpired:
        raise RenderError("LibreOffice conversion timed out")
    finally:
        shutil.rmtree(prof, ignore_errors=True)


def to_pdf(src: str, outdir: str) -> str:
    return soffice_convert(src, "pdf", outdir)


def pdf_to_pngs(pdf: str, outdir: str, prefix: str = "slide", dpi: int = 60, first: int | None = None, last: int | None = None) -> list[str]:
    cmd = ["pdftoppm", "-png", "-r", str(dpi)]
    if first:
        cmd += ["-f", str(first)]
    if last:
        cmd += ["-l", str(last)]
    cmd += [pdf, os.path.join(outdir, prefix)]
    subprocess.run(cmd, capture_output=True, timeout=180, check=False)
    return sorted(glob.glob(os.path.join(outdir, f"{prefix}-*.png")))


def recalc_xlsx(src: str, outdir: str) -> str:
    """Return path of a LibreOffice-recalculated copy (formulas get fresh cached values)."""
    os.makedirs(outdir, exist_ok=True)
    tmp_in = os.path.join(outdir, "recalc_src_" + os.path.basename(src))
    shutil.copy(src, tmp_in)
    sub = os.path.join(outdir, "recalc")
    os.makedirs(sub, exist_ok=True)
    return soffice_convert(tmp_in, "xlsx:Calc MS Excel 2007 XML", sub)


def available_fonts() -> set[str]:
    try:
        out = subprocess.run(["fc-list", ":", "family"], capture_output=True, timeout=20).stdout.decode()
    except Exception:  # noqa: BLE001
        return set()
    fams = set()
    for line in out.splitlines():
        for f in line.split(","):
            fams.add(f.strip().lower())
    return fams


def layout_checks(pptx_path: str, only_slides: set[int] | None = None, changed_cells: dict | None = None) -> list[dict]:
    """changed_cells: {(slide, shape_name, row, col_index): old_text} - for updated client decks only
    the cells we changed are checked, comparing against the space the original text needed."""
    """Heuristic text-fit checks per text frame and table cell; off-slide shapes; missing fonts."""
    prs = Presentation(pptx_path)
    W, H = prs.slide_width, prs.slide_height
    fonts = available_fonts()
    issues = []
    used_fonts = set()
    for idx, slide in enumerate(prs.slides, 1):
        if only_slides and idx not in only_slides:
            continue
        for sh in slide.shapes:
            if sh.left is not None and sh.width is not None:
                if sh.left < -Emu(10000) or sh.top < -Emu(10000) or sh.left + sh.width > W + 20000 or sh.top + sh.height > H + 20000:
                    pre = changed_cells is not None and not any(k[0] == idx for k in changed_cells)
                    issues.append({"slide": idx, "severity": "info" if pre else "warning", "check": "clipping",
                                   "message": f"'{sh.name}' extends beyond the slide edge" + (" (pre-existing in the client deck, not changed)" if pre else "")})
            frames = []
            if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
                frames.append((sh.text_frame, sh.width, sh.height, sh.name))
            if changed_cells is not None:
                frames = []
            if getattr(sh, "has_table", False) and sh.has_table:
                tbl = sh.table
                for ri, row in enumerate(tbl.rows):
                    for ci, cell in enumerate(row.cells):
                        if changed_cells is not None:
                            old = changed_cells.get((idx, sh.name, ri, ci))
                            if old is None:
                                continue
                            new = cell.text_frame.text
                            if len(new) > max(len(old) + 2, len(old) * 1.25):
                                issues.append({"slide": idx, "severity": "warning", "check": "overflow", "message": f"'{new}' is longer than the original '{old}' in {sh.name} r{ri + 1}c{ci + 1} - check it fits"})
                            continue
                        frames.append((cell.text_frame, tbl.columns[ci].width, row.height, f"{sh.name} r{ri + 1}c{ci + 1}"))
            for tf, w, h, name in frames:
                if not w or not h:
                    continue
                lines = 0.0
                max_pt = 0
                for p in tf.paragraphs:
                    size = None
                    for r in p.runs:
                        if r.font.size:
                            size = r.font.size.pt
                        if r.font.name:
                            used_fonts.add(r.font.name)
                    size = size or 18
                    max_pt = max(max_pt, size)
                    text = "".join(r.text for r in p.runs)
                    width_in = Emu(w).inches - 0.15
                    chars_per_line = max(1, width_in * 72 / (size * 0.47))
                    # long unbreakable tokens can't wrap
                    longest = max((len(t) for t in text.split()), default=0)
                    if longest > chars_per_line * 1.15 and "r" in name and "c" in name:
                        issues.append({"slide": idx, "severity": "warning", "check": "overflow", "message": f"Text '{text[:30]}' may not fit its cell ({name})"})
                    lines += max(1, math.ceil(len(text) / chars_per_line)) * size * 1.2 / 72
                if lines > Emu(h).inches * 1.35 + 0.15 and lines > 0.5 and tf.word_wrap is not False:
                    issues.append({"slide": idx, "severity": "warning", "check": "overflow", "message": f"Text in '{name}' may overflow its box (needs ~{lines:.1f}in, box {Emu(h).inches:.1f}in)"})
    missing = sorted(f for f in used_fonts if f.lower() not in fonts and not f.startswith("+"))
    for f in missing:
        issues.append({"slide": None, "severity": "info", "check": "fonts", "message": f"Font '{f}' is not installed on the render server; previews use a substitute (the client's PowerPoint will use the real font)"})
    return issues
