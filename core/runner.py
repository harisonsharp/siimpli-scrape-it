"""
Isolated script runner.
"""
from typing import Any, Dict, Optional
import importlib
import time
import logging
from core.exceptions import JobFailedError
from core.isolation import run_in_subprocess
from schemas.job_schema import JobConfig
from utils import db
from utils.notify import send_webhook
from utils.ratelimit import activate
from utils.storage import save_data

logger = logging.getLogger(__name__)


def _record(fn, *args, **kwargs):
    """Call a run-history function; a database problem must never fail the job."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.error(f"Run history unavailable ({fn.__name__}): {e}")
        return None


def _run_script(config: JobConfig, child_io: Optional[Dict[str, Any]] = None):
    """Return whatever the script's run() returns (request helpers honour rate_limit)."""
    if config.isolate:
        return run_in_subprocess(config, child_io)
    module = importlib.import_module(config.script)
    if not hasattr(module, "run"):
        raise AttributeError(f"Module {config.script} missing 'run' function")
    with activate(config.rate_limit):
        return module.run(params=config.parameters)


def _notify(config: JobConfig, status: str, run_id: Optional[int], runtime: float,
            item_count: Optional[int] = None, error: Optional[str] = None) -> None:
    """Send a webhook for failed / empty runs when the job asks for it."""
    n = config.notify
    if not n or not ((status == "failed" and n.on_failure) or (status == "empty" and n.on_empty)):
        return
    send_webhook(n.webhook_url, {
        "job_name": config.job_name,
        "status": status,
        "run_id": run_id,
        "duration_s": round(runtime, 2),
        "item_count": item_count,
        "error": error,
    })


def execute_job(config: JobConfig, run_id: Optional[int] = None,
                child_io: Optional[Dict[str, Any]] = None) -> bool:
    """Execute a scraper job specified by JobConfig.
    Returns True on success, False on failure.

    Each call is recorded in the run history (``utils.db``); scraped rows are
    stored in the results table and then exported to the configured file.

    ``run_id`` adopts a run row the caller already created (so it can report
    the id before the job finishes); ``child_io`` is forwarded to the worker
    process (see ``core.isolation.run_in_subprocess``).
    """
    max_attempts = max(1, config.retry.retries)
    attempt = 0
    job_start = time.perf_counter()
    if run_id is None:
        run_id = _record(db.start_run, config.job_name)
    last_error = ""

    while attempt < max_attempts:
        attempt += 1
        start_time = time.perf_counter()

        try:
            logger.info(f"Executing job: {config.job_name} (Attempt {attempt}/{max_attempts})")

            # 3-5. Execute the script's run(): in a child process with a timeout
            # by default, or in-process when the job sets isolate: false
            data = _run_script(config, child_io)

            # 6. Validate output
            if not isinstance(data, list):
                raise ValueError(f"Expected List[Dict] output, got {type(data)}")

            runtime = time.perf_counter() - start_time
            item_count = len(data)

            # 7. Store rows in the results database, then export the file
            export_rows = data
            if run_id is not None:
                db.store_results(config.job_name, run_id, data, config.output.key)
                if config.output.export == "all":
                    export_rows = db.load_results(config.job_name)
            elif config.output.export == "all":
                logger.warning("Results database unavailable; exporting this run's rows only")

            if save_data(export_rows, config.output):
                # 8. Log success
                status = "empty" if item_count == 0 else "success"
                total = time.perf_counter() - job_start
                if run_id is not None:
                    _record(db.finish_run, run_id, status, total, attempt, item_count)
                if status == "empty":
                    logger.warning(f"Job {config.job_name} returned 0 items")
                logger.info(f"Job {config.job_name} SUCCESS in {runtime:.2f}s, scraped {item_count} items")
                _notify(config, status, run_id, total, item_count)
                return True
            else:
                raise RuntimeError("Failed to save data")

        except Exception as e:
            runtime = time.perf_counter() - start_time
            # JobFailedError already carries the script's "Type: message"
            last_error = str(e) if isinstance(e, JobFailedError) else f"{type(e).__name__}: {e}"
            logger.error(f"Job {config.job_name} FAILED in {runtime:.2f}s: {str(e)}")

            if attempt < max_attempts:
                logger.info(f"Retrying in {config.retry.retry_delay}s...")
                time.sleep(config.retry.retry_delay)

    total = time.perf_counter() - job_start
    if run_id is not None:
        _record(db.finish_run, run_id, "failed", total, attempt, None, last_error)
    _notify(config, "failed", run_id, total, error=last_error)
    return False
