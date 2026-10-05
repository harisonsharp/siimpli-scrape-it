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
- Each run **overwrites** the output file (it does not append).
- **CSV:** Column headers are derived from the dictionary keys returned by `run()`. All rows must have the same keys.
- **JSON:** A pretty-printed JSON array (UTF-8, 2-space indent).

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

### Rate Limiting

The `rate_limit` block documents your intended rate limit, but enforcement is the responsibility of your scraper script (configure your HTTP session or add `time.sleep()` calls accordingly). Use the `parameters` block to pass these values into your script if needed.

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

The scheduler reads job files at startup. After adding or modifying a job YAML, restart the scheduler daemon.
