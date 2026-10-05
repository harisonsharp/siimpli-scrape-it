import os
import subprocess
import time

import pytest

from core.exceptions import JobFailedError
from core.isolation import run_in_subprocess
from core.runner import execute_job
from schemas.job_schema import JobConfig
from utils import db


@pytest.fixture
def scraper_dir(tmp_path, monkeypatch):
    """A directory on the child's PYTHONPATH where tests drop real scraper modules."""
    d = tmp_path / "scripts"
    d.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(d))
    return d


def write_scraper(scraper_dir, name, body):
    (scraper_dir / f"{name}.py").write_text(body, encoding="utf-8")
    return name


def make_job(tmp_path, script, **overrides):
    data = {
        "job_name": "iso_job",
        "script": script,
        "schedule": "hourly",
        "output": {"type": "json", "path": str(tmp_path / "out.json")},
        "retry": {"retries": 1, "retry_delay": 0},
    }
    data.update(overrides)
    return JobConfig(**data)


def pid_alive(pid: int) -> bool:
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_defaults_are_isolated_with_timeout(tmp_path):
    job = make_job(tmp_path, "x")
    assert job.isolate is True and job.timeout_seconds == 3600


def test_returns_rows_from_child_process(tmp_path, scraper_dir):
    name = write_scraper(scraper_dir, "iso_ok", "def run(params=None):\n    return [{'a': 1, 'p': params}]\n")
    job = make_job(tmp_path, name, parameters={"x": 5})
    assert run_in_subprocess(job) == [{"a": 1, "p": {"x": 5}}]


def test_script_exception_becomes_jobfailederror(tmp_path, scraper_dir):
    name = write_scraper(scraper_dir, "iso_err", "def run(params=None):\n    raise ValueError('induced')\n")
    with pytest.raises(JobFailedError, match="ValueError: induced"):
        run_in_subprocess(make_job(tmp_path, name))


def test_missing_run_function(tmp_path, scraper_dir):
    name = write_scraper(scraper_dir, "iso_norun", "x = 1\n")
    with pytest.raises(JobFailedError, match="missing 'run'"):
        run_in_subprocess(make_job(tmp_path, name))


def test_script_calling_sys_exit_is_a_failure(tmp_path, scraper_dir):
    name = write_scraper(scraper_dir, "iso_exit", "import sys\ndef run(params=None):\n    sys.exit(2)\n")
    with pytest.raises(JobFailedError, match="SystemExit"):
        run_in_subprocess(make_job(tmp_path, name))


def test_hard_crash_without_result(tmp_path, scraper_dir):
    name = write_scraper(scraper_dir, "iso_crash", "import os\ndef run(params=None):\n    os._exit(3)\n")
    with pytest.raises(JobFailedError, match="code 3 without a result"):
        run_in_subprocess(make_job(tmp_path, name))


def test_script_prints_do_not_corrupt_result(tmp_path, scraper_dir):
    name = write_scraper(scraper_dir, "iso_print", "def run(params=None):\n    print('noise')\n    return [{'a': 1}]\n")
    assert run_in_subprocess(make_job(tmp_path, name)) == [{"a": 1}]


def test_rate_limiter_is_active_inside_child(tmp_path, scraper_dir):
    name = write_scraper(
        scraper_dir, "iso_rl",
        "from utils.ratelimit import _current\n"
        "def run(params=None):\n    return [{'active': _current.get() is not None}]\n",
    )
    with_rl = make_job(tmp_path, name, rate_limit={"rate_limit": 5, "delay_min": 0, "delay_max": 0})
    assert run_in_subprocess(with_rl) == [{"active": True}]
    assert run_in_subprocess(make_job(tmp_path, name)) == [{"active": False}]


def test_timeout_kills_hung_script_and_its_children(tmp_path, scraper_dir):
    pidfile = tmp_path / "child.pid"
    name = write_scraper(
        scraper_dir, "iso_hang",
        "import subprocess, sys, time\n"
        "def run(params=None):\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        f"    open(r'{pidfile}', 'w').write(str(child.pid))\n"
        "    time.sleep(600)\n",
    )
    job = make_job(tmp_path, name, timeout_seconds=4)

    start = time.perf_counter()
    with pytest.raises(JobFailedError, match="Timed out after 4s"):
        run_in_subprocess(job)
    assert time.perf_counter() - start < 30

    child_pid = int(pidfile.read_text())
    deadline = time.time() + 10
    while pid_alive(child_pid) and time.time() < deadline:
        time.sleep(0.2)
    assert not pid_alive(child_pid), "grandchild process survived the timeout"


def test_execute_job_records_timeout_and_retries(tmp_path, scraper_dir):
    name = write_scraper(scraper_dir, "iso_slow", "import time\ndef run(params=None):\n    time.sleep(600)\n")
    job = make_job(tmp_path, name, timeout_seconds=2, retry={"retries": 2, "retry_delay": 0})
    assert execute_job(job) is False

    (run,) = db.recent_runs("iso_job")
    assert run["status"] == "failed" and run["attempts"] == 2
    assert "Timed out after 2s" in run["error"]


def test_execute_job_end_to_end_in_subprocess(tmp_path, scraper_dir):
    name = write_scraper(scraper_dir, "iso_e2e", "def run(params=None):\n    return [{'d': 'x', 'v': 1}]\n")
    job = make_job(tmp_path, name, output={"type": "json", "path": str(tmp_path / "o.json"), "key": ["d"]})
    assert execute_job(job) is True
    assert db.load_results("iso_job") == [{"d": "x", "v": 1}]


def test_non_list_output_from_child_is_rejected(tmp_path, scraper_dir):
    name = write_scraper(scraper_dir, "iso_dict", "def run(params=None):\n    return {'a': 1}\n")
    assert execute_job(make_job(tmp_path, name)) is False
    assert "Expected List[Dict]" in db.recent_runs("iso_job")[0]["error"]
