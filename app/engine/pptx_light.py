"""Open a deck without loading its pictures into memory.

python-pptx reads every part of a deck into memory on open, pictures and videos included. Client
decks with months of screenshots run to hundreds of MB, and the pipeline opens a deck several times
just to read text and tables (inspection, checks), which is how large tasks ran out of memory.

`open_light(path)` opens a copy of the deck whose media files are replaced by empty placeholders.
Text, tables, charts, shape positions and picture shapes are all still there, so it is safe for
anything that reads the deck. Never save a light deck over a real one: its pictures are empty.
"""
from __future__ import annotations

import gc
import os
import shutil
import tempfile
import zipfile

from pptx import Presentation

_MEDIA_PREFIX = "ppt/media/"
_KEEP_BYTES = 4096        # tiny media (icons, bullets) are kept as they are
_cache: dict[tuple, str] = {}
_CACHE_MAX = 6


def light_copy(path: str) -> str:
    """Path of a copy of `path` with its media emptied (cached per file version)."""
    st = os.stat(path)
    key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    hit = _cache.get(key)
    if hit and os.path.exists(hit):
        return hit
    fd, tmp = tempfile.mkstemp(suffix=".pptx", prefix="deck_light_")
    os.close(fd)
    try:
        with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.filename.startswith(_MEDIA_PREFIX) and info.file_size > _KEEP_BYTES:
                    zout.writestr(info.filename, b"")
                    continue
                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.compress_type = zipfile.ZIP_DEFLATED
                with zin.open(info) as src, zout.open(zi, "w", force_zip64=True) as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
    except Exception:
        os.unlink(tmp)
        raise
    while len(_cache) >= _CACHE_MAX:
        old = _cache.pop(next(iter(_cache)))
        try:
            os.unlink(old)
        except OSError:
            pass
    _cache[key] = tmp
    return tmp


def open_light(path: str):
    """Read-only Presentation of `path` without its pictures in memory (falls back to a normal open)."""
    try:
        return Presentation(light_copy(path))
    except (zipfile.BadZipFile, OSError, KeyError):
        return Presentation(path)


def free():
    """Release decks that are no longer referenced (python-pptx objects hold reference cycles, so a
    dropped deck stays in memory until the cycle collector runs)."""
    gc.collect()


RENDER_MAX_PX = 1600      # long edge of pictures in the copy LibreOffice renders (previews and PDF)


def render_copy(path: str, outdir: str) -> str:
    """Copy of the deck for LibreOffice with big pictures scaled down (same file name, in `outdir`).

    LibreOffice decodes every picture it draws, so a deck of full-size phone screenshots needs
    gigabytes to render. Pictures are shown a few inches tall on a slide, so 1600 px on the long
    edge still prints sharply. The deck the employee downloads is not changed."""
    from PIL import Image
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, os.path.basename(path))
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = zipfile.ZIP_DEFLATED
            if info.filename.startswith(_MEDIA_PREFIX) and info.file_size > 200_000 and \
                    info.filename.lower().endswith((".png", ".jpg", ".jpeg")):
                data = zin.read(info)
                try:
                    small = _shrink(data, Image)
                except Exception:  # noqa: BLE001 - an unreadable picture is copied as it is
                    small = None
                zout.writestr(zi, small or data)
                del data, small
                continue
            with zin.open(info) as src, zout.open(zi, "w", force_zip64=True) as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
    return out


def _shrink(data: bytes, Image) -> bytes | None:
    import io
    with Image.open(io.BytesIO(data)) as im:
        if max(im.size) <= RENDER_MAX_PX or im.format not in ("PNG", "JPEG"):
            return None
        fmt = im.format
        im.draft("RGB", (RENDER_MAX_PX, RENDER_MAX_PX)) if fmt == "JPEG" else None
        im.thumbnail((RENDER_MAX_PX, RENDER_MAX_PX))
        buf = io.BytesIO()
        if fmt == "JPEG":
            im.convert("RGB").save(buf, "JPEG", quality=88, optimize=True)
        else:
            im.save(buf, "PNG", optimize=False, compress_level=6)
    b = buf.getvalue()
    return b if len(b) < len(data) else None
