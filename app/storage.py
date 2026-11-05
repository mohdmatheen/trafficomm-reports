"""Private file storage (local directory, not web-served) and expiring signed download links.
Swap `LocalStorage` for an S3/GCS implementation with the same interface in production."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings


def safe_name(name: str) -> str:
    base = os.path.basename(name or "file")
    base = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" .") or "file"
    return base[:150]


class LocalStorage:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        os.chmod(self.root, 0o700)

    def task_dir(self, task_id: int, *parts) -> str:
        d = os.path.join(self.root, "tasks", str(int(task_id)), *[str(p) for p in parts])
        os.makedirs(d, exist_ok=True)
        return d

    def save_upload(self, task_id: int, file_id: int, name: str, src_path: str) -> str:
        d = self.task_dir(task_id, "uploads")
        dest = os.path.join(d, f"{file_id}_{safe_name(name)}")
        shutil.move(src_path, dest)
        os.chmod(dest, stat.S_IRUSR | stat.S_IRGRP)  # originals are kept unchanged (read-only)
        return dest

    def version_dir(self, task_id: int, version_no: int) -> str:
        return self.task_dir(task_id, "versions", f"v{version_no}")

    def path_ok(self, p: str) -> bool:
        return os.path.abspath(p).startswith(self.root + os.sep)

    def delete_task(self, task_id: int):
        d = os.path.join(self.root, "tasks", str(int(task_id)))
        if os.path.isdir(d):
            for dp, dn, fn in os.walk(d):
                for f in fn:
                    try:
                        os.chmod(os.path.join(dp, f), 0o600)
                    except OSError:
                        pass
            shutil.rmtree(d, ignore_errors=True)


storage = LocalStorage(settings.storage_dir)
_signer = URLSafeTimedSerializer(settings.secret_key, salt="download")


def sign_download(artifact_id: int, user_id: int) -> str:
    return _signer.dumps({"a": artifact_id, "u": user_id})


def verify_download(token: str) -> dict | None:
    try:
        return _signer.loads(token, max_age=settings.download_link_ttl)
    except (BadSignature, SignatureExpired):
        return None


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
