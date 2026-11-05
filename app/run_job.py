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
        # record exception type + location only (no report data) for admins
        tb = traceback.extract_tb(e.__traceback__)[-3:]
        with Session() as s:
            j = s.get(Job, job_id)
            j.error = f"{type(e).__name__} at " + " <- ".join(f"{f.filename.split('/')[-1]}:{f.lineno}" for f in tb)
            s.commit()
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
