"""
Declarative scraper: runs the step program in ``parameters`` (see ``core/steps.py``).

Parameters:
    steps: list of fetch / extract / for_each / emit steps (required)
    vars:  optional scalar variables available to templates, e.g. {"base": "https://..."}

Every request goes through ``core.safe_http`` (domain allowlist, public addresses
only, size cap) and the job's ``rate_limit`` block.
"""
from typing import Any, Dict, List, Optional

from core.steps import parse_program, run_program


def run(params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    return run_program(parse_program(params))
