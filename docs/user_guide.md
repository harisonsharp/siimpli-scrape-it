# siimpli_scrape_it — User Guide

`siimpli_scrape_it` is a Python-based web scraping daemon. You define scraper jobs in YAML files, point each job at a Python script that extracts data, and let the tool run them on a schedule — or trigger them manually from the command line. Results are written to CSV or JSON files automatically.

---

## Table of Contents

1. [Installation](#1-installation)
2. [Directory Structure](#2-directory-structure)
3. [Defining a Job](#3-defining-a-job)
4. [Writing a Scraper Script](#4-writing-a-scraper-script)
5. [CLI Commands](#5-cli-commands)
6. [Running the Scheduler Daemon](#6-running-the-scheduler-daemon)
7. [Output Files](#7-output-files)
8. [Retries and Rate Limiting](#8-retries-and-rate-limiting)
9. [Utility Helpers](#9-utility-helpers)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Installation

**Prerequisites:** Python 3.9 or later.

```bash
# Clone or copy the project, then install it in editable mode:
pip install -e .

# Install dev dependencies (only needed for running tests):
pip install -e ".[dev]"

# If any of your scraper scripts use browser automation:
playwright install chromium
```

After installation, the `siimpli_scrape_it` command is available on your PATH.

---

## 2. Directory Structure

```
siimpli_scrape_it/
├── scrapers/       ← Your scraper scripts live here
├── jobs/           ← One YAML file per job
├── core/           ← Internal engine (do not edit)
├── utils/          ← Shared helpers (HTTP, browser, storage)
├── schemas/        ← Pydantic data contracts (do not edit)
├── main.py         ← CLI entrypoint
└── pyproject.toml
```

You only need to work in **`scrapers/`** and **`jobs/`**.

---

## 3. Defining a Job

Create a `.yaml` file inside the `jobs/` directory. Each file defines one scraping job.

### Minimal Example

```yaml
# jobs/metal_prices.yaml
job_name: metal_prices
script: scrapers.metal_prices
schedule: daily
output:
  type: csv
  path: data/metal_prices.csv
```

### Full Example (all options)

```yaml
job_name: metal_prices
script: scrapers.metal_prices
schedule: "0 3 * * 1-5"   # 03:00 on weekdays (cron syntax)
output:
  type: json
  path: data/metal_prices.json
retry:
  retries: 3              # Number of attempts before giving up (default: 3)
  retry_delay: 60         # Seconds to wait between attempts (default: 60)
rate_limit:
  rate_limit: 10          # Max requests per minute (default: 10)
  delay_min: 2            # Min random delay between requests in seconds
  delay_max: 5            # Max random delay between requests in seconds
parameters:
  region: "us-west"       # Arbitrary key-value pairs passed to your script
  max_pages: 5
```

### Schedule Formats

| Value | Meaning |
|---|---|
| `hourly` | Once per hour, at :00 |
| `daily` | Once per day, at midnight |
| `"0 3 * * *"` | Standard 5-part cron expression |
| `"30 8 * * 1-5"` | 08:30 on Monday–Friday |

### Required Fields

| Field | Type | Description |
|---|---|---|
| `job_name` | string | Unique identifier for the job |
| `script` | string | Python module path to your scraper (dot-separated) |
| `schedule` | string | When to run (see table above) |
| `output.type` | `csv` or `json` | Output format |
| `output.path` | string | File path where results are written |

---

## 4. Writing a Scraper Script

Place your scraper file inside the `scrapers/` directory. The filename determines the module path used in the job YAML (`scrapers.my_script` → `scrapers/my_script.py`).

**Every scraper must define a `run()` function** with this exact signature:

```python
from typing import List, Dict, Any

def run(params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    """
    Extract data and return it as a list of dictionaries.
    The runner handles saving the output — do not write files here.
    """
    ...
```

- `params` contains all values from the `parameters` block in the job YAML (or `None` if omitted).
- The return value must be a **list of dicts** with consistent keys. Each dict becomes one row in the output.

### Example: Simple HTTP Scraper

```python
# scrapers/metal_prices.py
import requests
from bs4 import BeautifulSoup
from typing import List, Dict, Any

def run(params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    url = "https://example.com/prices"
    response = requests.get(url, timeout=10)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    data = []
    for row in soup.select("#price-table tr")[1:]:
        cols = row.find_all("td")
        data.append({
            "metal": cols[0].text.strip(),
            "price": cols[1].text.strip(),
        })
    return data
```

### Example: Browser Automation Scraper

For pages that require JavaScript rendering, use the built-in `get_browser_page` helper:

```python
# scrapers/dynamic_page.py
from utils.browser import get_browser_page
from typing import List, Dict, Any

def run(params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    results = []
    with get_browser_page(headless=True) as page:
        page.goto("https://example.com/dynamic")
        page.wait_for_selector(".data-row")
        for row in page.query_selector_all(".data-row"):
            results.append({"value": row.inner_text()})
    return results
```

### Example: HTTP Scraper with Retries

For resilient HTTP requests with automatic retry/backoff:

```python
# scrapers/resilient_scraper.py
from utils.http import get_retry_session
from typing import List, Dict, Any

def run(params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    session = get_retry_session()
    response = session.get("https://example.com/api/data", timeout=15)
    response.raise_for_status()
    return response.json()  # Assuming the API returns a list of dicts
```

---

## 5. CLI Commands

All commands are run from the project root directory.

### List All Jobs

Prints a formatted table of every valid job defined in the `jobs/` directory.

```bash
siimpli_scrape_it list
```

**Example output:**
```
          Configured Jobs
┌──────────────┬──────────┬───────────────────────┐
│ Job Name     │ Schedule │ Script                │
├──────────────┼──────────┼───────────────────────┤
│ metal_prices │ daily    │ scrapers.metal_prices │
│ news_feed    │ hourly   │ scrapers.news_feed    │
└──────────────┴──────────┴───────────────────────┘
```

---

### Run a Single Job

Immediately runs one job by name, regardless of its schedule.

```bash
siimpli_scrape_it run metal_prices
```

---

### Run All Jobs

Immediately runs every job in the `jobs/` directory, sequentially.

```bash
siimpli_scrape_it run --all
```

---

### Start the Scheduler Daemon

Loads all jobs and starts the background scheduler. Jobs will execute automatically according to their configured schedules. The process blocks until you press **Ctrl+C**.

```bash
siimpli_scrape_it scheduler
```

The scheduler persists job state to `jobs.sqlite` in the project root, so it resumes correctly if restarted.

### MCP Server (for AI agents)

`siimpli_scrape_it mcp` starts a [Model Context Protocol](https://modelcontextprotocol.io) server over stdio, so an AI agent (Claude Code, Claude Desktop, ...) can drive the tool directly. It needs the optional dependency (Python 3.10+):

```bash
pip install -e ".[mcp]"
```

Register it with your client. The client starts the server from its own working directory, so pass `--project-dir` pointing at the folder that holds `jobs/`:

```bash
claude mcp add siimpli-scrape-it -- siimpli_scrape_it mcp --project-dir /path/to/siimpli-scrape-it
```

```json
{ "mcpServers": { "siimpli-scrape-it": {
    "command": "siimpli_scrape_it",
    "args": ["mcp", "--project-dir", "/path/to/siimpli-scrape-it"] } } }
```

| Tool | What it does |
|---|---|
| `list_jobs` | Jobs with schedule, output settings and their last run |
| `run_job(job_name)` | Starts a job in the background and returns a `run_id` immediately |
| `get_run(run_id)` | Status of one run — poll until it is no longer `running`; failures include the error |
| `get_status(job_name?, limit)` | Recent runs, newest first |
| `query_results(job_name, limit, offset, where)` | Stored rows, paged (max 1000), filtered by exact field values, e.g. `{"commodity": "copper"}` |

What it deliberately **cannot** do: create or edit jobs or scripts, run arbitrary code, or run arbitrary SQL — only jobs already defined in `jobs/` can be started. Runs started through MCP always run isolated with the job's `timeout_seconds`, obey the job's `rate_limit`, and the same job can't be started twice at once by the server. (The scheduler daemon is a separate process and is not coordinated with the MCP server, so avoid starting a job from both at the same moment.)

---

## 6. Running the Scheduler Daemon

The scheduler is designed to run as a long-lived background process. A typical production setup keeps it running under a process manager:

### Using `nohup` (Linux/macOS)

```bash
nohup siimpli_scrape_it scheduler &> logs/scheduler.log &
```

### Using a systemd Service (Linux)

```ini
[Unit]
Description=siimpli_scrape_it Scheduler

[Service]
WorkingDirectory=/path/to/siimpli-scrape-it
ExecStart=siimpli_scrape_it scheduler
Restart=always

[Install]
WantedBy=multi-user.target
```

### Stopping the Daemon

Press **Ctrl+C** in the terminal where the scheduler is running. It will perform a clean shutdown, completing any in-progress job runs.

---

## 7. Output Files

- Output is written to the path specified in `output.path` in the job YAML.
- Parent directories are created automatically if they do not exist.
- By default each run **overwrites** the output file with that run's rows (`export: latest`).
- **CSV:** Column headers are the union of the keys across all rows (first-seen order); missing values are blank.
- **JSON:** A pretty-printed JSON array (UTF-8, 2-space indent).

### Results database and history

Every run also stores its rows in a SQLite database (`data/siimpli.db`, or the path in the `SIIMPLI_DB` environment variable), so history is kept even though the file is overwritten. Two optional `output` settings control this:

```yaml
output:
  type: csv
  path: data/scrap_monster/prices.csv
  key: [date]      # fields that identify a row; re-scraped rows update in place
  export: all      # file holds every stored row for the job, not just the last run
```

- Without `key`, every run's rows are appended to the database as new rows.
- With `key`, a row whose key already exists is updated and new keys are added. A row missing a key field makes the run fail.
- `export: all` rebuilds the file from the database after each run.

If you change a job's `key` (or the shape of its rows), rows stored under the old shape would be exported alongside the new ones. Clear them first — run history is kept:

```bash
siimpli_scrape_it purge my_job        # asks for confirmation; -y skips it
```

### Run history

Each execution (including retries) is recorded with its status (`success`, `empty` when 0 rows were returned, or `failed`), duration, attempts, item count and last error:

```bash
siimpli_scrape_it status              # recent runs, all jobs
siimpli_scrape_it status my_job -n 30 # one job, last 30 runs
```

If a run's process dies without finishing (killed, machine restarted), it is marked `failed` with an "Abandoned" error the next time history is read.

### Failure notifications

Add a `notify` block to POST a JSON summary (`job_name`, `status`, `run_id`, `duration_s`, `item_count`, `error`) to a webhook when a run fails or returns nothing:

```yaml
notify:
  webhook_url: "https://example.com/hooks/scrape"
  on_failure: true   # default
  on_empty: true     # default
```

### Output validation

A scraper that "succeeds" but returns junk (a site changed, a selector matches nothing, prices turned into strings) should fail loudly, not overwrite your last good data. Add a `validation` block:

```yaml
validation:
  min_rows: 1                 # fail if fewer rows than this (also turns "0 rows" from a warning into a failure)
  required_fields: [date]     # every row needs these, non-null and non-blank
  types:                      # expected type where the field is present and non-null
    price: number             # string | number | integer | boolean
```

If any rule is violated the run is marked `failed` with a summary such as `Output validation failed: required field 'date' is missing or empty in 3 of 120 row(s) (first at row 0)`. Nothing is stored in the database and the output file is **not** touched, so the last good export stays in place. A failed validation is retried like any other failure and triggers the failure webhook. Independently of this block, every row must be a dict.

---

## 8. Retries and Rate Limiting

### Retries

If a job's `run()` function raises any exception, or if saving data fails, the runner will retry automatically.

```yaml
retry:
  retries: 3       # Total attempts (default: 3)
  retry_delay: 60  # Seconds to wait between attempts (default: 60)
```

Set `retries: 1` to disable retries (run once and fail immediately).

### Timeouts and isolation

Each attempt runs your script in its own child process. If it runs longer than `timeout_seconds` (default `3600`; `null` for no limit), the process and everything it started (including browser windows) is killed and the attempt counts as failed, so it is retried like any other failure.

```yaml
timeout_seconds: 900   # per attempt
isolate: true          # false = run inside the runner's process (no timeout; for debugging)
```

Because the script runs in a separate process, anything it returns must be JSON-serializable (other values are converted to strings), and anything it prints still appears in your terminal.

### Rate Limiting

When a job has a `rate_limit` block, requests are limited **per domain** and the budget is **shared by every job** (and every scheduler worker) that hits the same site:

```yaml
rate_limit:
  rate_limit: 10        # max requests per minute to any one domain
  delay_min: 2          # min/max seconds between requests to the same domain (random)
  delay_max: 5
  respect_robots: false # true = refuse URLs that robots.txt disallows
```

What is covered automatically:

- `utils.http.get_with_retries` — every attempt waits for a slot.
- Playwright pages from `get_browser_page` / `captcha_gate` — each main-frame page load (not images, scripts, XHR or iframes). The default navigation timeout is raised to 3 minutes so a throttled load doesn't time out.
- `FileDownloader` — downloads use the shared limiter instead of their own constructor settings.

Time your script already spent sleeping counts towards the gap, so a scraper's own `time.sleep()` doesn't double up.

**Adaptive backoff:** a `429` or `503` response (including from the browser) backs the *whole domain* off for every job, using the `Retry-After` header (default 30 s, capped at 10 minutes).

**robots.txt:** with `respect_robots: true`, a disallowed URL raises `RobotsDisallowed` (HTTP) or is blocked (browser). A missing or unreachable robots.txt means "allowed".

Requests your script makes directly with `requests` or `urllib` are **not** limited — call `utils.ratelimit.throttle(url)` before them (and `penalize(url, response.status_code, response.headers)` after) or use `get_with_retries`. If the results database is unavailable, limiting falls back to per-process.

---

## 9. Utility Helpers

These modules are available for use inside your scraper scripts.

### `utils.http.get_retry_session()`

Returns a `requests.Session` pre-configured with automatic retries and exponential backoff. Use this instead of `requests.get()` directly for more resilient HTTP scraping.

```python
from utils.http import get_retry_session

session = get_retry_session()
response = session.get("https://example.com", timeout=10)
```

### `utils.browser.get_browser_page(headless=True)`

A context manager that launches a headless Chromium browser via Playwright, yields a `Page` object, and guarantees the browser is closed cleanly on exit — even if an exception occurs.

```python
from utils.browser import get_browser_page

with get_browser_page() as page:
    page.goto("https://example.com")
    # interact with the page...
```

Pass `headless=False` during development to watch the browser visually.

### `utils.browser.captcha_gate(url, ...)` — Bot-block / CAPTCHA Helper

When a target site uses bot-detection (Radware, Cloudflare, hCaptcha, etc.), plain HTTP requests will receive a block page instead of real data. `captcha_gate` handles this by:

1. Opening a **visible** Chromium window and navigating to the URL.
2. **Detecting** known block-page signals in the page title and body text.
3. If a block is detected, **pausing** and printing a prompt in the terminal asking the user to solve the CAPTCHA in the open browser window.
4. **Waiting** (up to a configurable timeout) until the page navigates past the block.
5. **Resuming** — yielding the authenticated `Page` so your scraping code runs on the cleared session.

```python
from utils.browser import captcha_gate
from typing import List, Dict, Any

def run(params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    with captcha_gate("https://protected-site.example.com/data") as page:
        page.wait_for_load_state("networkidle")
        # page is now on the real content — scrape normally
        rows = page.query_selector_all(".data-row")
        return [{"text": r.inner_text()} for r in rows]
```

**Optional parameters:**

| Parameter | Default | Description |
|---|---|---|
| `captcha_signals` | `None` | Extra strings (case-insensitive) that indicate a block page |
| `timeout_ms` | `300000` (5 min) | Milliseconds to wait for the user to solve the challenge |

> **Note:** Because the user must interact with the browser window, jobs that use `captcha_gate` are not suitable for fully unattended scheduler runs. Use them with `siimpli_scrape_it run <job_name>` when you are at your desk.



## 10. Troubleshooting

### Seeing what a job is doing

Progress and warnings (rate-limit waits, backoffs, empty results, scraper `logging` calls) are written to **stderr**:

```bash
siimpli_scrape_it -v run my_job     # -v = debug level
```

Set `SIIMPLI_LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`, ...) to change the level for the CLI, the scheduler, the MCP server and the job processes they start. Your scraper just uses `logging.getLogger(__name__)`.

### "No jobs found in jobs directory"

The `jobs/` directory contains no valid `.yaml` files, or all files have invalid configurations. Run `siimpli_scrape_it list` to verify — badly configured jobs are silently skipped. Check your YAML syntax and ensure all required fields are present.

### "Job `<name>` not found"

The `job_name` passed to `run` does not match any `job_name` field in the YAML files. Note that the name is **case-sensitive**.

### "Module missing 'run' function"

Your scraper script does not define a top-level `run()` function, or the `script` path in the YAML is wrong. Verify the module path is a dot-separated path relative to the project root (e.g., `scrapers.metal_prices`, not `scrapers/metal_prices.py`).

### Job silently fails / no output file

Check the application logs. The runner logs a structured error message for every failure, including the exception message and stack context. You can increase log verbosity by setting the `LOG_LEVEL` environment variable:

```bash
LOG_LEVEL=DEBUG siimpli_scrape_it run my_job
```

### Playwright browser not found

Run `playwright install chromium` to download the browser binary before using any scraper that uses `utils.browser`.

### Scheduler doesn't pick up new jobs

The scheduler reads job files at startup. After adding or modifying a job YAML, restart the scheduler daemon. On restart, edited jobs fully replace their stored versions (schedule and config) and jobs whose YAML was deleted are removed.
