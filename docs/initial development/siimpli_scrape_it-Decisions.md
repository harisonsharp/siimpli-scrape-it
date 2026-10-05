# siimpli_scrape_it - Decisions

### Q1. Execution Isolation Mechanism
**Why it matters:** Running user-defined scraper scripts using `importlib` in the same Python process allows for simpler implementation, but a memory leak or a fatal C-extension crash in a script could take down the entire `siimpli_scrape_it` daemon. Running them via subprocess provides true isolation but adds overhead.

| Option | Detail | Pro | Con |
|---|---|---|---|
| **A - Same Process (importlib)** | Scripts are imported as modules and run in the scheduler's process map using standard Threads/Asyncio. | Simpler context sharing, easier to build initially. | A script segfault brings down everything. |
| **B - Isolated Subprocess** | The runner spawns a new Python process (e.g. `python -m siimpli.execute_worker script_name`) | True isolation, memory is reclaimed on exit. | Slower startup, harder to collect return data (needs stdout/IPC). |

**Your Answer:** A
> 

**Default:** A - Same Process (importlib) — Best balance for a V1 iteration where scrapers are assumed to be somewhat trusted internal scripts; we rely on `try/except` for normal Python-level errors.

---

### Q2. Scheduler State Persistence
**Why it matters:** If the server is rebooted, the scheduler needs to know when to run jobs. If a daily job was missed during downtime, should it run immediately on startup?

| Option | Detail | Pro | Con |
|---|---|---|---|
| **A - In-Memory Store** | Schedules are stateless. Time checking starts fresh when application boots. | Extremely simple, no database dependency. | Missed jobs are simply skipped until next execution time. |
| **B - SQLite / DB Store** | APScheduler uses SQLite to track last-run and next-run times persistently. | Prevents missed jobs, highly resilient. | Requires disk locking, DB migrations, more complex state. |

**Your Answer:** B
> 

**Default:** A - In-Memory Store — Keeps the daemon lightweight and purely configuration-driven (YAML files as single source of truth).

---

### Q3. Scraper Concurrency Limits
**Why it matters:** If 10 hourly jobs trigger exactly at `00:00`, running 10 headless browser instances simultaneously could overwhelm the host machine. 

| Option | Detail | Pro | Con |
|---|---|---|---|
| **A - Fixed Thread/Process Pool** | The scheduler is configured with a strict ceiling (e.g., max 4 concurrent jobs). Extra jobs wait in queue. | Predictable resource usage, host machine won't crash. | Low priority jobs might delay high priority ones if queue backs up. |
| **B - Unbounded Execution** | Every job launches immediately upon trigger. | Simplest execution path, all jobs run "on time". | Risk of out-of-memory cascading failures. |

**Your Answer:** A
> 

**Default:** A - Fixed Thread/Process Pool — Necessary to ensure the `Reliability` constraint of URPS, avoiding out-of-memory errors on small VMs.
