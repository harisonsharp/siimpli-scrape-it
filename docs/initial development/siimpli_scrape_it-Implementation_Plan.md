# siimpli_scrape_it - Implementation Plan

## 1. Locked Decisions
Based on the Decision Questionnaire, the following architectural paths are locked in:

| Decision Area | Chosen Path | Impact |
| --- | --- | --- |
| **Execution Isolation** | Same Process (`importlib`) | Runner will load scripts as modules and manage them via standard `try/except`. Scripts must not crash the main thread. |
| **Scheduler State** | SQLite / DB Store | `APScheduler` will be configured with an SQLAlchemy job store (SQLite) to persist job schedules across daemon restarts. |
| **Scraper Concurrency** | Fixed Thread/Process Pool | The scheduler will use a bounded thread pool (e.g., `ThreadPoolExecutor(max_workers=4)`) to prevent resource exhaustion from concurrent headless browsers. |

## 2. Agent Execution Rules
Agents must strictly adhere to the following operational guidelines:
- **Test-Driven Development (TDD) Mandatory:** For every ticket, the agent MUST write failing `pytest` tests first, implement the target logic to pass them, and ensure 100% pass rates before proceeding.
- **Strict Typing:** All data inputs and config parsing MUST be validated via `Pydantic` models before logic execution.
- **Context Boundaries:** The agent must only edit files specified in the **Context** of the current ticket to minimize unintended side effects.

## 3. Task Chunking & Parallelization
| Phase | Dependency | Parallelizable? | Notes |
| --- | --- | --- | --- |
| 0. Foundation | None | No | Must be completed first to establish environment. |
| 1. Schemas | Phase 0 | Yes (Internal) | `job_schema` can be developed independently, then `config` integrates it. |
| 2. Runner | Phase 1 | No | Runner depends directly on schemas. Storage can be implemented parallel to runner scaffolding. |
| 3. Scheduler | Phase 2 | No | Scheduler needs the runner function to bind jobs. Requires adding SQLite capabilities to APScheduler. |
| 4. CLI | Phases 1 & 3 | Yes | Can start mapping typer commands to empty functions once Phase 1 config is done. |
| 5. Utilities | Phase 0 | Yes | `browser.py` and `http.py` can be written completely parallel to other phases. |

## 4. Phase-by-Phase Tickets

### Phase 0: Foundation
#### Ticket 0.1: Project Environment & Scaffolding
- **Context:** `pyproject.toml`, directory structure.
- **Requirements:** 
  - Create the exact directory structure outlined in the architectural blueprint.
  - Define `pyproject.toml` with dependencies: `pydantic`, `typer`, `rich`, `pyyaml`, `apscheduler`, `SQLAlchemy`, `pytest`, etc.
- **Acceptance Criteria:** `pip install -e .[dev]` succeeds. Running `pytest` (even if empty) does not error. `tree` (or equivalent directory listing) shows the correct scaffolding structure.

### Phase 1: Core Schemas and Config
#### Ticket 1.1: Data Contracts
- **Context:** `schemas/job_schema.py` (and related output schemas if separated).
- **Requirements:** Implement strict `Pydantic` models for `JobConfig`, `OutputConfig`, `RetryConfig`, `RateLimitConfig` based on the Stage 1 specification.
- **Acceptance Criteria:** `tests/test_schemas.py` validates matching dicts, asserts default constraints, and successfully raises `ValidationError` for structurally invalid simulated inputs.

#### Ticket 1.2: Configurations Parser
- **Context:** `core/config.py`.
- **Requirements:** Method to parse strings/files of YAML located in `jobs/` and map them to returning valid `JobConfig` instantiations. Handled malformed YAML with specific warnings rather than exceptions killing the daemon.
- **Acceptance Criteria:** `tests/test_config.py` passes when reading both valid and explicitly malformed mock YAML strings.

### Phase 2: Execution and Storage
#### Ticket 2.1: Storage Utilities
- **Context:** `utils/storage.py`.
- **Requirements:** Implement robust CSV and JSON writing functions. Must accept the `List[Dict[str, Any]]` output format and the target path configured by `OutputConfig`.
- **Acceptance Criteria:** `tests/test_storage.py` writes temporary data chunks to mock destinations and correctly parses the saved `.json` or `.csv` files confirming their structure matches the output dict content.

#### Ticket 2.2: Isolated Runner
- **Context:** `core/runner.py`.
- **Requirements:** Use `importlib` to dynamically import the user script string (e.g., `scrapers.metal_prices`). Enclose execution in a rigid `try/except` block to prevent failures from bubbling. Feed script output to storage utility.
- **Acceptance Criteria:** `tests/test_runner.py` writes a temporary dummy python module returning static data, calls runner string to execute it, and verifies `utils/storage.py` correctly logged output. Check failures are logged explicitly without raising global errors.

### Phase 3: Scheduler
#### Ticket 3.1: Persistent APScheduler Engine
- **Context:** `core/scheduler.py`.
- **Requirements:** Incorporate `apscheduler.schedulers.background.BackgroundScheduler`. Set up `SQLAlchemyJobStore` referencing `sqlite:///jobs.sqlite`. Implement a pool executor configured strictly for `max_workers=4`. Parse `JobConfig` objects to assign to triggers dynamically. 
- **Acceptance Criteria:** `tests/test_scheduler.py` utilizes in-memory SQLite store (`sqlite:///:memory:`) to verify jobs are registered via APScheduler interface without runtime crash and concurrency defaults hold structurally firm rules.

### Phase 4: CLI Integration
#### Ticket 4.1: Typer Commands
- **Context:** `main.py`.
- **Requirements:** Construct typer app endpoints matching spec: `run --all`, `run [job_name]`, `list`, `scheduler`. Utilize `rich` library heavily for terminal table lists and outputs.
- **Acceptance Criteria:** `tests/test_cli.py` exercises typer's native `CliRunner` simulating user terminal text. Validates commands generate formatted stdout outputs logically responding to job store statuses.

### Phase 5: Utilities
#### Ticket 5.1: Robust Utilities
- **Context:** `utils/http.py`, `utils/browser.py`.
- **Requirements:** Design `requests` connection wrapper managing simple `urllib3` retry sessions. Formulate Playwright context manager executing specific browser init parameters assuring deterministic cleanup enclosed in standard `finally:` directives. 
- **Acceptance Criteria:** Mocks confirm HTTP configurations respond successfully to status code `502`/`503`. System integration check verifies Playwright process yields effectively and safely disposes processes.

## 5. Agent Context Window Guidelines
- **Agent Constraint:** Execute strictly **ONE** ticket per prompt.
- **File Constraint:** When working on a specific ticket, ONLY interact with files defined explicitly in the ticket's `Context` variable to avoid state drift.
- **Verification Priority:** Perform full test suite runner verification (`pytest -v`) after every implemented ticket. Document your stdout result. Do not conclude resolving the ticket until tests fully pass!
