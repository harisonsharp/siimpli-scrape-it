import csv
import json
import sys
import types

import pytest

from core import runner
from core.runner import execute_job
from schemas.job_schema import JobConfig
from utils import db


def install_scraper(monkeypatch, name, fn):
    """Register an in-memory scraper module so nothing is written to scrapers/."""
    mod = types.ModuleType(name)
    mod.run = fn
    monkeypatch.setitem(sys.modules, name, mod)
    return name


def make_job(tmp_path, script, **overrides):
    data = {
        "job_name": "hist_job",
        "script": script,
        "schedule": "hourly",
        "output": {"type": "json", "path": str(tmp_path / "out.json")},
        "retry": {"retries": 1, "retry_delay": 0},
        "isolate": False,   # fake scrapers live in sys.modules; see test_isolation.py for subprocess runs
    }
    data.update(overrides)
    return JobConfig(**data)


def test_success_is_recorded(tmp_path, monkeypatch):
    script = install_scraper(monkeypatch, "fake_ok", lambda params=None: [{"a": 1}, {"a": 2}])
    assert execute_job(make_job(tmp_path, script)) is True

    (run,) = db.recent_runs("hist_job")
    assert run["status"] == "success"
    assert run["item_count"] == 2 and run["attempts"] == 1
    assert run["finished_at"] and run["duration_s"] >= 0
    assert len(db.load_results("hist_job")) == 2


def test_failure_is_recorded_with_error_and_attempts(tmp_path, monkeypatch):
    def boom(params=None):
        raise ValueError("induced")
    script = install_scraper(monkeypatch, "fake_fail", boom)
    job = make_job(tmp_path, script, retry={"retries": 2, "retry_delay": 0})
    assert execute_job(job) is False

    (run,) = db.recent_runs("hist_job")
    assert run["status"] == "failed" and run["attempts"] == 2
    assert "ValueError: induced" in run["error"]


def test_empty_result_is_flagged_but_succeeds(tmp_path, monkeypatch):
    script = install_scraper(monkeypatch, "fake_empty", lambda params=None: [])
    assert execute_job(make_job(tmp_path, script)) is True
    assert db.recent_runs("hist_job")[0]["status"] == "empty"


def test_keyed_export_all_accumulates_history(tmp_path, monkeypatch):
    batches = iter([
        [{"date": "d1", "price": 1}, {"date": "d2", "price": 2}],
        [{"date": "d2", "price": 22}, {"date": "d3", "price": 3}],
    ])
    script = install_scraper(monkeypatch, "fake_series", lambda params=None: next(batches))
    out = tmp_path / "prices.csv"
    job = make_job(tmp_path, script, output={"type": "csv", "path": str(out), "key": ["date"], "export": "all"})

    assert execute_job(job) and execute_job(job)
    with open(out, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows == [
        {"date": "d1", "price": "1"},
        {"date": "d2", "price": "22"},
        {"date": "d3", "price": "3"},
    ]


def test_export_latest_is_default(tmp_path, monkeypatch):
    batches = iter([[{"a": 1}], [{"a": 2}]])
    script = install_scraper(monkeypatch, "fake_latest", lambda params=None: next(batches))
    job = make_job(tmp_path, script)
    execute_job(job)
    execute_job(job)
    assert json.loads((tmp_path / "out.json").read_text(encoding="utf-8")) == [{"a": 2}]


def test_database_failure_does_not_fail_job(tmp_path, monkeypatch):
    script = install_scraper(monkeypatch, "fake_nodb", lambda params=None: [{"a": 1}])

    def broken(*a, **k):
        raise OSError("disk gone")
    monkeypatch.setattr(db, "start_run", broken)
    assert execute_job(make_job(tmp_path, script)) is True
    assert (tmp_path / "out.json").exists()


@pytest.mark.parametrize("scraper_result,expected_status", [(RuntimeError("x"), "failed"), ([], "empty")])
def test_webhook_sent_on_failure_and_empty(tmp_path, monkeypatch, scraper_result, expected_status):
    def fn(params=None):
        if isinstance(scraper_result, Exception):
            raise scraper_result
        return scraper_result
    script = install_scraper(monkeypatch, f"fake_hook_{expected_status}", fn)
    sent = []
    monkeypatch.setattr(runner, "send_webhook", lambda url, payload: sent.append((url, payload)))
    job = make_job(tmp_path, script, notify={"webhook_url": "http://hook.test/x"})
    execute_job(job)

    assert len(sent) == 1
    url, payload = sent[0]
    assert url == "http://hook.test/x"
    assert payload["status"] == expected_status and payload["job_name"] == "hist_job"


def test_no_webhook_on_success_or_when_disabled(tmp_path, monkeypatch):
    script = install_scraper(monkeypatch, "fake_quiet", lambda params=None: [{"a": 1}])
    sent = []
    monkeypatch.setattr(runner, "send_webhook", lambda *a: sent.append(a))
    execute_job(make_job(tmp_path, script, notify={"webhook_url": "http://hook.test/x"}))
    assert sent == []

    script2 = install_scraper(monkeypatch, "fake_quiet2", lambda params=None: [])
    execute_job(make_job(tmp_path, script2, notify={"webhook_url": "http://hook.test/x", "on_empty": False}))
    assert sent == []
