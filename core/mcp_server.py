"""
MCP server: lets an agent list jobs, trigger runs, and read run history and
scraped results.  Start it with ``siimpli_scrape_it mcp``.

Scope is deliberately narrow: the agent can only run jobs that already exist
as YAML files in ``jobs/`` and read what they stored.  It cannot create or edit
jobs or scripts, and it cannot run arbitrary code or SQL.

Runs started here are always isolated (child process + ``timeout_seconds``),
and the child's stdin/stdout are detached from the server's own, because over
stdio those streams are the protocol channel.
"""
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import load_all_jobs
from core.logger import configure_logging
from core.runner import execute_job
from schemas.job_schema import JobConfig
from utils import db

logger = logging.getLogger(__name__)

MAX_ROWS = 1000
MAX_RUNS = 100

INSTRUCTIONS = (
    "Tools for the siimpli_scrape_it scraping daemon. list_jobs shows what can be run. "
    "run_job starts a job in the background and returns a run_id immediately; poll "
    "get_run(run_id) until status is success, empty or failed (failed runs carry the "
    "error). query_results reads the rows a job has stored. get_status lists recent runs."
)


class JobService:
    """The operations the MCP tools expose, independent of the MCP SDK."""

    def __init__(self, jobs_dir: str = "jobs", child_io: Optional[Dict[str, Any]] = None):
        self.jobs_dir = jobs_dir
        self.child_io = child_io
        self._running: Dict[str, int] = {}   # job_name -> run_id, for jobs started by this server
        self._lock = threading.Lock()

    def _jobs(self) -> Dict[str, JobConfig]:
        return {j.job_name: j for j in load_all_jobs(self.jobs_dir)}

    def list_jobs(self) -> List[Dict[str, Any]]:
        out = []
        for job in self._jobs().values():
            last = db.recent_runs(job.job_name, 1)
            out.append({
                "job_name": job.job_name,
                "script": job.script,
                "schedule": job.schedule,
                "output": job.output.model_dump(),
                "timeout_seconds": job.timeout_seconds,
                "rate_limit": job.rate_limit.model_dump() if job.rate_limit else None,
                "validation": job.validation.model_dump() if job.validation else None,
                "last_run": ({k: last[0][k] for k in ("id", "status", "started_at", "item_count", "error")}
                             if last else None),
            })
        return out

    def get_status(self, job_name: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        return db.recent_runs(job_name, max(1, min(limit, MAX_RUNS)))

    def get_run(self, run_id: int) -> Dict[str, Any]:
        run = db.get_run(run_id)
        if run is None:
            raise ValueError(f"No run with id {run_id}")
        return run

    def query_results(self, job_name: str, limit: int = 100, offset: int = 0,
                      where: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        limit = max(1, min(limit, MAX_ROWS))
        total, rows = db.query_results(job_name, limit, offset, where)
        return {"job_name": job_name, "total": total, "offset": offset,
                "returned": len(rows), "rows": rows}

    def run_job(self, job_name: str) -> Dict[str, Any]:
        jobs = self._jobs()
        if job_name not in jobs:
            raise ValueError(f"Unknown job {job_name!r}. Available: {sorted(jobs)}")

        with self._lock:
            if job_name in self._running:
                return {"started": False, "reason": "already_running",
                        "run_id": self._running[job_name]}
            run_id = db.start_run(job_name)
            self._running[job_name] = run_id

        # Always isolated: gives the run its timeout and keeps its output off our stdio
        config = jobs[job_name].model_copy(update={"isolate": True})
        threading.Thread(target=self._run, args=(config, run_id),
                         name=f"job-{job_name}", daemon=True).start()
        return {"started": True, "run_id": run_id, "status": "running",
                "next": f"call get_run({run_id}) to check progress"}

    def _run(self, config: JobConfig, run_id: int) -> None:
        try:
            execute_job(config, run_id=run_id, child_io=self.child_io)
        except Exception as e:  # execute_job reports failures itself; this is a backstop
            logger.exception("Unexpected error running %s", config.job_name)
            db.finish_run(run_id, "failed", 0.0, 0, error=f"{type(e).__name__}: {e}")
        finally:
            with self._lock:
                self._running.pop(config.job_name, None)


def create_server(service: Optional[JobService] = None):
    """Build the MCP server around a JobService (imports the MCP SDK lazily)."""
    from mcp.server.mcpserver import MCPServer

    service = service or JobService()
    server = MCPServer("siimpli-scrape-it", instructions=INSTRUCTIONS)

    @server.tool()
    def list_jobs() -> List[Dict[str, Any]]:
        """List configured scraper jobs with their schedule, output settings and last run."""
        return service.list_jobs()

    @server.tool()
    def get_status(job_name: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """Recent runs, newest first (optionally for one job). Each has status
        (running/success/empty/failed), duration_s, attempts, item_count and error."""
        return service.get_status(job_name, limit)

    @server.tool()
    def get_run(run_id: int) -> Dict[str, Any]:
        """Status of one run, e.g. the run_id returned by run_job. Poll until status is not 'running'."""
        return service.get_run(run_id)

    @server.tool()
    def query_results(job_name: str, limit: int = 100, offset: int = 0,
                      where: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Rows a job has stored, oldest first, paged with limit (max 1000) / offset.
        `where` filters by exact field value, e.g. {"commodity": "copper"}."""
        return service.query_results(job_name, limit, offset, where)

    @server.tool()
    def run_job(job_name: str) -> Dict[str, Any]:
        """Start a configured job now. Returns immediately with a run_id (the job runs in the
        background and may take minutes); check it with get_run. Hits real websites, subject to
        the job's rate limit and timeout. Returns started=false if this job is already running."""
        return service.run_job(job_name)

    return server


def serve(project_dir: Optional[str] = None, jobs_dir: str = "jobs") -> None:
    """Run the MCP server over stdio until the client disconnects."""
    configure_logging(stream=sys.stderr)
    if project_dir:
        os.chdir(project_dir)  # jobs/ and output paths are relative to the project

    child_io: Dict[str, Any] = {"stdin": subprocess.DEVNULL}
    try:
        child_io["stdout"] = sys.stderr.fileno()   # script prints go to stderr, never the protocol stream
    except (AttributeError, OSError, ValueError):
        child_io["stdout"] = subprocess.DEVNULL
    create_server(JobService(jobs_dir, child_io)).run("stdio")
