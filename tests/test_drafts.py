import json
import time

import pytest
import yaml
from typer.testing import CliRunner

from core import drafts, safe_http
from core.drafts import DraftError, DraftStore
from core.mcp_server import JobService
from main import app
from tests.conftest_site import Site
from utils import db

PRICES = """<table id="p"><tr><td>2025-01-01</td><td>1,234.5</td></tr><tr><td>2025-01-02</td><td>1,300</td></tr></table>"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    """cwd = tmp dir (jobs/, data/ live there), local allowlisted site, loopback allowed in-process."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "jobs").mkdir()
    site = Site()
    site.routes["/p"] = (200, {}, PRICES)
    allow = tmp_path / "allowed_domains.txt"
    allow.write_text("127.0.0.1\n", encoding="utf-8")
    monkeypatch.setenv(safe_http.ALLOWLIST_ENV, str(allow))
    monkeypatch.setattr(safe_http, "_ip_blocked", lambda ip: False)
    monkeypatch.setattr(safe_http, "ALLOWED_PORTS", (None, 80, 443, site.port))
    # run dry-runs in-process so the patches above apply; the subprocess path has its own test
    real_execute = drafts.execute_job
    monkeypatch.setattr(
        drafts, "execute_job",
        lambda cfg, **kw: real_execute(cfg.model_copy(update={"isolate": False}), **kw))
    yield site, DraftStore("jobs")
    site.close()


def good(site, **over):
    data = {
        "job_name": "my_prices",
        "script": "scrapers.declarative",
        "schedule": "daily",
        "output": {"type": "csv", "path": "data/my_prices/prices.csv", "key": ["date"], "export": "all"},
        "retry": {"retries": 1, "retry_delay": 0},
        "rate_limit": {"rate_limit": 10, "delay_min": 0, "delay_max": 0, "respect_robots": True},
        "validation": {"min_rows": 1, "required_fields": ["date", "price"], "types": {"price": "number"}},
        "timeout_seconds": 60,
        "parameters": {"steps": [
            {"fetch": {"url": f"{site.base}/p", "as": "page"}},
            {"extract": {"from": "page", "rows": "table#p tr", "as": "rows",
                         "fields": {"date": {"select": "td:nth-child(1)"},
                                    "price": {"select": "td:nth-child(2)", "type": "number"}}}},
            {"emit": {"from": "rows"}},
        ]},
    }
    data.update(over)
    return data


def propose(store, data):
    return store.propose(yaml.safe_dump(data), author="test")


# ---- proposing ----------------------------------------------------------

def test_valid_proposal_is_saved_as_a_draft_and_never_loaded_as_a_job(env):
    site, store = env
    res = propose(store, dict(good(site), evil_extra_field="x"))
    assert res["ok"] and res["name"] == "my_prices"
    saved = yaml.safe_load((store.drafts_dir / "my_prices.yaml").read_text())
    assert "evil_extra_field" not in saved                     # only validated fields reach disk
    from core.config import load_all_jobs
    assert load_all_jobs("jobs") == []                         # invisible to the scheduler / run --all
    assert [d["name"] for d in store.list()] == ["my_prices"]


@pytest.mark.parametrize("change, expect", [
    ({"script": "scrapers.first_scraper"}, "agents may not supply code"),
    ({"script": "os.system"}, "agents may not supply code"),
    ({"notify": {"webhook_url": "http://x"}}, "notify is not allowed"),
    ({"isolate": False}, "isolate"),
    ({"timeout_seconds": None}, "timeout_seconds"),
    ({"timeout_seconds": 100000}, "timeout_seconds"),
    ({"retry": {"retries": 50, "retry_delay": 0}}, "retries"),
    ({"validation": None}, "validation is required"),
    ({"validation": {"min_rows": 1}}, "validation is required"),
    ({"validation": {"required_fields": ["a"]}}, "validation is required"),
    ({"rate_limit": None}, "rate_limit is required"),
    ({"rate_limit": {"rate_limit": 500, "respect_robots": True}}, "at most 30"),
    ({"rate_limit": {"rate_limit": 10, "respect_robots": False}}, "respect_robots"),
    ({"job_name": "../evil"}, "job_name"),
    ({"job_name": "Has Spaces"}, "job_name"),
    ({"output": {"type": "csv", "path": "/etc/cron.d/x.csv"}}, "relative"),
    ({"output": {"type": "csv", "path": "C:\\Windows\\x.csv"}}, "relative"),
    ({"output": {"type": "csv", "path": "data/../jobs/x.csv"}}, "under data/"),
    ({"output": {"type": "csv", "path": "elsewhere/x.csv"}}, "under data/"),
    ({"output": {"type": "csv", "path": "data/drafts/x.csv"}}, "reserved"),
    ({"output": {"type": "json", "path": "data/x.csv"}}, "end in .json"),
    ({"output": {"type": "csv", "path": "data/siimpli.db"}}, "end in .csv"),
])
def test_rejected_proposals_say_why_and_save_nothing(env, change, expect):
    site, store = env
    res = propose(store, good(site, **change))
    assert res["ok"] is False
    assert any(expect in p for p in res["problems"]), res["problems"]
    assert store.list() == []


def test_program_errors_are_reported(env):
    site, store = env
    bad = good(site, parameters={"steps": [{"fetch": {"url": "http://x", "as": "p", "headers": {"Cookie": "a"}}}]})
    res = propose(store, bad)
    assert not res["ok"] and any("Cookie" in p or "header" in p for p in res["problems"])
    res = propose(store, good(site, parameters={"steps": [{"exec": "import os"}]}))
    assert not res["ok"]


def test_literal_urls_must_be_on_the_allowlist(env):
    site, store = env
    steps = good(site)["parameters"]["steps"]
    steps[0]["fetch"]["url"] = "https://not-allowed.example.org/data"
    res = propose(store, good(site, parameters={"steps": steps}))
    assert not res["ok"] and any("allowlist" in p for p in res["problems"])


@pytest.mark.parametrize("text, expect", [
    ("", "empty"),
    ("x: [1, 2", "invalid YAML"),
    ("- a\n- b", "mapping"),
    ("a: &x [1]\nb: *x\n", "anchors"),
    pytest.param("a: " + "x" * 60000, "longer than", id="oversized"),
])
def test_malformed_yaml_is_refused(env, text, expect):
    _, store = env
    res = store.propose(text)
    assert not res["ok"] and any(expect in p for p in res["problems"])


def test_cannot_shadow_a_live_job_or_reuse_its_output(env):
    site, store = env
    live = good(site, job_name="live_one")
    live["output"]["path"] = "data/live/out.csv"
    (store.jobs_dir / "live_one.yaml").write_text(yaml.safe_dump(live), encoding="utf-8")
    assert not propose(store, good(site, job_name="live_one"))["ok"]
    clash = good(site, job_name="other")
    clash["output"]["path"] = "data/live/out.csv"
    res = propose(store, clash)
    assert not res["ok"] and any("already used" in p for p in res["problems"])


def test_reproposing_replaces_the_draft_and_resets_its_test_status(env):
    site, store = env
    propose(store, good(site))
    store.run_test("my_prices")
    assert store.get("my_prices")["tested_current_version"]
    changed = good(site)
    changed["schedule"] = "hourly"
    propose(store, changed)
    assert not store.get("my_prices")["tested_current_version"]


# ---- dry runs ------------------------------------------------------------

def test_dry_run_config_differs_from_production_only_where_intended(env):
    site, store = env
    propose(store, good(site))
    _, config = store._load("my_prices")
    dry_cfg = store._test_config(config)
    prod, dry = config.model_dump(), dry_cfg.model_dump()
    changed = {k for k in prod if prod[k] != dry[k]}
    assert changed <= {"job_name", "output", "notify", "isolate"}
    for field in ("retry", "rate_limit", "validation", "timeout_seconds", "parameters", "script", "schedule"):
        assert prod[field] == dry[field]
    assert dry["output"]["key"] == prod["output"]["key"] and dry["output"]["export"] == prod["output"]["export"]
    assert dry["job_name"] == "draft:my_prices" and dry["notify"] is None and dry["isolate"] is True
    assert dry["output"]["path"] == "data/drafts/my_prices/out.csv"


def test_dry_run_goes_through_the_real_runner_and_stays_in_its_namespace(env, tmp_path):
    site, store = env
    propose(store, good(site))
    res = store.run_test("my_prices")
    assert res["status"] == "success" and res["item_count"] == 2
    assert len(db.load_results("draft:my_prices")) == 2
    assert db.load_results("my_prices") == []                  # nothing under the production name
    assert (tmp_path / "data/drafts/my_prices/out.csv").exists()
    assert not (tmp_path / "data/my_prices/prices.csv").exists()
    assert db.recent_runs("draft:my_prices")[0]["status"] == "success"
    d = store.get("my_prices")
    assert d["tested_current_version"] and d["result_rows"] == 2 and d["sample"][0]["date"] == "2025-01-01"


def test_dry_run_applies_production_validation(env):
    site, store = env
    site.routes["/p"] = (200, {}, PRICES.replace("1,300", "n/a"))
    propose(store, good(site))
    res = store.run_test("my_prices")
    assert res["status"] == "failed" and "'price'" in res["error"]
    assert not store.get("my_prices")["tested_current_version"]


def test_each_dry_run_starts_clean(env):
    site, store = env
    propose(store, good(site))
    store.run_test("my_prices")
    site.routes["/p"] = (200, {}, PRICES.replace("2025-01-02", "2025-02-02"))
    store.run_test("my_prices")
    assert sorted(r["date"] for r in db.load_results("draft:my_prices")) == ["2025-01-01", "2025-02-02"]


def test_dry_run_sends_no_webhook(env, monkeypatch):
    site, store = env
    propose(store, good(site))
    sent = []
    monkeypatch.setattr("core.runner.send_webhook", lambda *a, **k: sent.append(a))
    site.routes["/p"] = (200, {}, "<p>nothing</p>")
    assert store.run_test("my_prices")["status"] == "failed"
    assert sent == []


def test_a_real_subprocess_dry_run_still_enforces_the_real_network_rules(tmp_path, monkeypatch):
    """No in-process patches: the child process must refuse a loopback target."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "jobs").mkdir()
    site = Site()
    try:
        allow = tmp_path / "allowed_domains.txt"
        allow.write_text("127.0.0.1\n", encoding="utf-8")
        monkeypatch.setenv(safe_http.ALLOWLIST_ENV, str(allow))
        store = DraftStore("jobs")
        assert propose(store, good(site))["ok"]          # allowlisted literal host passes the static check
        res = store.run_test("my_prices")
        assert res["status"] == "failed" and "blocked" in res["error"]
        assert site.hits == []
    finally:
        site.close()


# ---- approval -------------------------------------------------------------

def test_approve_requires_a_passing_test_of_the_exact_version(env):
    site, store = env
    propose(store, good(site))
    with pytest.raises(DraftError, match="no successful dry run"):
        store.approve("my_prices")
    store.run_test("my_prices")
    # edit the draft file behind the test's back: the hash no longer matches
    path = store.drafts_dir / "my_prices.yaml"
    path.write_text(path.read_text() + "\n# tampered\n", encoding="utf-8")
    with pytest.raises(DraftError, match="no successful dry run"):
        store.approve("my_prices")


def test_approve_moves_the_draft_into_jobs_and_cleans_up(env):
    site, store = env
    propose(store, good(site))
    store.run_test("my_prices")
    target = store.approve("my_prices")
    assert target == store.jobs_dir / "my_prices.yaml" and target.exists()
    assert not (store.drafts_dir / "my_prices.yaml").exists()
    assert not (store.drafts_dir / "my_prices.meta.json").exists()
    assert db.load_results("draft:my_prices") == []
    from core.config import load_all_jobs
    assert [j.job_name for j in load_all_jobs("jobs")] == ["my_prices"]
    with open("data/agent_audit.jsonl", encoding="utf-8") as f:
        audit = [json.loads(line)["event"] for line in f]
    assert audit == ["propose", "test_start", "test_end", "approve"]


def test_approve_refuses_if_checks_no_longer_pass(env, tmp_path):
    site, store = env
    propose(store, good(site))
    store.run_test("my_prices")
    (tmp_path / "allowed_domains.txt").write_text("", encoding="utf-8")   # owner revoked the domain
    with pytest.raises(DraftError, match="allowlist"):
        store.approve("my_prices")


def test_approve_never_overwrites_a_live_job(env):
    site, store = env
    propose(store, good(site))
    store.run_test("my_prices")
    (store.jobs_dir / "my_prices.yaml").write_text("job_name: not_valid_but_present\n", encoding="utf-8")
    with pytest.raises(DraftError):
        store.approve("my_prices")
    assert (store.jobs_dir / "my_prices.yaml").read_text() == "job_name: not_valid_but_present\n"


def test_draft_names_cannot_escape_the_drafts_directory(env):
    _, store = env
    for name in ("../jobs/x", "a/b", "..", "x.yaml", "", "UP"):
        with pytest.raises(DraftError):
            store.get(name)
        with pytest.raises(DraftError):
            store.delete(name)


# ---- the agent-facing service and the CLI --------------------------------

def test_service_flow_propose_test_poll_inspect(env):
    site, _ = env
    svc = JobService("jobs")
    assert svc.propose_job(yaml.safe_dump(good(site)))["ok"]
    started = svc.test_job("my_prices")
    assert started["started"]
    for _ in range(100):
        run = svc.get_run(started["run_id"])
        if run["status"] != "running":
            break
        time.sleep(0.1)
    assert run["status"] == "success"
    d = svc.get_draft("my_prices")
    assert d["tested_current_version"] and len(d["sample"]) == 2
    assert svc.list_drafts()[0]["last_test"]["status"] == "success"
    with pytest.raises(ValueError, match="no draft"):
        svc.get_draft("missing")
    assert svc.delete_draft("my_prices") == {"deleted": True}
    assert db.load_results("draft:my_prices") == []
    assert not any("approve" in n for n in dir(svc))


def test_cli_review_flow(env):
    site, store = env
    propose(store, good(site))
    runner = CliRunner()
    assert "never run" in runner.invoke(app, ["draft", "list"]).stdout
    refused = runner.invoke(app, ["draft", "approve", "my_prices", "-y"])
    assert refused.exit_code == 1 and "no successful dry run" in refused.stdout
    tested = runner.invoke(app, ["draft", "test", "my_prices"])
    assert tested.exit_code == 0 and "success" in tested.stdout
    declined = runner.invoke(app, ["draft", "approve", "my_prices"], input="n\n")
    assert declined.exit_code != 0 and not (store.jobs_dir / "my_prices.yaml").exists()
    ok = runner.invoke(app, ["draft", "approve", "my_prices", "-y"])
    assert ok.exit_code == 0 and (store.jobs_dir / "my_prices.yaml").exists()
