"""Subprocess entry point for one job (spawned by the worker with resource limits)."""
import logging
import sys
import traceback

from .db import Job, Session
from .pipeline import Cancelled
from .service import UserError, process_job


def main():
    logging.basicConfig(level=logging.WARNING)
    job_id = int(sys.argv[1])
    try:
        process_job(job_id)
        sys.exit(0)
    except Cancelled:
        sys.exit(3)
    except UserError as e:
        print(f"USER_ERROR: {e}")
        sys.exit(2)
    except MemoryError:
        print("USER_ERROR: The files are too large to process within the memory limit.")
        sys.exit(2)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        sys.stdout.flush()
        # one line the worker turns into the visible error: exception type, message and where it was raised
        print(f"CRASH: {describe_exception(e)}", flush=True)
        try:
            with Session() as s:
                j = s.get(Job, job_id)
                j.error = describe_exception(e)
                s.commit()
        except Exception:  # noqa: BLE001 - never hide the original error behind a DB error
            pass
        sys.exit(1)


def describe_exception(e: BaseException) -> str:
    tb = traceback.extract_tb(e.__traceback__)
    app_frames = [f for f in tb if "/app/" in f.filename.replace("\\", "/")] or tb
    where = " <- ".join(f"{f.filename.replace(chr(92), '/').split('/')[-1]}:{f.lineno}" for f in reversed(app_frames[-3:]))
    msg = " ".join(str(e).split())[:300]
    return f"{type(e).__name__}: {msg} (at {where})" if msg else f"{type(e).__name__} (at {where})"


if __name__ == "__main__":
    main()
