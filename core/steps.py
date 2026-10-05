"""
Declarative scraping steps: the only way an agent may define a scraper.

A program is a list of steps run in order (YAML under ``parameters.steps``):

    - fetch:   {url, method, params, form, json, headers, parse, as}
    - extract: {from, rows, fields: {name: {select, attr, regex, type, value}}, as}
    - for_each: {in, as, steps: [...]}
    - emit:    {from, add: {field: template}}

``fetch`` downloads a page (through ``core.safe_http``) into the variable named
by ``as``. ``extract`` turns an HTML page (CSS selectors) or JSON document (dotted
paths such as ``data.items.*.price``) into a list of row dicts. ``for_each``
repeats nested steps for every item of a list. ``emit`` appends rows to the
job's result.

Templates are ``{name}`` / ``{item.key}`` lookups in the current variables; there
is no expression evaluation, so a program can't run code. Hard limits bound the
work a program can do (steps, fetches, loop size, rows).
"""
from __future__ import annotations

import json as jsonlib
import logging
import re
from typing import Any, Dict, List, Optional, Union

from bs4 import BeautifulSoup
from soupsieve import SelectorSyntaxError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core import safe_http

logger = logging.getLogger(__name__)

MAX_STEPS = 100
MAX_FETCHES = 200
MAX_LOOP_ITEMS = 1000
MAX_ROWS = 100_000
MAX_DEPTH = 3
MAX_REGEX_LENGTH = 200
MAX_FIELD_CHARS = 10_000

_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_TEMPLATE = re.compile(r"\{(" + _NAME + r"(?:\.[A-Za-z0-9_]+)*)\}")
_JUNK_IN_NUMBERS = re.compile(r"[,\s$€£¥ ]")


class StepError(Exception):
    """A step failed; the message says which one and why."""


# ---- models --------------------------------------------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class FetchSpec(_Strict):
    url: str
    method: str = "GET"
    params: Optional[Dict[str, str]] = None
    form: Optional[Dict[str, str]] = None
    json_body: Optional[Dict[str, Any]] = Field(default=None, alias="json")
    headers: Optional[Dict[str, str]] = None
    parse: str = "auto"
    as_: str = Field(alias="as")

    @field_validator("method")
    @classmethod
    def _method(cls, v):
        if v.upper() not in ("GET", "POST"):
            raise ValueError("method must be GET or POST")
        return v.upper()

    @field_validator("parse")
    @classmethod
    def _parse(cls, v):
        if v not in ("auto", "html", "json", "text"):
            raise ValueError("parse must be auto, html, json or text")
        return v

    @field_validator("as_")
    @classmethod
    def _as(cls, v):
        if not re.fullmatch(_NAME, v):
            raise ValueError("'as' must be a plain variable name")
        return v

    @field_validator("headers")
    @classmethod
    def _headers(cls, v):
        for name in v or {}:
            if name.lower() not in safe_http.ALLOWED_REQUEST_HEADERS:
                raise ValueError(
                    f"header {name!r} is not allowed (allowed: {', '.join(safe_http.ALLOWED_REQUEST_HEADERS)})"
                )
        return v

    @model_validator(mode="after")
    def _one_body(self):
        if self.form is not None and self.json_body is not None:
            raise ValueError("use either 'form' or 'json', not both")
        if self.method == "GET" and (self.form is not None or self.json_body is not None):
            raise ValueError("'form'/'json' need method: POST")
        return self


class FieldSpec(_Strict):
    select: Optional[str] = None       # CSS selector (html) or dotted path (json), relative to the row
    attr: str = "text"                 # html only: "text", or an attribute name such as "href"
    regex: Optional[str] = None        # keep the first group (or whole match) of this pattern
    type: str = "string"
    value: Optional[str] = None        # literal or template instead of selecting

    @field_validator("type")
    @classmethod
    def _type(cls, v):
        if v not in ("string", "number", "integer"):
            raise ValueError("type must be string, number or integer")
        return v

    @field_validator("regex")
    @classmethod
    def _regex(cls, v):
        if v is None:
            return v
        if len(v) > MAX_REGEX_LENGTH:
            raise ValueError(f"regex longer than {MAX_REGEX_LENGTH} characters")
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"invalid regex: {e}")
        return v

    @model_validator(mode="after")
    def _select_or_value(self):
        if self.value is not None and (self.select is not None or self.regex is not None):
            raise ValueError("'value' can't be combined with 'select' or 'regex'")
        return self


class ExtractSpec(_Strict):
    from_: str = Field(alias="from")
    rows: Optional[str] = None         # selector/path giving one element per row; omitted = one row
    fields: Dict[str, FieldSpec] = Field(min_length=1)
    as_: str = Field(alias="as")

    @field_validator("as_", "from_")
    @classmethod
    def _names(cls, v):
        if not re.fullmatch(_NAME, v):
            raise ValueError("must be a plain variable name")
        return v


class EmitSpec(_Strict):
    from_: str = Field(alias="from")
    add: Dict[str, str] = Field(default_factory=dict)


class ForEachSpec(_Strict):
    in_: Union[str, List[Any]] = Field(alias="in")
    as_: str = Field(default="item", alias="as")
    steps: List["Step"] = Field(min_length=1)

    @field_validator("as_")
    @classmethod
    def _as(cls, v):
        if not re.fullmatch(_NAME, v):
            raise ValueError("'as' must be a plain variable name")
        return v


class FetchStep(_Strict):
    fetch: FetchSpec


class ExtractStep(_Strict):
    extract: ExtractSpec


class EmitStep(_Strict):
    emit: EmitSpec


class ForEachStep(_Strict):
    for_each: ForEachSpec


Step = Union[FetchStep, ExtractStep, ForEachStep, EmitStep]
ForEachSpec.model_rebuild()
ForEachStep.model_rebuild()


def _count(steps: List[Any], depth: int = 1) -> int:
    if depth > MAX_DEPTH:
        raise ValueError(f"for_each nested deeper than {MAX_DEPTH} levels")
    total = 0
    for s in steps:
        total += 1
        if isinstance(s, ForEachStep):
            total += _count(s.for_each.steps, depth + 1)
    return total


class Program(_Strict):
    steps: List[Step] = Field(min_length=1)
    vars: Dict[str, Union[str, int, float, bool]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _limits(self):
        if _count(self.steps) > MAX_STEPS:
            raise ValueError(f"more than {MAX_STEPS} steps")
        for name in self.vars:
            if not re.fullmatch(_NAME, name):
                raise ValueError(f"variable name {name!r} is not valid")
        return self


def parse_program(params: Optional[Dict[str, Any]]) -> Program:
    """Validate a job's ``parameters`` as a program (raises pydantic ValidationError)."""
    return Program(**(params or {}))


def literal_urls(program: Program) -> List[str]:
    """Fetch URLs with no ``{template}`` in them (for static allowlist checks)."""
    found: List[str] = []

    def walk(steps):
        for s in steps:
            if isinstance(s, FetchStep) and not _TEMPLATE.search(s.fetch.url):
                found.append(s.fetch.url)
            elif isinstance(s, ForEachStep):
                walk(s.for_each.steps)

    walk(program.steps)
    return found


# ---- templates and paths -------------------------------------------------

def _lookup(path: str, scope: Dict[str, Any]) -> Any:
    head, *rest = path.split(".")
    if head not in scope:
        raise StepError(f"unknown variable {head!r} in {{{path}}}")
    value = scope[head]
    for token in rest:
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdigit() and int(token) < len(value):
            value = value[int(token)]
        else:
            raise StepError(f"{{{path}}}: no {token!r} in the value")
    return value


def render(template: str, scope: Dict[str, Any]) -> str:
    """Substitute ``{name.path}`` placeholders; values must be scalars."""
    def sub(match):
        value = _lookup(match.group(1), scope)
        if value is None or isinstance(value, (dict, list, BeautifulSoup)):
            raise StepError(f"{{{match.group(1)}}} is not a plain value")
        return str(value)
    return _TEMPLATE.sub(sub, template)


def _render_map(mapping: Optional[Dict[str, Any]], scope: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if mapping is None:
        return None
    def walk(v):
        if isinstance(v, str):
            return render(v, scope)
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v
    return walk(mapping)


def json_select(value: Any, path: str) -> List[Any]:
    """Follow a dotted path; ``*`` fans out over a list (or a dict's values).
    Returns every match (missing keys simply produce no match)."""
    current = [value]
    for token in [t for t in path.split(".") if t != ""]:
        nxt = []
        for item in current:
            if token == "*":
                if isinstance(item, list):
                    nxt.extend(item)
                elif isinstance(item, dict):
                    nxt.extend(item.values())
            elif isinstance(item, dict) and token in item:
                nxt.append(item[token])
            elif isinstance(item, list) and token.isdigit() and int(token) < len(item):
                nxt.append(item[int(token)])
        current = nxt
    return current


def parse_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return None
    cleaned = _JUNK_IN_NUMBERS.sub("", value)
    try:
        return float(cleaned)
    except ValueError:
        return None


def convert(value: Any, type_: str) -> Any:
    if value is None:
        return None
    if type_ == "string":
        return value.strip() if isinstance(value, str) else str(value)
    number = parse_number(value)
    if number is None:
        return None
    if type_ == "integer":
        return int(number) if float(number).is_integer() else None
    return int(number) if isinstance(number, int) else number


# ---- interpreter ---------------------------------------------------------

class _Run:
    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []
        self.fetches = 0


def _parse_body(response, mode: str) -> Any:
    if mode == "auto":
        ctype = response.headers.get("Content-Type", "").lower()
        text = response.text.lstrip()
        if "json" in ctype:
            mode = "json"
        elif "html" in ctype or text.startswith("<"):
            mode = "html"
        else:
            mode = "text"
    if mode == "json":
        try:
            return jsonlib.loads(response.text)
        except ValueError as e:
            raise StepError(f"response is not valid JSON: {e}")
    if mode == "html":
        return BeautifulSoup(response.text, "lxml")
    return response.text


def _do_fetch(spec: FetchSpec, scope: Dict[str, Any], run: _Run) -> None:
    run.fetches += 1
    if run.fetches > MAX_FETCHES:
        raise StepError(f"more than {MAX_FETCHES} fetches")
    url = render(spec.url, scope)
    try:
        response = safe_http.request(
            spec.method, url,
            params=_render_map(spec.params, scope),
            data=_render_map(spec.form, scope),
            json=_render_map(spec.json_body, scope),
            headers=_render_map(spec.headers, scope),
        )
    except safe_http.BlockedRequest as e:
        raise StepError(f"blocked: {e}")
    if response.status_code >= 400:
        raise StepError(f"{spec.method} {url} returned HTTP {response.status_code}")
    scope[spec.as_] = _parse_body(response, spec.parse)


def _field_value(spec: FieldSpec, element: Any, scope: Dict[str, Any]) -> Any:
    if spec.value is not None:
        return convert(render(spec.value, scope), spec.type)

    if isinstance(element, BeautifulSoup) or hasattr(element, "select_one"):
        try:
            target = element.select_one(spec.select) if spec.select else element
        except SelectorSyntaxError as e:
            raise StepError(f"bad CSS selector {spec.select!r}: {str(e).splitlines()[0]}")
        if target is None:
            return None
        raw = target.get_text(" ", strip=True) if spec.attr == "text" else target.get(spec.attr)
        if isinstance(raw, list):               # multi-valued attributes such as class
            raw = " ".join(raw)
    elif isinstance(element, str):
        raw = element
    else:
        matches = json_select(element, spec.select) if spec.select else [element]
        raw = matches[0] if matches else None
        if isinstance(raw, (dict, list)):
            raw = jsonlib.dumps(raw)

    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw[:MAX_FIELD_CHARS]
        if spec.regex:
            m = re.search(spec.regex, raw)
            if not m:
                return None
            raw = m.group(1) if m.groups() else m.group(0)
    return convert(raw, spec.type)


def _do_extract(spec: ExtractSpec, scope: Dict[str, Any]) -> None:
    if spec.from_ not in scope:
        raise StepError(f"unknown variable {spec.from_!r}")
    doc = scope[spec.from_]
    if hasattr(doc, "select"):
        try:
            elements = doc.select(spec.rows) if spec.rows else [doc]
        except SelectorSyntaxError as e:
            raise StepError(f"bad CSS selector {spec.rows!r}: {str(e).splitlines()[0]}")
    elif isinstance(doc, str):
        elements = [doc]
    else:
        elements = json_select(doc, spec.rows) if spec.rows else [doc]
    if len(elements) > MAX_ROWS:
        raise StepError(f"extract matched {len(elements)} rows (limit {MAX_ROWS})")
    scope[spec.as_] = [
        {name: _field_value(f, el, scope) for name, f in spec.fields.items()}
        for el in elements
    ]


def _do_emit(spec: EmitSpec, scope: Dict[str, Any], run: _Run) -> None:
    rows = scope.get(spec.from_)
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise StepError(f"emit: {spec.from_!r} is not a list of rows")
    extra = {k: render(v, scope) for k, v in spec.add.items()}
    if len(run.rows) + len(rows) > MAX_ROWS:
        raise StepError(f"more than {MAX_ROWS} rows emitted")
    run.rows.extend({**row, **extra} for row in rows)


def _exec(steps: List[Any], scope: Dict[str, Any], run: _Run) -> None:
    for number, step in enumerate(steps, 1):
        try:
            if isinstance(step, FetchStep):
                _do_fetch(step.fetch, scope, run)
            elif isinstance(step, ExtractStep):
                _do_extract(step.extract, scope)
            elif isinstance(step, EmitStep):
                _do_emit(step.emit, scope, run)
            else:
                spec = step.for_each
                items = spec.in_
                if isinstance(items, str):
                    if items not in scope:
                        raise StepError(f"unknown variable {items!r}")
                    items = scope[items]
                if not isinstance(items, list):
                    raise StepError("for_each 'in' must be a list")
                if len(items) > MAX_LOOP_ITEMS:
                    raise StepError(f"for_each over {len(items)} items (limit {MAX_LOOP_ITEMS})")
                for item in items:
                    _exec(spec.steps, {**scope, spec.as_: item}, run)
        except StepError as e:
            kind = type(step).__name__.replace("Step", "").lower()
            raise StepError(f"step {number} ({kind}): {e}") from None


def run_program(program: Program) -> List[Dict[str, Any]]:
    """Execute a validated program and return the emitted rows."""
    run = _Run()
    _exec(program.steps, dict(program.vars), run)
    logger.info("declarative program emitted %d row(s) from %d fetch(es)", len(run.rows), run.fetches)
    return run.rows
