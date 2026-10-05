


> **"Act as a Principal Software Architect. Review the provided system specification for 'siimpli_scrape_it'. Completely restructure and optimize this specification so that an autonomous AI coding agent (like Devin, Aider, or an AutoGPT) can implement it flawlessly with minimal human intervention. Focus heavily on establishing explicit data contracts (Pydantic), enforcing strict typing, designing modular and atomic components, building in test-driven milestones, and structuring a step-by-step implementation plan that respects LLM context windows."**

*Here is the response acting on that elevated, highly rigorous prompt.*

***

# Architectural Blueprint: `siimpli_scrape_it` (Optimized for Agentic Implementation)

To ensure an autonomous AI agent can successfully build, test, and maintain this system, the specification has been fundamentally restructured. AI agents struggle with ambiguity, global state, and monolithic tasks. Therefore, this revised spec introduces **strict data contracts**, **dependency injection**, **test-driven boundaries**, and a **phased implementation plan**.

---

## 1. Core Agentic Design Principles

1.  **Strict Typing & Schemas First:** All configuration and data passing will be governed by `Pydantic` models. This gives the AI agent deterministic rules for input validation.
2.  **Pure Data Extraction Contracts:** Scraper scripts must be pure extractors. The `run()` function must return a `List[Dict[str, Any]]`. The runner handles output routing, storage, and logging. Scripts *do not* write to disk themselves.
3.  **Atomic Implementation Phases:** The system is broken down into discrete, testable milestones. The agent must complete and pass tests for Phase N before proceeding to Phase N+1.
4.  **Graceful Degradation & Observability:** Structured JSON logging and explicit `try/except` boundaries around dynamically loaded modules prevent a single failing script from crashing the scheduler daemon.

---

## 2. Directory Structure (Agent-Optimized)

*Changes: Added `schemas/`, `tests/`, and `exceptions.py` to bound the agent's context and enforce modularity.*

```text
siimpli_scrape_it/
├── scrapers/               # User-defined scripts (injected at runtime)
├── jobs/                   # YAML configurations
├── core/
│   ├── config.py           # Loads YAML into Pydantic models
│   ├── runner.py           # Dynamically imports and executes scripts
│   ├── scheduler.py        # APScheduler daemon wrap
│   ├── logger.py           # Structured logging utility
│   └── exceptions.py       # Custom error classes (JobFailedError, ConfigError)
├── schemas/                # STRICT DATA CONTRACTS
│   ├── job_schema.py       # Pydantic models for Jobs
│   └── output_schema.py    # Standardized output formats
├── utils/                  # Shared helper functions
│   ├── http.py             # requests wrapper with retries
│   ├── browser.py          # Playwright context managers
│   └── storage.py          # CSV/JSON writers
├── tests/                  # Pytest directory (Agent must write these alongside code)
├── main.py                 # Typer CLI entrypoint
├── pyproject.toml          # Modern dependency management
└── README.md
```

---

## 3. Explicit Data Contracts (Schemas)

*Agent Instruction: Implement these in `schemas/job_schema.py` using Pydantic before writing any operational code.*

```python
from pydantic import BaseModel, Field
from typing import Optional, Literal, Dict, Any

class OutputConfig(BaseModel):
    type: Literal["csv", "json"]
    path: str

class RetryConfig(BaseModel):
    retries: int = Field(default=3, ge=0)
    retry_delay: int = Field(default=60, ge=0) # in seconds

class RateLimitConfig(BaseModel):
    rate_limit: int = Field(default=10) # requests per minute
    delay_min: int = Field(default=2)
    delay_max: int = Field(default=5)

class JobConfig(BaseModel):
    job_name: str
    script: str                 # e.g., "scrapers.metal_prices"
    schedule: str               # e.g., "hourly", "daily", or cron "0 3 * * *"
    output: OutputConfig
    retry: RetryConfig = RetryConfig()
    rate_limit: Optional[RateLimitConfig] = None
    parameters: Optional[Dict[str, Any]] = None
```

---

## 4. Standardized Script Interface

To allow the AI to build a robust `runner.py`, the interface between the core system and the user scripts must be strictly typed.

**The Contract:**
Every script in the `scrapers/` directory MUST contain a `run` function with the following signature:

```python
from typing import List, Dict, Any

def run(params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    """
    Extracts data and returns it as a list of dictionaries.
    The runner.py will handle saving this data to the configured Output location.
    """
    pass
```

*Example Script (`scrapers/metal_prices.py`):*
```python
import requests
from bs4 import BeautifulSoup
from typing import List, Dict, Any

def run(params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    url = "https://example.com/prices"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    
    data = []
    for row in soup.select("#price-table tr")[1:]:
        cols = row.find_all("td")
        data.append({
            "metal": cols[0].text.strip(),
            "price": cols[1].text.strip()
        })
    return data
```

---

## 5. Execution Pipeline (`core/runner.py`)

*Agent Instruction: The runner must operate as an isolated state machine with robust error boundaries.*

**Pipeline Sequence:**
1. Accept a `JobConfig` object.
2. Initialize `try/except` block catching `Exception`.
3. Use `importlib.import_module(config.script)` to load the script.
4. Measure execution time (`time.perf_counter()`).
5. Execute `module.run(params=config.parameters)`.
6. Validate the output is `List[Dict[str, Any]]`.
7. Pass data to `utils.storage.save_data(data, config.output)`.
8. Log success/failure to `core.logger` (including runtime and scraped item count).
9. If failure, check `config.retry` and queue retry if applicable.

---

## 6. Utilities & Browser Automation

**Playwright Context Manager (`utils/browser.py`):**
To prevent hanging processes, the AI must implement browser automation using context managers.

```python
from playwright.sync_api import sync_playwright
from contextlib import contextmanager

@contextmanager
def get_browser_page(headless: bool = True):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()
            browser.close()
```

---

## 7. CLI Interface (`main.py`)

*Agent Instruction: Use `Typer` to build the CLI. Expose exactly four commands.*

1. `siimpli_scrape_it run --all`: Executes all jobs sequentially immediately.
2. `siimpli_scrape_it run [job_name]`: Executes a single YAML job by name.
3. `siimpli_scrape_it list`: Prints a formatted table (use `rich` library) of available jobs in the `jobs/` directory.
4. `siimpli_scrape_it scheduler`: Initializes `APScheduler` (BackgroundScheduler), binds jobs based on their YAML `schedule` string, and blocks the main thread waiting for executions.

---

## 8. Agentic Implementation Plan

*Agent Instruction: Do not attempt to build the entire system at once. Follow these phases sequentially. Write Pytest tests for each phase before moving to the next.*

### Phase 1: Core Schemas and Config Parsing (TDD Focus)
*   **Task:** Create `schemas/job_schema.py`. Create `core/config.py` to parse `jobs/*.yaml` into `JobConfig` objects.
*   **Validation:** Write `tests/test_config.py` to ensure YAML parsing fails gracefully on invalid schemas.

### Phase 2: Storage and Runner
*   **Task:** Implement `utils/storage.py` (CSV and JSON writers). Implement `core/runner.py` using `importlib`.
*   **Validation:** Write `tests/test_runner.py` using a dummy scraper script that returns static data. Ensure it writes to disk correctly and captures errors if the dummy script raises an Exception.

### Phase 3: CLI Integration
*   **Task:** Implement `main.py` using `Typer` and `Rich`. Hook up the `run` and `list` commands to the Phase 2 runner and Phase 1 config parser.
*   **Validation:** Test CLI commands via `typer.testing.CliRunner`.

### Phase 4: Scheduler
*   **Task:** Implement `core/scheduler.py` using `APScheduler`. Write a translation layer that converts string schedules (`"hourly"`, `"0 3 * * *"`) into APScheduler triggers.
*   **Validation:** Mock the system time and verify that jobs are enqueued at the correct intervals.

### Phase 5: Utilities & Polish
*   **Task:** Implement `utils/http.py` (with automatic backoff/retries) and `utils/browser.py` (Playwright wrappers). Add structured logging via the standard `logging` library configured to output JSON to `logs/runs.log`.

---

## 9. Dependencies (`pyproject.toml`)

Using a standard modern Python dependency stack instead of raw `requirements.txt`:

```toml
[project]
name = "siimpli_scrape_it"
version = "0.1.0"
dependencies =[
    "pydantic>=2.0.0",
    "typer>=0.9.0",
    "rich>=13.0.0",
    "pyyaml>=6.0",
    "apscheduler>=3.10.0",
    "requests>=2.31.0",
    "beautifulsoup4>=4.12.0",
    "playwright>=1.39.0",
]

[project.optional-dependencies]
dev =[
    "pytest>=7.4.0",
    "pytest-mock>=3.11.1"
]
```