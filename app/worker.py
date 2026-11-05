"""Run the background worker:  python -m app.worker"""
import logging

from .db import init_db
from .jobs import worker_loop

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    init_db()
    worker_loop()
