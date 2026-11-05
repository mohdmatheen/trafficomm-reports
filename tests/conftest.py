"""Test configuration. Web tests use a separate Postgres database (TEST_DATABASE_URL, default
trafficomm_test on the local socket) and a temporary storage directory."""
import os
import tempfile

os.environ.setdefault("TEST_DATABASE_URL", "postgresql+psycopg://postgres@/trafficomm_test?host=/tmp")
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["STORAGE_DIR"] = tempfile.mkdtemp(prefix="tc_storage_")
os.environ.pop("ANTHROPIC_API_KEY", None)  # tests are deterministic: rule-based (mocked) AI only
os.environ.setdefault("SECRET_KEY", "test-secret-key")

import pytest  # noqa: E402

from tests.fixtures.make_fixtures import FIX_DIR_READY, main as make_fixtures  # noqa: E402,F401


def pytest_configure(config):
    config.addinivalue_line("markers", "real_client: uses real client sample files (skipped when absent)")
    config.addinivalue_line("markers", "web: end-to-end through the web app, database and worker")
    make_fixtures()
