# siimpli_scrape_it - Roadmap

## 1. Goal Statement
Develop an agent-optimized, robust web scraping daemon (`siimpli_scrape_it`) that executes user-defined pure data extraction scripts on scheduled intervals, validates configurations and outputs against strict data contracts (Pydantic), and reliably saves the results to local storage.

## 2. Current State Assessment
- **Current State:** An initial architectural blueprint and directory structure exist in `idea.md`, focusing on strict typing, pure data extraction contracts, and atomic implementation phases. No code has been implemented.
- **Goal State:** A fully functional, tested Python application deployable as a background daemon or CLI tool, capable of running multiple sandboxed scrapers concurrently with observability.

## 3. Phased Breakdown

| Phase | Description | Estimated Effort | Deliverable |
|---|---|---|---|
| **Phase 0: Foundation** | Project setup, dependency management (`pyproject.toml`), and directory scaffolding. | Low | Empty project structure with dependencies configured. |
| **Phase 1: Core Schemas** | Implement strict data contracts using Pydantic for jobs and output validation. | Medium | `schemas/job_schema.py` and `core/config.py` with passing tests. |
| **Phase 2: Storage & Runner** | Build isolated script execution via `importlib` and robust output writing (JSON/CSV) with `try/except` boundaries. | High | `core/runner.py` and `utils/storage.py` with dummy scraper tests. |
| **Phase 3: CLI Integration** | Implement the Typer CLI exposing `run`, `list`, and `scheduler` commands. | Medium | CLI executable returning expected console output. |
| **Phase 4: Scheduler** | Integrate APScheduler, translating YAML schedules (e.g., cron) into background triggers. | High | `core/scheduler.py` actively queuing and executing jobs based on mock time. |
| **Phase 5: Utilities & Polish** | Add HTTP retry wrappers, Playwright browser context managers, and structured JSON logging. | Medium | Utility modules and logging integrated into the runner. |

## 4. Architecture Diagram

```mermaid
graph TD
    CLI[CLI Main Entrypoint] -->|Start Daemon| Sched[APScheduler]
    CLI -->|Run Single Job| Run[Runner]
    Sched -->|Cron Trigger| Run
    
    YML[(job.yaml configs)] -.-> ConfigReader[Config Parser]
    ConfigReader -->|Validates| Pyd[Pydantic strict models]
    Pyd --> Run
    
    Run -->|Dynamically Loads| Script[User Scraper Scripts]
    Script -->|Uses| Utils[Browser / HTTP Utils]
    Script -->|Returns List[Dict]| Run
    
    Run -->|Validates Output| Pyd
    Run --> Storage[(CSV / JSON)]
    Run --> Log[Structured Logger]
```

## 5. Risk Register

| Risk | Impact | Mitigation Strategy |
|---|---|---|
| **Third-party site structure changes** | Scrapers fail, breaking jobs. | Scripts return empty data/errors; runner catches all, logs gracefully, does not crash scheduler. |
| **Hanging Browser Processes** | Memory leaks exhaust system resources. | Enforce use of Playwright Context Managers (`utils/browser.py`) that strictly `finally: close()`. |
| **Corrupted/Invalid YAML job configs** | Daemon crashes on startup. | Strict Pydantic validation on load; ignore invalid files with a warning, start daemon anyway. |
| **Over-concurrency** | Too many jobs scheduled at once lock CPU/Network. | Implement thread pool constraints in the runner or APScheduler configuration. |
