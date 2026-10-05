import json
import sys
import types
from pathlib import Path

import pytest
from pydantic import ValidationError

from core import runner
from core.config import load_all_jobs
from core.runner import execute_job
from core.validation import format_problems, validate_rows
from schemas.job_schema import JobConfig, ValidationConfig
from utils import db


def cfg(**kw):
    return ValidationConfig(**kw)


# ---- validate_rows -------------------------------------------------------

def test_no_config_only_checks_rows_are_dicts():
    assert validate_rows([{"a": 1}], None) == []
    assert validate_rows([], None) == []
    problems = validate_rows([{"a": 1}, "oops"], None)
    assert len(problems) == 1 and "not dicts" in problems[0] and "row 1" in problems[0]


def test_min_rows():
    assert validate_rows([{"a": 1}], cfg(min_rows=1)) == []
    problems = validate_rows([], cfg(min_rows=1))
    assert problems == ["expected at least 1 row(s), got 0"]
    assert validate_rows([], cfg(min_rows=0)) == []


def test_required_fields_missing_null_and_blank():
    rows = [{"date": "d1"}, {"date": None}, {"date": "  "}, {"other": 1}]
    (problem,) = validate_rows(rows, cfg(required_fields=["date"]))
    assert "'date'" in problem and "3 of 4" in problem and "first at row 1" in problem
    assert validate_rows([{"date": "d1", "p": 0}], cfg(required_fields=["date", "p"])) == []   # 0 counts as present


def test_types():
    rows = [{"p": 1.5, "n": 2, "s": "x", "b": True, "z": None}]
    ok = cfg(types={"p": "number", "n": "integer", "s": "string", "b": "boolean", "z": "number", "absent": "string"})
    assert validate_rows(rows, ok) == []

    (problem,) = validate_rows([{"p": 1}, {"p": "1,234"}, {"p": "x"}], cfg(types={"p": "number"}))
    assert "'p'" in problem and "2 row(s)" in problem and "str '1,234'" in problem

    # bool is not a number/integer, and a float is not an integer
    assert validate_rows([{"p": True}], cfg(types={"p": "number"}))
    assert validate_rows([{"n": True}], cfg(types={"n": "integer"}))
    assert validate_rows([{"n": 1.5}], cfg(types={"n": "integer"}))


def test_multiple_problems_are_all_reported_and_summary_is_capped():
    problems = validate_rows([{"a": "x"}], cfg(min_rows=2, required_fields=["d"], types={"a": "number"}))
    assert len(problems) == 3
    many = [f"problem {i}" for i in range(8)]
    text = format_problems(many)
    assert text.startswith("Output validation failed: problem 0;") and "problem 4" in text
    assert "problem 5" not in text and text.endswith("(+3 more)")


def test_config_rejects_unknown_type_and_negative_min_rows():
    with pytest.raises(ValidationError):
        ValidationConfig(types={"a": "decimal"})
    with pytest.raises(ValidationError):
        ValidationConfig(min_rows=-1)


def test_shipped_job_yamls_load_with_validation():
    jobs = {j.job_name: j for j in load_all_jobs(Path(__file__).parent.parent / "jobs")}
    assert jobs["scrap_monster_prices"].validation.required_fields == ["commodity", "region", "date", "price"]
    assert jobs["scrap_monster_prices"].validation.types == {"price": "number"}
    assert jobs["scrap_monster_prices"].output.key == ["commodity", "region", "date"]
    assert jobs["scrap_monster_prices"].validation.min_rows == 1
    assert jobs["usgs_download"].validation.required_fields == ["url", "status"]


# ---- runner integration --------------------------------------------------

def install(monkeypatch, name, fn):
    mod = types.ModuleType(name)
    mod.run = fn
    monkeypatch.setitem(sys.modules, name, mod)
    return name


def make_job(tmp_path, script, **kw):
    data = {
        "job_name": "val_job", "script": script, "schedule": "hourly", "isolate": False,
        "output": {"type": "json", "path": str(tmp_path / "out.json"), "key": ["id"], "export": "all"},
        "retry": {"retries": 1, "retry_delay": 0},
        "validation": {"min_rows": 1, "required_fields": ["id"], "types": {"price": "number"}},
    }
    data.update(kw)
    return JobConfig(**data)


def test_invalid_output_fails_the_run_and_keeps_last_good_data(tmp_path, monkeypatch):
    batches = iter([
        [{"id": 1, "price": 10.0}, {"id": 2, "price": 20.0}],
        [{"id": 3, "price": "N/A"}, {"price": 5.0}],          # junk: wrong type and missing id
    ])
    script = install(monkeypatch, "val_scraper", lambda params=None: next(batches))
    job = make_job(tmp_path, script)
    out = tmp_path / "out.json"

    assert execute_job(job) is True
    good_file = out.read_text(encoding="utf-8")
    good_rows = db.load_results("val_job")
    assert len(good_rows) == 2

    assert execute_job(job) is False
    assert out.read_text(encoding="utf-8") == good_file          # export untouched
    assert db.load_results("val_job") == good_rows               # nothing stored

    run = db.recent_runs("val_job")[0]
    assert run["status"] == "failed"
    assert run["error"].startswith("Output validation failed:")
    assert "'id'" in run["error"] and "'price'" in run["error"]


def test_empty_result_fails_when_min_rows_set_and_notifies(tmp_path, monkeypatch):
    script = install(monkeypatch, "val_empty", lambda params=None: [])
    sent = []
    monkeypatch.setattr(runner, "send_webhook", lambda url, payload: sent.append(payload))
    job = make_job(tmp_path, script, notify={"webhook_url": "http://hook.test/x"})

    assert execute_job(job) is False
    assert db.recent_runs("val_job")[0]["status"] == "failed"      # not "empty"
    assert len(sent) == 1 and sent[0]["status"] == "failed"
    assert "expected at least 1 row(s)" in sent[0]["error"]
    assert not (tmp_path / "out.json").exists()


def test_without_validation_block_behaviour_is_unchanged(tmp_path, monkeypatch):
    script = install(monkeypatch, "val_none", lambda params=None: [])
    job = make_job(tmp_path, script, validation=None, output={"type": "json", "path": str(tmp_path / "o.json")})
    assert execute_job(job) is True
    assert db.recent_runs("val_job")[0]["status"] == "empty"


def test_non_dict_rows_always_fail(tmp_path, monkeypatch):
    script = install(monkeypatch, "val_strs", lambda params=None: ["a", "b"])
    job = make_job(tmp_path, script, validation=None, output={"type": "json", "path": str(tmp_path / "o.json")})
    assert execute_job(job) is False
    assert "not dicts" in db.recent_runs("val_job")[0]["error"]


def test_valid_output_passes(tmp_path, monkeypatch):
    script = install(monkeypatch, "val_ok", lambda params=None: [{"id": 1, "price": 1.0}])
    assert execute_job(make_job(tmp_path, script)) is True
    assert json.loads((tmp_path / "out.json").read_text(encoding="utf-8")) == [{"id": 1, "price": 1.0}]
