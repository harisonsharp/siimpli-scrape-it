import pytest


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point the results/run-history database at a temp file for every test."""
    monkeypatch.setenv("SIIMPLI_DB", str(tmp_path / "test.db"))
