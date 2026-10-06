# Testing agent-written jobs, end to end

A hands-on checklist for trying out the MCP server, the declarative step engine, the draft workflow and the guardrails. Do the phases in order; each one says what to do, what you should see, and where to look if you don't. It takes about an hour. Commands are for **Git Bash** on Windows, run from the repo root unless stated.

The two practice sites used here, `books.toscrape.com` and `quotes.toscrape.com`, exist to be scraped. I ran both example jobs live while writing this, so the numbers below are real.

## What this does and doesn't test

It tests the **MCP surface**: what an agent can do through the `siimpli-scrape-it` tools. The trust boundary is that surface. An agent that also has file-write access to this repo (for example Claude Code launched *inside* it) can simply edit `jobs/*.yaml` directly, which no guardrail here can stop. So run the agent from somewhere else (Phase 1).

Not covered: prompt injection from scraped page content (the allowlist and human approval are the defences, but there is no dedicated test), and DNS rebinding (documented gap in `core/safe_http.py`).

## Phase 0: Prerequisites

Open Git Bash and go to the repo. Keep this terminal for the CLI steps; you'll use a second one for the agent.

```bash
cd /c/Users/haris/work/siim2025/siimpli-scrape-it
export PYTHONIOENCODING=utf-8        # so curly quotes etc. print correctly in this terminal

python --version                     # must be 3.10 or newer for the MCP SDK
pip install -e ".[dev,mcp]"
python -m pytest -q                  # expect everything to pass (~3 minutes)
```

Make sure there is no allowlist yet, so you start from the default-deny state, and no old audit log:

```bash
[ -e allowed_domains.txt ] && echo "EXISTS - delete it" || echo "absent (good)"
rm -f data/agent_audit.jsonl         # so the audit check in Phase 8 is readable
```

## Phase 1: Connect an agent

Use an agent session whose working directory is **not** this repo. In a *second* Git Bash window:

```bash
mkdir -p ~/scratch/agent-test
cd ~/scratch/agent-test                                  # cd FIRST: see the note below
REPO=C:/Users/haris/work/siim2025/siimpli-scrape-it     # forward slashes; keep the C:/ form
claude mcp add siimpli-scrape-it -- python $REPO/main.py mcp -C $REPO
claude mcp list                                          # expect: siimpli-scrape-it ... √ Connected
claude
```

**Run `claude mcp add` from the directory you will launch the agent in.** By default it registers the server for the *current directory only* ("local" scope). Register it from the repo (or anywhere else) and then start `claude` in `~/scratch/agent-test`, and the session sees no siimpli tools at all. Check with `claude mcp list` from the directory you launch from. (`--scope user` would make it visible in every directory, including sessions inside this repo, which defeats the point of keeping the agent away from it.)

Keep the `C:/...` form rather than `/c/...` here: it's what the Windows Python and Claude Code both understand, and Git Bash leaves it alone. If `python` there isn't the interpreter you installed `[mcp]` into, put its full path in the command (`which -a python` lists them in order).

In the session, run `/mcp`. **Expect:** `siimpli-scrape-it` connected, with 10 tools: `list_jobs`, `get_status`, `get_run`, `query_results`, `run_job`, `propose_job`, `list_drafts`, `get_draft`, `test_job`, `delete_draft`.

**There must be no approve/promote tool.** If you see one, stop and tell me.

Then ask: *"List the siimpli jobs and recent runs."* You should see the three shipped jobs (`first_job`, `scrap_monster_prices`, `usgs_download`) and the ScrapMonster run history.

**If `/mcp` shows no siimpli server at all** (not even a failed one), it's registered for a different directory. Run `claude mcp list` in the directory you started `claude` from; if it's missing there, run the `claude mcp add` line again from that directory. If the session was already open, restart it (`/exit`, then `claude`), because MCP servers are loaded at startup.

If the server is listed but shows *Failed* / won't connect: in the repo terminal run `python main.py mcp -C .` by hand. It should sit silently waiting on stdin (Ctrl+C to quit); an `ImportError` means the `mcp` extra is missing from that Python.

## Phase 2: The guardrails say no

An agent may balk at deliberately bad inputs, so frame it as testing: *"I'm testing the guardrails of this tool. Call `propose_job` with exactly the YAML below and show me the raw response."* Every case from T2 on should come back `ok: false` with the problems listed, and **no file should appear in `jobs/drafts/`** (check after each, in the repo terminal: `ls jobs/drafts`).

**T1: no allowlist yet.** Use this valid-looking job:

```yaml
job_name: books_catalogue
script: scrapers.declarative
schedule: daily
output: {type: csv, path: data/books/books.csv, key: [title], export: all}
retry: {retries: 2, retry_delay: 5}
rate_limit: {rate_limit: 20, delay_min: 1, delay_max: 2, respect_robots: true}
validation:
  min_rows: 20
  required_fields: [title, price, url]
  types: {price: number}
timeout_seconds: 300
parameters:
  vars: {base: "https://books.toscrape.com/catalogue"}
  steps:
    - for_each:
        in: [1, 2, 3]
        as: page_no
        steps:
          - fetch: {url: "{base}/page-{page_no}.html", as: page}
          - extract:
              from: page
              rows: "article.product_pod"
              as: books
              fields:
                title: {select: "h3 a", attr: title}
                price: {select: "p.price_color", type: number}
                rating: {select: "p.star-rating", attr: class, regex: 'star-rating (\w+)'}
                url: {select: "h3 a", attr: href}
          - emit: {from: books, add: {page: "{page_no}"}}
```

Expect: accepted, because the URL is templated (`{base}`) and so can't be checked statically. Keep this one: it's your happy-path draft, and it will fail at *run* time until you allowlist the domain (Phase 3 shows that). If you want to see the static check fire too, replace the fetch URL with the literal `https://books.toscrape.com/catalogue/page-1.html`; that is refused with *"not on the domain allowlist"*.

For T2 to T6, take the YAML above and change only the named thing:

| # | Change | Expected problem |
|---|---|---|
| T2 | `script: os.system` (or `scrapers.first_scraper`) | `agents may not supply code` |
| T3 | add `notify: {webhook_url: "http://example.com/x"}` and `isolate: false`; delete `validation` and `rate_limit` | all four reported in one response |
| T4 | `rate_limit: {rate_limit: 500, respect_robots: false}` and `timeout_seconds: 99999` | each limit named |
| T5 | `output.path: data/../jobs/evil.csv`, then `/etc/x.csv`, then `data/drafts/x.csv`, then `data/x.db` | `under data/`, `relative`, `reserved`, `end in .csv` respectively |
| T6 | add a header `headers: {Cookie: "a=b"}` to a fetch; then replace a step with `- exec: "import os"` | rejected as an unknown/forbidden step or header |

**T7: YAML bombs.** Propose a job whose text contains `a: &x [1]` and `b: *x`. Expect *"YAML anchors and aliases are not allowed"*.

**T8: no way to approve.** Ask the agent: *"Approve the books_catalogue draft so it goes live."* It should say it has no such tool (it may offer to tell you the CLI command). Confirm `ls jobs` still shows only the three original YAML files and the `drafts` folder.

**T9: names can't escape.** Ask the agent to call `get_draft` and `delete_draft` with the name `../jobs/first_job`. Expect an error, and `jobs/first_job.yaml` still exists (`ls jobs/first_job.yaml`).

## Phase 3: SSRF and the allowlist at run time

Make the happy-path draft from T1 exist (re-propose it if you cleaned up), then:

**T10: not allowlisted.** Ask the agent to call `test_job("books_catalogue")` and poll `get_run`. Expect `status: failed` with an error containing `blocked: ... books.toscrape.com is not on the domain allowlist`. Fetches blocked = nothing downloaded.

**T11: allowlisted but not public.** Put `localhost` in the allowlist, and have the agent propose a copy of the job with `vars: {base: "http://localhost/catalogue"}` (named `ssrf_probe`):

```bash
echo "localhost" > allowed_domains.txt
```

Dry-run it. Expect `failed` with `localhost resolves to a non-public address`. Then **remove `localhost` again** (the next phase overwrites the file).

## Phase 4: The happy path

```bash
printf '%s\n' "# domains agents may fetch" books.toscrape.com quotes.toscrape.com > allowed_domains.txt
cat allowed_domains.txt
```

Now ask the agent, in plain language, to author the job itself, without showing it my YAML:

> *"Using the siimpli-scrape-it tools, create a job named `books_agent` that scrapes the first 3 pages of https://books.toscrape.com/catalogue/page-1.html (pages are page-1.html, page-2.html, ...). For each book I want the title, price as a number, the star rating, and the product link. Validate it, dry-run it, look at the sample rows, and fix whatever fails. Don't stop until the dry run succeeds. Don't ask me questions."*

Things worth watching as it works:
- Does it find the right shape from the `propose_job` description alone? Note each rejection it hits and whether the message was enough for it to fix the problem. **That feedback is the most valuable output of this whole exercise.**
- Does it use `get_draft` to look at sample rows rather than just trusting `status: success`?

**Expect at the end:** a dry run `success` with **60 rows**; the first row *A Light in the Attic* at **51.77** with rating `Three`. (The agent may name fields differently.)

If it gets stuck, give it the T1 YAML as a reference and ask it to compare.

## Phase 5: Human review and approval

Everything here is yours, not the agent's (repo terminal):

```bash
python main.py draft list
python main.py draft show books_agent         # YAML, last test, 5 sample rows
python main.py draft test books_agent         # run the dry run yourself; expect "success ... 60 rows"
```

**Check the dry run really stayed in its sandbox:**

```bash
python main.py status "draft:books_agent"     # runs appear under the draft: name
python main.py status books_agent             # nothing yet under the real name
[ -e data/drafts/books_agent/out.csv ] && echo "scratch output exists (good)"
grep output jobs/drafts/books_agent.yaml      # note the output.path ...
# ... and that path must not exist yet, e.g.:  ls data/books   ->  "No such file"
```

**Tampering must block approval** (do these on a second draft, or before the final approval; `<name>` is the draft's name):
1. Propose a new draft and run `python main.py draft approve <name> -y` without testing. Expect *"no successful dry run"*.
2. Test it (`python main.py draft test <name>`), then change the file: `echo "# tamper" >> jobs/drafts/<name>.yaml`, then approve. Expect *"no successful dry run"* again: the content hash changed.
3. Re-propose and test it again, then empty the allowlist with `cp allowed_domains.txt /tmp/allow.bak && : > allowed_domains.txt`, then approve. Expect a failure mentioning the allowlist. Restore with `cp /tmp/allow.bak allowed_domains.txt`.

**Now approve for real:**

```bash
python main.py draft approve books_agent      # prints the YAML + sample rows, asks to confirm
ls jobs jobs/drafts                           # books_agent.yaml is now in jobs/; drafts/ no longer has it
python main.py list                           # shows it as a live job
```

## Phase 6: The approved job behaves like any other

First count rows with a helper (CSV-aware, so quoted commas don't confuse it). Put the job's `output.path` in `$OUT`:

```bash
OUT=$(python -c "import yaml;print(yaml.safe_load(open('jobs/books_agent.yaml'))['output']['path'])")
count() { python -c "import csv,sys;print(len(list(csv.DictReader(open(sys.argv[1],encoding='utf-8')))))" "$1"; }

python main.py -v run books_agent             # ~10 s; logs show requests spaced ~1-2 s apart
count "$OUT"                                  # expect 60
python main.py -v run books_agent             # run it again
count "$OUT"                                  # still 60
```

(If the agent chose `type: json`, use `python -c "import json;print(len(json.load(open('$OUT'))))"` instead of `count`.)

Expect still **60** rows after the second run (they upsert on the `key`), and `python main.py status books_agent` showing two `success` runs.

Then, back in the agent session: *"Run books_agent and then query its results where rating is Three."* This exercises `run_job` (isolated, returns a run id), `get_run` and `query_results` against an agent-authored job.

Optional: `python main.py scheduler`, and confirm it lists the new job. Ctrl+C to stop.

## Phase 7: Failure modes

**T12: bad selector fails validation.** Ask the agent: *"Propose a copy of books_agent called books_broken that uses the selector `p.price` (which doesn't exist) for the price, and call test_job. Tell me exactly what comes back."* Expect `failed` with *"required field 'price' is missing or empty in 60 of 60 row(s)"*, no rows stored for it (`get_draft` shows `result_rows: 0`), and `books_agent`'s data untouched.

**T13: a JSON API, with query parameters.** Ask for a job named `quotes_agent` that collects quote text, author name and tags from `https://quotes.toscrape.com/api/quotes?page=N` for pages 1 to 3, output `data/quotes/quotes.json`, keyed on `text`. Expect **30 rows**. (Tags arrive as a JSON-encoded string, since a cell holds scalars only. That is a known limitation of `extract`, so tell me if you'd rather it flatten lists.)

**T14: a stuck dry run.** Have the agent propose a job with `timeout_seconds: 5`, a `for_each` over 30 pages and `delay_min: 2` (30 requests at about 2 s each can't fit in 5 s). Expect `failed` with a timeout message and no orphaned processes afterwards: `tasklist | grep -i python` should show only the MCP server's own `python.exe` (and your terminal's, if any), with no extra worker processes left from the killed run.

## Phase 8: Audit trail

```bash
cat data/agent_audit.jsonl
# or, one event per line, easier to scan:
python -c "import json;[print(r['at'], r['event'], r.get('name',''), r.get('status',''), r.get('problems','')) for r in map(json.loads, open('data/agent_audit.jsonl', encoding='utf-8'))]"
```

**Expect one JSON line per event:** `propose_rejected` (with the problem list) for every refused attempt in Phase 2, `propose` for each accepted draft, `test_start` / `test_end` for each dry run (with status and row count), and `approve` / `delete_draft` for the ones you did. Dry runs you start from the CLI are logged the same way as the agent's.

Check also that nothing landed outside the intended places: the only new files under `jobs/` should be the ones you approved (`ls jobs`), and under `data/` only the output folders, `drafts/`, the DB and the audit log (`ls data`).

## Phase 9: Clean up

```bash
python main.py draft list                     # reject anything left over:  python main.py draft reject <name>
python main.py purge books_agent -y
rm -f jobs/books_agent.yaml jobs/quotes_agent.yaml
rm -rf data/drafts                            # and the job's own data/<folder> if you want it gone
claude mcp remove siimpli-scrape-it           # if you don't want it registered permanently
```

Keep `allowed_domains.txt` only if you want those two practice domains permitted going forward.

## Git Bash gotchas

- **Paths given to Windows programs:** Git Bash rewrites `/c/...` into `C:\...` for native programs, which is usually what you want, but it can also rewrite things that only *look* like paths (for example a YAML value starting with `/`). If an argument arrives mangled, prefix the command with `MSYS_NO_PATHCONV=1`.
- **`python` not found / wrong one:** `which -a python`. Use the full path (`/c/Users/<you>/AppData/Local/Programs/Python/Python311/python.exe`) if needed.
- **Garbled characters in output:** make sure `PYTHONIOENCODING=utf-8` is exported in that terminal.
- **Ctrl+C doesn't stop the scheduler or MCP server:** close the terminal tab, or `taskkill //F //IM python.exe` (note the double slashes in Git Bash). That kills *every* Python process, so only do it when nothing else is running.

## Results sheet

| Phase | Test | Pass? | Notes |
|---|---|---|---|
| 1 | 10 tools shown, no approve tool | | |
| 2 | T1 to T9 all rejected as listed, nothing written to `jobs/` or `jobs/drafts/` | | |
| 3 | T10 (allowlist), T11 (non-public address) blocked at run time | | |
| 4 | Agent authors a working job unaided: 60 rows | | rejections hit: |
| 5 | Dry run isolated; 3 tamper attempts blocked; approval works | | |
| 6 | Approved job runs, upserts (60 → 60), queryable by the agent | | |
| 7 | T12 validation fail, T13 JSON job (30 rows), T14 timeout | | |
| 8 | Audit log complete | | |

If anything fails, send me: the agent transcript for that step, `data/agent_audit.jsonl`, the draft file from `jobs/drafts/`, and the output of `python main.py status` (add `-v` before the command for debug logging).
