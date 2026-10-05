import io
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from core.logger import LOG_LEVEL_ENV, configure_logging, get_logger
from main import app

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def restore_env(monkeypatch):
    # configure_logging writes os.environ directly; register it so teardown restores it
    monkeypatch.setenv(LOG_LEVEL_ENV, "INFO")


def handlers():
    return [h for h in logging.getLogger().handlers if getattr(h, "_siimpli", False)]


def test_logs_go_to_the_given_stream_at_the_configured_level():
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    log = get_logger("some.module")
    log.info("hello world")
    log.debug("hidden detail")
    out = buf.getvalue()
    assert "hello world" in out and "INFO" in out and "some.module" in out
    assert "hidden detail" not in out


def test_debug_level_shows_debug():
    buf = io.StringIO()
    configure_logging("debug", stream=buf)
    get_logger("x").debug("detail")
    assert "detail" in buf.getvalue()


def test_configure_is_idempotent_and_can_change_level():
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    configure_logging("INFO", stream=buf)
    assert len(handlers()) == 1
    get_logger("x").info("once")
    assert buf.getvalue().count("once") == 1
    configure_logging("WARNING", stream=buf)
    get_logger("x").info("suppressed")
    assert "suppressed" not in buf.getvalue()


def test_level_comes_from_env_and_is_exported_for_workers(monkeypatch):
    monkeypatch.setenv(LOG_LEVEL_ENV, "WARNING")
    configure_logging(stream=io.StringIO())
    assert logging.getLogger().level == logging.WARNING
    configure_logging("DEBUG", stream=io.StringIO())
    assert os.environ[LOG_LEVEL_ENV] == "DEBUG"


def test_invalid_level_falls_back_to_info():
    configure_logging("LOUD", stream=io.StringIO())
    assert logging.getLogger().level == logging.INFO
    assert os.environ[LOG_LEVEL_ENV] == "INFO"


def test_noisy_third_party_loggers_are_quieted():
    configure_logging("DEBUG", stream=io.StringIO())
    assert logging.getLogger("apscheduler").level >= logging.WARNING
    assert logging.getLogger("urllib3").level >= logging.WARNING


def test_cli_verbose_flag_sets_debug():
    result = CliRunner().invoke(app, ["-v", "list"])
    assert result.exit_code == 0
    assert os.environ[LOG_LEVEL_ENV] == "DEBUG"


def test_cli_default_level_is_info():
    CliRunner().invoke(app, ["list"])
    assert os.environ[LOG_LEVEL_ENV] == "INFO"


def test_job_worker_process_logs_scraper_messages_to_stderr(tmp_path):
    """Scraper logging inside the child process must be visible (it used to be dropped)."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "chatty_scraper.py").write_text(
        "import logging\n"
        "log = logging.getLogger('chatty')\n"
        "def run(params=None):\n"
        "    log.info('fetching page 1')\n"
        "    log.debug('parsing details')\n"
        "    return [{'a': 1}]\n",
        encoding="utf-8",
    )
    job = tmp_path / "job.json"
    job.write_text(
        '{"job_name": "chatty", "script": "chatty_scraper", "schedule": "hourly",'
        ' "output": {"type": "json", "path": "o.json"}}', encoding="utf-8")

    def run_worker(level):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(scripts)])
        env[LOG_LEVEL_ENV] = level
        return subprocess.run(
            [sys.executable, "-m", "core.worker", str(job), str(tmp_path / f"result_{level}.json")],
            env=env, capture_output=True, text=True, cwd=str(tmp_path),
        )

    info = run_worker("INFO")
    assert info.returncode == 0
    assert "fetching page 1" in info.stderr and "parsing details" not in info.stderr
    assert "fetching page 1" not in info.stdout

    debug = run_worker("DEBUG")
    assert "parsing details" in debug.stderr
