import logging

import pytest


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point the results/run-history database at a temp file for every test."""
    monkeypatch.setenv("SIIMPLI_DB", str(tmp_path / "test.db"))


@pytest.fixture(autouse=True)
def reset_logging():
    """configure_logging() installs a root handler; remove it so tests don't leak it."""
    root = logging.getLogger()
    level = root.level
    yield
    for h in [h for h in root.handlers if getattr(h, "_siimpli", False)]:
        root.removeHandler(h)
    root.setLevel(level)
