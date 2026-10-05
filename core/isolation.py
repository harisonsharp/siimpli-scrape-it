"""
Run a scraper script in a child process with a hard timeout.

A hung Playwright page or runaway script is killed (together with any browser
processes it spawned) instead of holding a scheduler worker forever.
"""
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from core.exceptions import JobFailedError
from schemas.job_schema import JobConfig

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child and everything it spawned (e.g. Chromium)."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except OSError:
        pass
    proc.wait()


def run_in_subprocess(config: JobConfig, child_io: Optional[Dict[str, Any]] = None) -> Any:
    """Run ``config.script`` in a child process and return what ``run()`` returned.

    ``child_io`` is passed to ``Popen`` as ``stdin``/``stdout``/``stderr``
    overrides, for hosts (like the MCP server) whose own stdio is a protocol
    channel the script must not touch.

    Raises JobFailedError if the script raises, times out
    (``config.timeout_seconds``), or the process dies without a result.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(_REPO_ROOT), env.get("PYTHONPATH")) if p
    )
    popen_kwargs: dict = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    with tempfile.TemporaryDirectory(prefix="siimpli_job_") as tmp:
        config_path = Path(tmp) / "job.json"
        result_path = Path(tmp) / "result.json"
        config_path.write_text(config.model_dump_json(), encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, "-m", "core.worker", str(config_path), str(result_path)],
            env=env,
            **popen_kwargs,
            **(child_io or {}),
        )
        try:
            try:
                proc.wait(timeout=config.timeout_seconds)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                raise JobFailedError(
                    f"Timed out after {config.timeout_seconds}s (process killed)"
                )
        finally:
            if proc.poll() is None:  # e.g. KeyboardInterrupt in the parent
                _kill_tree(proc)

        if not result_path.exists():
            raise JobFailedError(
                f"Worker process exited with code {proc.returncode} without a result"
            )
        payload = json.loads(result_path.read_text(encoding="utf-8"))

    if not payload["ok"]:
        logger.debug(payload.get("traceback", ""))
        raise JobFailedError(payload["error"])
    return payload["data"]
