"""
Subprocess entry point that runs one scraper script in isolation.

Usage (internal, started by ``core.isolation``):
    python -m core.worker <job_config.json> <result.json>

The job config is read from the first file; the outcome is written to the
second as ``{"ok": true, "data": ...}`` or
``{"ok": false, "error": "...", "traceback": "..."}``.  Results travel through
a file rather than stdout so scripts remain free to print.
"""
import importlib
import json
import sys
import traceback
from pathlib import Path

from core.logger import configure_logging
from schemas.job_schema import JobConfig
from utils.ratelimit import activate


def main(argv: list) -> int:
    configure_logging()  # level inherited from the parent via SIIMPLI_LOG_LEVEL
    config_path, result_path = argv
    config = JobConfig.model_validate_json(Path(config_path).read_text(encoding="utf-8"))

    try:
        module = importlib.import_module(config.script)
        if not hasattr(module, "run"):
            raise AttributeError(f"Module {config.script} missing 'run' function")
        with activate(config.rate_limit):
            data = module.run(params=config.parameters)
        payload = {"ok": True, "data": data}
    except BaseException as e:  # includes SystemExit raised by a script
        payload = {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        }

    Path(result_path).write_text(json.dumps(payload, default=str), encoding="utf-8")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
