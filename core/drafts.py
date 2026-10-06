"""
Agent-authored job drafts: propose, dry-run, and (humans only) approve.

An agent can only ever create *declarative* jobs (``scrapers.declarative``, see
``core/steps.py``), so it never supplies code. Drafts live in ``jobs/drafts/``
(which ``load_all_jobs`` never reads, so they are never scheduled or run by
``run --all``). Promoting a draft into ``jobs/`` is ``DraftStore.approve``,
which is exposed on the CLI only, never over MCP.

A dry run (``run_test``) goes through the real ``execute_job``: same retries,
subprocess isolation, timeout, rate limiter, robots check, validation and
database/export code. It differs from production only in where things are
written (``draft:<name>`` in the database, a scratch file under
``data/drafts/``) and in sending no webhook. Approval requires a successful
test of the *exact* content being approved.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import yaml
from pydantic import ValidationError

from core import safe_http
from core.config import load_all_jobs
from core.runner import execute_job
from core.steps import literal_urls, parse_program
from schemas.job_schema import JobConfig
from utils import db

logger = logging.getLogger(__name__)

SCRIPT = "scrapers.declarative"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_YAML_CHARS = 50_000
MAX_RATE_PER_MINUTE = 30
MAX_RETRIES = 3
MAX_TIMEOUT_SECONDS = 900
SAMPLE_ROWS = 20
AUDIT_FILE = Path("data") / "agent_audit.jsonl"


class DraftError(Exception):
    """A draft operation was refused; the message says why."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _audit(event: str, **fields: Any) -> None:
    """Append one line to the agent audit log (never fails the operation)."""
    try:
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": _now(), "event": event, **fields}) + "\n")
    except OSError as e:
        logger.warning("Could not write the agent audit log: %s", e)


def _no_anchors(text: str) -> None:
    """YAML anchors/aliases can expand a tiny file into a huge structure; refuse them."""
    for event in yaml.parse(text):
        if getattr(event, "anchor", None) or isinstance(event, yaml.AliasEvent):
            raise DraftError("YAML anchors and aliases are not allowed")


def _output_path_problem(path: str, output_type: str) -> Optional[str]:
    if PureWindowsPath(path).is_absolute() or PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive:
        return "output.path must be relative"
    parts = PurePosixPath(path.replace("\\", "/")).parts
    if not parts or parts[0] != "data" or ".." in parts or len(parts) < 2:
        return "output.path must be a file under data/"
    if parts[1] == "drafts":
        return "output.path must not be under data/drafts/ (reserved for dry runs)"
    if not path.lower().endswith("." + output_type):
        return f"output.path must end in .{output_type}"
    return None


def check_job(config: JobConfig, production_jobs: List[JobConfig],
              allowlist: Optional[List[str]] = None) -> List[str]:
    """Every rule an agent-written job must satisfy; returns the problems found."""
    problems: List[str] = []
    if not NAME_RE.match(config.job_name):
        problems.append("job_name must be lowercase letters, digits and underscores, starting with a letter (max 64)")
    if any(j.job_name == config.job_name for j in production_jobs):
        problems.append(f"job_name {config.job_name!r} is already a live job; pick another name")
    if config.script != SCRIPT:
        problems.append(f"script must be {SCRIPT!r} (agents may not supply code)")
    if config.notify is not None:
        problems.append("notify is not allowed on agent-written jobs")
    if not config.isolate:
        problems.append("isolate: false is not allowed")
    if config.timeout_seconds is None or config.timeout_seconds > MAX_TIMEOUT_SECONDS:
        problems.append(f"timeout_seconds must be set and at most {MAX_TIMEOUT_SECONDS}")
    if config.retry.retries > MAX_RETRIES:
        problems.append(f"retry.retries must be at most {MAX_RETRIES}")

    v = config.validation
    if v is None or not v.min_rows or not v.required_fields:
        problems.append("validation is required: set min_rows >= 1 and at least one required_fields entry")
    rl = config.rate_limit
    if rl is None:
        problems.append("rate_limit is required")
    else:
        if rl.rate_limit > MAX_RATE_PER_MINUTE:
            problems.append(f"rate_limit.rate_limit must be at most {MAX_RATE_PER_MINUTE} requests per minute")
        if not rl.respect_robots:
            problems.append("rate_limit.respect_robots must be true")

    why = _output_path_problem(config.output.path, config.output.type)
    if why:
        problems.append(why)
    elif any(Path(j.output.path) == Path(config.output.path) for j in production_jobs):
        problems.append("output.path is already used by a live job")

    try:
        program = parse_program(config.parameters)
    except ValidationError as e:
        problems.extend(f"parameters.{_loc(err)}: {err['msg']}" for err in e.errors())
    else:
        allow = safe_http.load_allowlist() if allowlist is None else allowlist
        for url in literal_urls(program):
            host = (urlsplit(url).hostname or "")
            if not safe_http.host_allowed(host, allow):
                problems.append(
                    f"{host or url!r} is not on the domain allowlist ({safe_http.DEFAULT_ALLOWLIST}); "
                    "a human must add it before this job can run"
                )
    return problems


def _loc(err: Dict[str, Any]) -> str:
    return ".".join(str(p) for p in err["loc"])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DraftStore:
    def __init__(self, jobs_dir: str = "jobs", child_io: Optional[Dict[str, Any]] = None):
        self.jobs_dir = Path(jobs_dir)
        self.drafts_dir = self.jobs_dir / "drafts"
        self.child_io = child_io
        self._testing: Dict[str, int] = {}
        self._lock = threading.Lock()

    # ---- paths ----

    def _name(self, name: str) -> str:
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise DraftError("invalid draft name")
        return name

    def _yaml_path(self, name: str) -> Path:
        return self.drafts_dir / f"{self._name(name)}.yaml"

    def _meta_path(self, name: str) -> Path:
        return self.drafts_dir / f"{self._name(name)}.meta.json"

    def _read_meta(self, name: str) -> Dict[str, Any]:
        try:
            return json.loads(self._meta_path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_meta(self, name: str, meta: Dict[str, Any]) -> None:
        self._meta_path(name).write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def _load(self, name: str) -> "tuple[str, JobConfig]":
        path = self._yaml_path(name)
        if not path.exists():
            raise DraftError(f"no draft named {name!r}")
        text = path.read_text(encoding="utf-8")
        return text, JobConfig(**yaml.safe_load(text))

    # ---- agent-facing operations ----

    def propose(self, yaml_text: str, author: str = "agent") -> Dict[str, Any]:
        """Validate and save a draft. Returns ``{"ok": False, "problems": [...]}``
        (nothing saved) or ``{"ok": True, "name": ..., "next": ...}``."""
        def refuse(problems: List[str]) -> Dict[str, Any]:
            _audit("propose_rejected", author=author, problems=problems, sha=_sha(yaml_text or ""))
            return {"ok": False, "problems": problems}

        if not isinstance(yaml_text, str) or not yaml_text.strip():
            return refuse(["yaml_text is empty"])
        if len(yaml_text) > MAX_YAML_CHARS:
            return refuse([f"yaml_text is longer than {MAX_YAML_CHARS} characters"])
        try:
            _no_anchors(yaml_text)
            data = yaml.safe_load(yaml_text)
        except DraftError as e:
            return refuse([str(e)])
        except yaml.YAMLError as e:
            return refuse([f"invalid YAML: {str(e).splitlines()[0]}"])
        if not isinstance(data, dict):
            return refuse(["the YAML must be a mapping with job_name, script, schedule, output, ..."])
        try:
            config = JobConfig(**data)
        except ValidationError as e:
            return refuse([f"{_loc(err)}: {err['msg']}" for err in e.errors()])

        problems = check_job(config, load_all_jobs(self.jobs_dir))
        if problems:
            return refuse(problems)

        # Save a normalised dump of the validated model, not the agent's raw text,
        # so only fields we understand ever reach disk.
        canonical = yaml.safe_dump(config.model_dump(mode="json", exclude_none=True), sort_keys=False)
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        self._yaml_path(config.job_name).write_text(canonical, encoding="utf-8")
        self._write_meta(config.job_name, {"author": author, "created_at": _now(), "sha": _sha(canonical)})
        _audit("propose", author=author, name=config.job_name, sha=_sha(canonical))
        return {"ok": True, "name": config.job_name, "sha": _sha(canonical),
                "next": f"call test_job({config.job_name!r}), then ask a human to run: "
                        f"siimpli_scrape_it draft approve {config.job_name}"}

    def list(self) -> List[Dict[str, Any]]:
        out = []
        for path in sorted(self.drafts_dir.glob("*.yaml")) if self.drafts_dir.exists() else []:
            name = path.stem
            meta = self._read_meta(name)
            out.append({"name": name, "created_at": meta.get("created_at"),
                        "author": meta.get("author"), "last_test": meta.get("last_test")})
        return out

    def get(self, name: str) -> Dict[str, Any]:
        text, config = self._load(name)
        meta = self._read_meta(name)
        total, rows = db.query_results(self._ns(name), SAMPLE_ROWS, 0, None)
        return {"name": name, "yaml": text, "created_at": meta.get("created_at"),
                "last_test": meta.get("last_test"), "tested_current_version": self._tested(name, text),
                "result_rows": total, "sample": rows}

    def delete(self, name: str) -> Dict[str, Any]:
        self._yaml_path(name)  # validates the name
        removed = False
        for path in (self._yaml_path(name), self._meta_path(name)):
            if path.exists():
                path.unlink()
                removed = True
        db.delete_results(self._ns(name))
        _audit("delete_draft", name=name)
        return {"deleted": removed}

    # ---- dry runs ----

    @staticmethod
    def _ns(name: str) -> str:
        return f"draft:{name}"

    def _test_config(self, config: JobConfig) -> JobConfig:
        """The production config, redirected to the draft namespace and a scratch file."""
        out = config.output.model_copy(update={
            "path": f"data/drafts/{config.job_name}/out.{config.output.type}"})
        return config.model_copy(update={
            "job_name": self._ns(config.job_name), "output": out, "notify": None, "isolate": True})

    def _tested(self, name: str, text: str) -> bool:
        last = self._read_meta(name).get("last_test") or {}
        return last.get("status") == "success" and last.get("sha") == _sha(text)

    def start_test(self, name: str) -> Dict[str, Any]:
        """Start a dry run in the background; returns the run_id to poll with get_run."""
        text, config = self._load(name)
        problems = check_job(config, load_all_jobs(self.jobs_dir))
        if problems:
            raise DraftError("draft no longer passes the checks: " + "; ".join(problems))
        with self._lock:
            if name in self._testing:
                return {"started": False, "reason": "already_running", "run_id": self._testing[name]}
            run_id = db.start_run(self._ns(name))
            self._testing[name] = run_id
        threading.Thread(target=self._test_thread, args=(name, run_id), name=f"draft-{name}", daemon=True).start()
        return {"started": True, "run_id": run_id, "status": "running",
                "next": f"call get_run({run_id}); when finished, get_draft({name!r}) shows sample rows"}

    def _test_thread(self, name: str, run_id: int) -> None:
        try:
            self.run_test(name, run_id=run_id)
        except Exception as e:
            logger.exception("Draft test crashed: %s", name)
            db.finish_run(run_id, "failed", 0.0, 0, error=f"{type(e).__name__}: {e}")
        finally:
            with self._lock:
                self._testing.pop(name, None)

    def run_test(self, name: str, run_id: Optional[int] = None) -> Dict[str, Any]:
        """Dry-run a draft synchronously through the production runner."""
        text, config = self._load(name)
        ns = self._ns(name)
        db.delete_results(ns)          # always a clean first run, like production's
        if run_id is None:
            run_id = db.start_run(ns)
        _audit("test_start", name=name, sha=_sha(text), run_id=run_id)
        execute_job(self._test_config(config), run_id=run_id, child_io=self.child_io)
        run = db.get_run(run_id) or {}
        result = {"status": run.get("status"), "run_id": run_id, "item_count": run.get("item_count"),
                  "error": run.get("error"), "sha": _sha(text), "at": _now()}
        meta = self._read_meta(name)
        meta["last_test"] = result
        self._write_meta(name, meta)
        _audit("test_end", name=name, **{k: result[k] for k in ("status", "run_id", "item_count", "error", "sha")})
        return result

    # ---- human-only ----

    def approve(self, name: str) -> Path:
        """Promote a tested draft into ``jobs/``. CLI only; never wired to MCP."""
        text, config = self._load(name)
        problems = check_job(config, load_all_jobs(self.jobs_dir))
        if problems:
            raise DraftError("draft fails the checks: " + "; ".join(problems))
        if not self._tested(name, text):
            raise DraftError("this exact version has no successful dry run; run `draft test` first")
        target = self.jobs_dir / f"{name}.yaml"
        try:
            with target.open("x", encoding="utf-8") as f:       # never overwrite a live job
                f.write(text)
        except FileExistsError:
            raise DraftError(f"{target} already exists")
        self._yaml_path(name).unlink()
        self._meta_path(name).unlink(missing_ok=True)
        db.delete_results(self._ns(name))
        _audit("approve", name=name, sha=_sha(text))
        return target
