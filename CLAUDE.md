# CLAUDE.md

`siimpli_scrape_it` is a Python (>=3.9) scraping daemon and CLI. Jobs are defined as YAML files in `jobs/`, each pointing at a scraper module in `scrapers/`. Jobs run on demand or on a cron schedule, and results are written to CSV or JSON. See `docs/user_guide.md` for the full user-facing guide.

## Commands

```bash
pip install -e ".[dev]"        # install with test deps
playwright install chromium    # only needed for browser-based scrapers
siimpli_scrape_it -v <command>         # debug-level logging (or SIIMPLI_LOG_LEVEL=DEBUG)
pytest                         # run tests (from repo root; tests use relative paths)

siimpli_scrape_it list                 # show configured jobs
siimpli_scrape_it run <job_name>       # run one job now
siimpli_scrape_it run --all            # run every job in jobs/
siimpli_scrape_it status [job] [-n N]  # recent run history
siimpli_scrape_it purge <job> [-y]     # delete a job's stored results (after changing output.key / row shape)
siimpli_scrape_it scheduler            # start APScheduler daemon (Ctrl+C to stop)
siimpli_scrape_it mcp -C <project_dir>   # MCP server over stdio (needs: pip install -e ".[mcp]")
siimpli_scrape_it draft list|show|test|approve|reject <name>   # review agent-written jobs (human-only)
```

Run everything from the repo root: `main.py` looks for `./jobs` and output paths are relative to the working directory.

## Architecture

- `main.py` — Typer CLI (`list`, `run`, `status`, `scheduler`). Loads every `jobs/*.yaml` via `core.config.load_config`; invalid YAML is logged and skipped (returns `None`).
- `schemas/job_schema.py` — Pydantic models: `JobConfig` (`job_name`, `script`, `schedule`, `output`, `retry`, `rate_limit`, `notify`, `parameters`), `OutputConfig` (`csv`|`json` + `path`, optional `key`, `export`), `RetryConfig`, `RateLimitConfig`, `NotifyConfig`.
- `core/runner.py` — `execute_job(config)`: runs the script's `run(params=config.parameters)` via `_run_script` (child process by default, see `core/isolation.py` / `core/worker.py`), requires a `List[Dict]` return, then `utils.storage.save_data`. Retries up to `retry.retries` attempts (minimum 1) with `retry_delay` seconds between. Returns `True`/`False`; it does not raise. Each call is recorded in `runs` (status `success`/`empty`/`failed`), rows go to the `results` table, and the output file is then exported from them. Run-history DB errors are logged and never fail the job. Optional `notify.webhook_url` gets a POST on failed/empty runs.
- `core/validation.py` — `validate_rows` / `format_problems`; the runner applies a job's optional `validation` block (`min_rows`, `required_fields`, `types`) right after the list check and **before** anything is stored or exported, raising `JobFailedError` so the last good output survives. Rows must always be dicts.
- `core/logger.py` — `configure_logging()` (stderr, level from `-v` / `SIIMPLI_LOG_LEVEL`, exported to the env so job worker processes log at the same level). Called by the CLI callback, `core/worker.py` and the MCP server; libraries just use `logging.getLogger(__name__)`.
- `core/isolation.py` / `core/worker.py` — each attempt runs `python -m core.worker` in a child process (config in, result out via temp JSON files). `timeout_seconds` (default 3600; `None` = unlimited) kills the whole process tree, including Chromium. `isolate: false` in the job runs in-process instead (no timeout; use for debuggers). Child needs importable scripts: the repo root is added to its `PYTHONPATH`; rows must be JSON-serializable (others are stringified).
- `core/mcp_server.py` — `JobService` (list_jobs, run_job, get_run, get_status, query_results; SDK-independent) wrapped by `create_server()` using the MCP **2.x** SDK (`mcp.server.mcpserver.MCPServer`, not the 1.x `FastMCP`). Read-only except `run_job` and the draft tools (`propose_job`, `list_drafts`, `get_draft`, `test_job`, `delete_draft`), which creates a run row up front, executes in a background thread via `execute_job(config, run_id=..., child_io=...)` and always forces `isolate=True`. Live jobs/scrapers are never created or edited through it; agents only write drafts. `main.py mcp` imports it lazily so the base install works without the SDK.
- `utils/db.py` — SQLite store (`data/siimpli.db`, override with `SIIMPLI_DB`): `runs` history and `results` rows. `output.key` upserts on those fields; `output.export: all` writes the file from every stored row (default `latest` = this run's rows).
- `core/scheduler.py` — `ScraperScheduler` wraps APScheduler `BackgroundScheduler` with a SQLAlchemy job store (`sqlite:///jobs.sqlite` in the cwd), 4 worker threads, `max_instances=1`. Schedule strings: `hourly`, `daily`, or a 5-field cron expression.
- `utils/` — `http.get_with_retries` (throttled + 429/503 backoff via `ratelimit` when the job has `rate_limit`), `ratelimit` (per-domain shared limiter, `respect_robots`), `notify.send_webhook`, `browser.get_browser_page` / `captcha_gate` (Playwright), `downloader.FileDownloader` (rate-limited PDF/XLSX downloads), `storage.save_data`.
- `core/steps.py` / `core/safe_http.py` / `scrapers/declarative.py` — declarative scrapers (the only kind agents will be allowed to author). `scrapers.declarative.run(params)` validates `parameters` as a `Program` (`steps`: `fetch` / `extract` / `for_each` / `emit`, optional `vars`) and interprets it; templates are plain `{a.b}` lookups, no eval. All HTTP goes through `safe_http.request`: default-deny domain allowlist (`allowed_domains.txt` in the cwd, or `SIIMPLI_ALLOWED_DOMAINS_FILE`), public IPs only (checked again on every redirect hop), ports 80/443, 5 MB cap, `trust_env=False`, shared rate limiter. Unpatched, loopback test servers are refused, so tests patch `safe_http._ip_blocked` / `ALLOWED_PORTS` (see `tests/test_steps.py`) and subprocess tests see the real rules. Known gap: DNS-rebinding TOCTOU between check and connect.
- `core/drafts.py` — agent-written jobs. `DraftStore.propose` validates (`check_job`: declarative script only, `validation`/`rate_limit`+`respect_robots`/`timeout_seconds<=900` required, no notify, no `isolate: false`, output under `data/`, literal URLs on the allowlist, YAML anchors refused) and saves a normalised dump to `jobs/drafts/` (`load_all_jobs` never reads it, so drafts are never scheduled) plus a `.meta.json` with the content hash. `run_test` pushes the draft through the real `execute_job` as `draft:<name>` (scratch output under `data/drafts/`, no webhook, clean slate each time; `_test_config` is the only place it differs from production, and a test pins that). `approve` needs a passing test of the exact hash, re-runs the checks, and refuses to overwrite a live job. **Approve/promote must never be exposed over MCP** (`tests/test_mcp.py` asserts the tool list). Agent actions are logged to `data/agent_audit.jsonl`.
- `scrapers/scrap_monster_prices.py` — plain-HTTP scraper: GET the commodity page, read its region tabs (EXW China, FOB China, ...) and each tab's unit, then POST `/metalprice/chartdata` with `range=0` per region for the full history. Emits tidy rows `{commodity, region, unit, date, price}` (job key `[commodity, region, date]`); the old `drawchart`/`getchart` and browser-clicking paths are gone. `browser: true` fetches via a visible `captcha_gate` session instead. Units differ per region (MT/Kg/Lb).
- `scrapers/` — one module per scraper, each exposing `run(params: dict | None) -> List[Dict[str, Any]]`.

## Adding a scraper

1. Create `scrapers/<name>.py` with `run(params=None) -> List[Dict[str, Any]]`.
2. Create `jobs/<name>.yaml` with `script: scrapers.<name>` (dotted module path, not a file path). Copy `jobs/first_job.yaml` as a template.
3. Add a test in `tests/`.

Conventions the existing code follows: module docstring describing the scraper and its `parameters`, `logging.getLogger(__name__)` for logs, type hints, and scraper-specific settings read from `params` with defaults.

## Gotchas and known gaps

- `schemas/output_schema.py` is an empty stub; don't rely on it.
- A job's `rate_limit` block is enforced per *domain* and shared across all jobs/processes (reservations in the `rate_hits` / `domain_backoff` tables, see `utils/ratelimit.py`). It covers `get_with_retries`, Playwright main-frame navigations and `FileDownloader`; code using `requests` directly must call `throttle(url)` / `penalize(...)` itself (ScrapMonster does). The limiter lives in a `ContextVar`, which Playwright's sync handlers (separate greenlet) can't see, so `utils/browser.py` captures it at attach time.
- `captcha_gate` opens a visible (non-headless) browser and can block up to 5 minutes waiting for a human. `docs/user_guide.md` says the runner wires it in automatically; it does not. Scrapers opt in by calling it, and such jobs aren't suitable for unattended scheduler runs.
- CSV headers are the union of all rows' keys. An empty result writes an empty file and is recorded as status `empty`.
- With `output.key` set, a row missing a key field fails the run (e.g. scrapers that return `{"status": "error"}` rows instead of raising); the shipped jobs also set `validation.required_fields` so such marker rows fail the run explicitly.
- Tests get an isolated DB via the autouse fixture in `tests/conftest.py`; for in-process fakes register them in `sys.modules` and set `isolate: False` (`tests/test_run_history.py`); for real subprocess runs write modules to a tmp dir on `PYTHONPATH` (`tests/test_isolation.py`). Either way, don't write into `scrapers/`.
- Each run row records the executing process's `pid`; `recent_runs`/`get_run` first mark `running` rows whose process is gone as `failed` ("Abandoned: ..."). Rows from before pid tracking (NULL pid) are never reaped.
- `mcp` extra requires Python >= 3.10 while the package declares >= 3.9; the MCP tests `importorskip("mcp")`.
- `tests/test_runner.py` writes a dummy scraper into the real `scrapers/` directory; clean up if a test run is interrupted.
- `pyproject.toml` references `README.md`, which doesn't exist. `main` is installed as a standalone `py-module`; packages are `core`, `schemas`, `utils`, `scrapers`.
- Gitignored: `data/` (scraped output and `siimpli.db`), `jobs.sqlite` (scheduler job store, created in the cwd), `debug_*.html`, `pytest_*out.txt`, `.env*`, `.claude/settings.local.json`.
