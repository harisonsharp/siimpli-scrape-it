"""
Output validation: check a scraper's rows against a job's expectations
before anything is stored or exported.

A scraper that "succeeds" with junk (a site changed, a selector now matches
nothing, prices became strings) fails the run instead, so the last good
output file and the stored history are never overwritten with bad data.
"""
from typing import Any, Dict, List, Optional

from schemas.job_schema import ValidationConfig

MAX_PROBLEMS_SHOWN = 5

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def validate_rows(rows: List[Any], config: Optional[ValidationConfig]) -> List[str]:
    """Return a list of human-readable problems (empty if the rows are valid).

    Always checks that every row is a dict.  With a ``ValidationConfig`` it also
    checks ``min_rows``, ``required_fields`` (present and not null/blank) and
    ``types`` (for fields that are present and not null).
    """
    bad = [i for i, row in enumerate(rows) if not isinstance(row, dict)]
    if bad:
        return [f"{len(bad)} row(s) are not dicts (first at row {bad[0]}: {type(rows[bad[0]]).__name__})"]
    if config is None:
        return []

    problems: List[str] = []

    if config.min_rows is not None and len(rows) < config.min_rows:
        problems.append(f"expected at least {config.min_rows} row(s), got {len(rows)}")

    for field in config.required_fields:
        missing = [i for i, row in enumerate(rows) if _missing(row.get(field))]
        if missing:
            problems.append(
                f"required field {field!r} is missing or empty in {len(missing)} of "
                f"{len(rows)} row(s) (first at row {missing[0]})"
            )

    for field, expected in config.types.items():
        check = _TYPE_CHECKS[expected]
        wrong = [i for i, row in enumerate(rows)
                 if row.get(field) is not None and not check(row[field])]
        if wrong:
            sample = rows[wrong[0]][field]
            problems.append(
                f"field {field!r} should be {expected} but {len(wrong)} row(s) differ "
                f"(first at row {wrong[0]}: {type(sample).__name__} {sample!r})"
            )

    return problems


def format_problems(problems: List[str]) -> str:
    """One-line summary suitable for a run's error field."""
    shown = "; ".join(problems[:MAX_PROBLEMS_SHOWN])
    extra = len(problems) - MAX_PROBLEMS_SHOWN
    return f"Output validation failed: {shown}" + (f" (+{extra} more)" if extra > 0 else "")
