import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
import yaml

pytest.importorskip("mcp")

from mcp import Client, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from core.mcp_server import JobService, create_server  # noqa: E402
from utils import db  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

GOOD_SCRAPER = (
    "def run(params=None):\n"
    "    print('NOISE that must not reach the MCP protocol stream')\n"
    "    return [{'commodity': 'copper', 'date': 'd1', 'price': 1},\n"
    "            {'commodity': 'copper', 'date': 'd2', 'price': 2},\n"
    "            {'commodity': 'zinc', 'date': 'd1', 'price': 9}]\n"
)
SLOW_SCRAPER = "import time\ndef run(params=None):\n    time.sleep(3)\n    return [{'a': 1}]\n"
FAIL_SCRAPER = "def run(params=None):\n    raise ValueError('induced failure')\n"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project dir with jobs/, plus scraper modules importable by the worker."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(scripts))

    def add(name, source, **extra):
        (scripts / f"{name}.py").write_text(source, encoding="utf-8")
        cfg = {
            "job_name": name, "script": name, "schedule": "hourly",
            "output": {"type": "json", "path": str(tmp_path / "data" / f"{name}.json"), "key": ["commodity", "date"]},
            "retry": {"retries": 1, "retry_delay": 0},
        }
        cfg.update(extra)
        (jobs / f"{name}.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    add("good_job", GOOD_SCRAPER)
    add("slow_job", SLOW_SCRAPER, output={"type": "json", "path": str(tmp_path / "slow.json")})
    add("bad_job", FAIL_SCRAPER, output={"type": "json", "path": str(tmp_path / "bad.json")})
    return tmp_path


def wait_done(run_id, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = db.get_run(run_id)
        if run and run["status"] != "running":
            return run
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} did not finish")


# ---- JobService (no MCP involved) ---------------------------------------

def test_list_jobs(project):
    svc = JobService(str(project / "jobs"))
    jobs = {j["job_name"]: j for j in svc.list_jobs()}
    assert set(jobs) == {"good_job", "slow_job", "bad_job"}
    assert jobs["good_job"]["output"]["key"] == ["commodity", "date"]
    assert jobs["good_job"]["last_run"] is None


def test_run_job_returns_run_id_immediately_then_completes(project):
    svc = JobService(str(project / "jobs"))
    started = time.time()
    res = svc.run_job("slow_job")
    assert res["started"] is True and time.time() - started < 2   # didn't wait for the 3s scraper
    assert db.get_run(res["run_id"])["status"] == "running"

    run = wait_done(res["run_id"])
    assert run["status"] == "success" and run["item_count"] == 1
    slow = next(j for j in svc.list_jobs() if j["job_name"] == "slow_job")
    assert slow["last_run"]["status"] == "success"


def test_run_job_failure_is_reported_in_run(project):
    svc = JobService(str(project / "jobs"))
    run = wait_done(svc.run_job("bad_job")["run_id"])
    assert run["status"] == "failed" and "induced failure" in run["error"]


def test_run_job_unknown_name(project):
    with pytest.raises(ValueError, match="Unknown job.*good_job"):
        JobService(str(project / "jobs")).run_job("nope")


def test_same_job_cannot_run_twice_concurrently(project):
    svc = JobService(str(project / "jobs"))
    first = svc.run_job("slow_job")
    second = svc.run_job("slow_job")
    assert second == {"started": False, "reason": "already_running", "run_id": first["run_id"]}
    wait_done(first["run_id"])
    assert svc.run_job("slow_job")["started"] is True   # allowed again once finished
    time.sleep(0.2)


def test_query_results_filter_and_paging(project):
    svc = JobService(str(project / "jobs"))
    wait_done(svc.run_job("good_job")["run_id"])

    everything = svc.query_results("good_job")
    assert everything["total"] == 3 and everything["returned"] == 3

    copper = svc.query_results("good_job", where={"commodity": "copper"})
    assert copper["total"] == 2 and {r["date"] for r in copper["rows"]} == {"d1", "d2"}

    page = svc.query_results("good_job", limit=1, offset=2)
    assert page["total"] == 3 and page["returned"] == 1 and page["rows"][0]["commodity"] == "zinc"

    assert svc.query_results("good_job", where={"price": 9})["total"] == 1
    assert svc.query_results("good_job", where={"missing_field": None})["total"] == 3
    assert svc.query_results("good_job", where={"commodity": "gold"})["total"] == 0


def test_query_results_limit_is_capped_and_field_names_validated(project):
    svc = JobService(str(project / "jobs"))
    assert svc.query_results("whatever", limit=10**9)["returned"] == 0   # clamped, no error
    with pytest.raises(ValueError):
        svc.query_results("good_job", where={'a"b': 1})


def test_get_run_unknown(project):
    with pytest.raises(ValueError, match="No run"):
        JobService(str(project / "jobs")).get_run(99999)


def test_get_status_filters_by_job(project):
    svc = JobService(str(project / "jobs"))
    wait_done(svc.run_job("good_job")["run_id"])
    wait_done(svc.run_job("bad_job")["run_id"])
    assert {r["job_name"] for r in svc.get_status()} == {"good_job", "bad_job"}
    assert [r["job_name"] for r in svc.get_status("bad_job")] == ["bad_job"]


# ---- MCP protocol --------------------------------------------------------

def payload(result):
    """Tool result -> plain python, across SDK result shapes."""
    assert not result.is_error, result
    data = getattr(result, "structured_content", None)
    if data is None:
        data = json.loads(result.content[0].text)
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    return data


def test_tools_are_exposed_in_process(project):
    async def main():
        async with Client(create_server(JobService(str(project / "jobs")))) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert names == {"list_jobs", "get_status", "get_run", "query_results", "run_job",
                             "propose_job", "list_drafts", "get_draft", "test_job", "delete_draft"}
            # an agent must never be able to promote a draft or otherwise write into jobs/
            assert not any("approve" in n or "promote" in n for n in names)
            jobs = payload(await client.call_tool("list_jobs"))
            assert {j["job_name"] for j in jobs} == {"good_job", "slow_job", "bad_job"}
            err = await client.call_tool("run_job", {"job_name": "nope"})
            assert err.is_error
    asyncio.run(main())


def test_end_to_end_over_stdio_with_noisy_scraper(project):
    """Real server process: a scraper that prints to stdout must not break the protocol."""
    env = {
        "SIIMPLI_DB": os.environ["SIIMPLI_DB"],
        "PYTHONPATH": os.environ["PYTHONPATH"] + os.pathsep + str(REPO_ROOT),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(REPO_ROOT / "main.py"), "mcp", "--project-dir", str(project)],
        env=env,
    )

    stderr_path = project / "server_stderr.txt"

    async def main(errlog):
        async with Client(stdio_client(params, errlog=errlog)) as client:
            started = payload(await client.call_tool("run_job", {"job_name": "good_job"}))
            assert started["started"] is True
            run = None
            for _ in range(150):
                run = payload(await client.call_tool("get_run", {"run_id": started["run_id"]}))
                if run["status"] != "running":
                    break
                await asyncio.sleep(0.2)
            assert run["status"] == "success" and run["item_count"] == 3

            res = payload(await client.call_tool(
                "query_results", {"job_name": "good_job", "where": {"commodity": "zinc"}}))
            assert res["total"] == 1 and res["rows"][0]["price"] == 9

            status = payload(await client.call_tool("get_status", {"limit": 5}))
            assert status[0]["job_name"] == "good_job"

    with open(stderr_path, "w", encoding="utf-8") as errlog:
        asyncio.run(main(errlog))
    # The scraper's print() went to the server's stderr, i.e. off the protocol channel.
    # (mcp 2.x also diverts the child's inherited fd 0/1 itself, so this holds even without
    # our explicit child_io; test_serve_detaches_child_stdio checks our side of it.)
    assert "NOISE that must not reach" in stderr_path.read_text(encoding="utf-8", errors="replace")


def test_serve_detaches_child_stdio(project, monkeypatch):
    """serve() must give job processes no stdin and a stdout that isn't the protocol stream."""
    import subprocess
    import core.mcp_server as ms

    captured = {}

    class FakeServer:
        def run(self, transport):
            captured["transport"] = transport

    def fake_create(service):
        captured["service"] = service
        return FakeServer()

    monkeypatch.setattr(ms, "create_server", fake_create)
    monkeypatch.setattr(ms.sys, "stderr", type("E", (), {"fileno": lambda self: 2})())
    monkeypatch.chdir(project.parent)

    ms.serve(project_dir=str(project))

    assert captured["transport"] == "stdio"
    assert captured["service"].child_io == {"stdin": subprocess.DEVNULL, "stdout": 2}
    assert Path.cwd().resolve() == project.resolve()   # relative jobs/ and data paths now resolve there
