import json
import textwrap

import pytest
import yaml
from pydantic import ValidationError

from core import safe_http, steps
from core.runner import execute_job
from core.steps import StepError, parse_program, render, run_program
from schemas.job_schema import JobConfig
from tests.conftest_site import Site
from utils import db


@pytest.fixture
def site(tmp_path, monkeypatch):
    s = Site()
    allow = tmp_path / "allowed_domains.txt"
    allow.write_text("127.0.0.1\n", encoding="utf-8")
    monkeypatch.setenv(safe_http.ALLOWLIST_ENV, str(allow))
    monkeypatch.setattr(safe_http, "_ip_blocked", lambda ip: False)
    monkeypatch.setattr(safe_http, "ALLOWED_PORTS", (None, 80, 443, s.port))
    yield s
    s.close()


def program(text, **fmt):
    text = textwrap.dedent(text)
    return parse_program(yaml.safe_load(text.format(**fmt) if fmt else text))


PRICES_HTML = """
<table id="prices">
  <tr><th>Date</th><th>Price</th></tr>
  <tr><td>2025-01-01</td><td>$1,234.50</td><td><a href="/d/1">detail</a></td></tr>
  <tr><td>2025-01-02</td><td>1,300</td><td><a href="/d/2">detail</a></td></tr>
  <tr><td>2025-01-03</td><td>n/a</td><td><a href="/d/3">detail</a></td></tr>
</table>"""


def test_html_table_to_rows(site):
    site.routes["/p"] = (200, {}, PRICES_HTML)
    rows = run_program(program("""
        steps:
          - fetch: {{url: "{base}/p", as: page}}
          - extract:
              from: page
              rows: "table#prices tr:has(td)"
              fields:
                date: {{select: "td:nth-child(1)"}}
                price: {{select: "td:nth-child(2)", type: number}}
                link: {{select: "a", attr: href}}
                year: {{select: "td:nth-child(1)", regex: '^(\\d{{4}})', type: integer}}
              as: rows
          - emit: {{from: rows, add: {{source: "test"}}}}
    """, base=site.base))
    assert rows[0] == {"date": "2025-01-01", "price": 1234.5, "link": "/d/1", "year": 2025, "source": "test"}
    assert rows[1]["price"] == 1300
    assert rows[2]["price"] is None                      # "n/a": left for validation to catch
    assert len(rows) == 3


def test_json_with_wildcards_and_post_body(site):
    def handler(h):
        return (200, {}, {"data": {"series": [{"t": "2025-01", "v": "5"}, {"t": "2025-02", "v": 6}]}})
    site.routes["/api"] = handler
    rows = run_program(program("""
        vars: {{metal: sb}}
        steps:
          - fetch:
              url: "{base}/api"
              method: POST
              json: {{metal: "{{metal}}", range: 0}}
              as: doc
          - extract:
              from: doc
              rows: data.series.*
              fields:
                month: {{select: t}}
                value: {{select: v, type: number}}
                metal: {{value: "{{metal}}"}}
              as: out
          - emit: {{from: out}}
    """, base=site.base))
    assert rows == [{"month": "2025-01", "value": 5, "metal": "sb"},
                    {"month": "2025-02", "value": 6, "metal": "sb"}]
    assert '"metal": "sb"' in site.hits[0][3]            # the template was rendered into the body


def test_for_each_follows_links_and_scopes_variables(site):
    site.routes["/list"] = (200, {}, PRICES_HTML)
    for i in (1, 2, 3):
        site.routes[f"/d/{i}"] = (200, {}, f"<h1>Item {i}</h1><span class=sku>SKU-{i}</span>")
    rows = run_program(program("""
        steps:
          - fetch: {{url: "{base}/list", as: page}}
          - extract:
              from: page
              rows: "a"
              fields: {{href: {{attr: href}}}}
              as: links
          - for_each:
              in: links
              as: link
              steps:
                - fetch: {{url: "{base}{{link.href}}", as: detail}}
                - extract:
                    from: detail
                    fields: {{sku: {{select: ".sku"}}, title: {{select: h1}}}}
                    as: one
                - emit: {{from: one, add: {{href: "{{link.href}}"}}}}
    """, base=site.base))
    assert [r["sku"] for r in rows] == ["SKU-1", "SKU-2", "SKU-3"]
    assert rows[1] == {"sku": "SKU-2", "title": "Item 2", "href": "/d/2"}


def test_literal_for_each_list_and_text_regex(site):
    site.routes["/t"] = (200, {"Content-Type": "text/plain"}, "price=42.5 USD")
    rows = run_program(program("""
        steps:
          - for_each:
              in: [a, b]
              as: k
              steps:
                - fetch: {{url: "{base}/t?k={{k}}", as: txt}}
                - extract:
                    from: txt
                    fields: {{p: {{regex: 'price=([0-9.]+)', type: number}}}}
                    as: one
                - emit: {{from: one, add: {{k: "{{k}}"}}}}
    """, base=site.base))
    assert rows == [{"p": 42.5, "k": "a"}, {"p": 42.5, "k": "b"}]
    assert [h[1] for h in site.hits] == ["/t?k=a", "/t?k=b"]


# ---- safety --------------------------------------------------------------

def test_programs_cannot_run_code_or_use_unknown_steps():
    for bad in (
        {"steps": [{"exec": "import os"}]},
        {"steps": [{"fetch": {"url": "http://x", "as": "p"}, "eval": "1"}]},
        {"steps": [{"fetch": {"url": "http://x", "as": "p", "headers": {"Cookie": "a=b"}}}]},
        {"steps": [{"fetch": {"url": "http://x", "as": "p", "headers": {"Authorization": "x"}}}]},
        {"steps": [{"fetch": {"url": "http://x", "as": "bad name"}}]},
        {"steps": [{"fetch": {"url": "http://x", "as": "p", "method": "DELETE"}}]},
        {"steps": [{"fetch": {"url": "http://x", "as": "p", "method": "GET", "form": {"a": "b"}}}]},
        {"steps": [{"extract": {"from": "p", "as": "r", "fields": {"a": {"select": "x", "regex": "(" }}}}]},
        {"steps": [{"extract": {"from": "p", "as": "r", "fields": {"a": {"select": "x", "regex": "a" * 300}}}}]},
        {"steps": [{"extract": {"from": "p", "as": "r", "fields": {"a": {"value": "v", "select": "x"}}}}]},
        {"steps": []},
        {"steps": [{"emit": {"from": "r"}}], "script": "x"},
    ):
        with pytest.raises(ValidationError):
            parse_program(bad)


def test_nesting_and_step_count_limits():
    inner = {"emit": {"from": "r"}}
    deep = {"for_each": {"in": [1], "steps": [{"for_each": {"in": [1], "steps": [
        {"for_each": {"in": [1], "steps": [{"for_each": {"in": [1], "steps": [inner]}}]}}]}}]}}
    with pytest.raises(ValidationError, match="deeper"):
        parse_program({"steps": [deep]})
    with pytest.raises(ValidationError, match="steps"):
        parse_program({"steps": [inner] * (steps.MAX_STEPS + 1)})


def test_templates_only_look_up_variables_never_evaluate():
    scope = {"item": {"a": "x"}, "s": "text", "n": 3}
    assert render("{item.a}-{s}-{n}", scope) == "x-text-3"
    for attack in ("{item.__class__}", "{s.__class__}", "{item.a.upper}", "{__builtins__}", "{missing}"):
        with pytest.raises(StepError):
            render(attack, scope)
    with pytest.raises(StepError, match="plain value"):
        render("{item}", scope)
    assert render("{0} {x!r} {{}}", scope) == "{0} {x!r} {{}}"      # str.format syntax is inert


def test_fetches_outside_the_allowlist_fail_the_step(site):
    with pytest.raises(StepError, match=r"step 1 \(fetch\): blocked: .*not on the domain allowlist"):
        run_program(program("""
            steps:
              - fetch: {url: "https://example.org/", as: p}
        """))


def test_url_built_from_scraped_data_is_still_checked(site):
    site.routes["/p"] = (200, {}, '<a href="http://example.org/steal">x</a>')
    with pytest.raises(StepError, match="allowlist"):
        run_program(program("""
            steps:
              - fetch: {{url: "{base}/p", as: p}}
              - extract: {{from: p, rows: a, fields: {{u: {{attr: href}}}}, as: links}}
              - for_each:
                  in: links
                  as: l
                  steps:
                    - fetch: {{url: "{{l.u}}", as: q}}
        """, base=site.base))


def test_bad_css_selector_is_a_step_error_not_a_crash(site):
    site.routes["/p"] = (200, {}, "<p>x</p>")
    with pytest.raises(StepError, match="step 2 .*bad CSS selector"):
        run_program(program("""
            steps:
              - fetch: {{url: "{base}/p", as: p}}
              - extract: {{from: p, rows: "p[", fields: {{a: {{select: p}}}}, as: r}}
        """, base=site.base))


def test_http_errors_and_bad_json_name_the_step(site):
    with pytest.raises(StepError, match="step 1 .*HTTP 404"):
        run_program(program('steps: [{{fetch: {{url: "{base}/nope", as: p}}}}]', base=site.base))
    site.routes["/j"] = (200, {"Content-Type": "application/json"}, "{not json")
    with pytest.raises(StepError, match="not valid JSON"):
        run_program(program('steps: [{{fetch: {{url: "{base}/j", as: p}}}}]', base=site.base))


def test_limits_on_fetches_and_loop_size(site, monkeypatch):
    site.routes["/x"] = (200, {}, "<p>1</p>")
    monkeypatch.setattr(steps, "MAX_FETCHES", 2)
    with pytest.raises(StepError, match="more than 2 fetches"):
        run_program(program("""
            steps:
              - for_each:
                  in: [1, 2, 3]
                  steps:
                    - fetch: {{url: "{base}/x", as: p}}
        """, base=site.base))
    monkeypatch.setattr(steps, "MAX_LOOP_ITEMS", 2)
    with pytest.raises(StepError, match="limit 2"):
        run_program(program("""
            steps:
              - for_each:
                  in: [1, 2, 3]
                  steps:
                    - emit: {from: r}
        """))


def test_literal_urls_helper_skips_templated_ones():
    p = program("""
        steps:
          - fetch: {url: "https://a.com/x", as: p}
          - for_each:
              in: [1]
              steps:
                - fetch: {url: "https://b.com/{item}", as: q}
                - for_each:
                    in: [1]
                    steps:
                      - fetch: {url: "https://c.com/y", as: r}
    """)
    assert steps.literal_urls(p) == ["https://a.com/x", "https://c.com/y"]


# ---- through the real runner ---------------------------------------------

def declarative_job(site, out, **kw):
    data = {
        "job_name": "decl_job",
        "script": "scrapers.declarative",
        "schedule": "hourly",
        "output": {"type": "json", "path": str(out), "key": ["date"], "export": "all"},
        "retry": {"retries": 1, "retry_delay": 0},
        "validation": {"min_rows": 1, "required_fields": ["date", "price"], "types": {"price": "number"}},
        "timeout_seconds": 60,
        "parameters": {"steps": [
            {"fetch": {"url": f"{site.base}/p", "as": "page"}},
            {"extract": {"from": "page", "rows": "table#prices tr:has(td)", "as": "rows",
                         "fields": {"date": {"select": "td:nth-child(1)"},
                                    "price": {"select": "td:nth-child(2)", "type": "number"}}}},
            {"emit": {"from": "rows"}},
        ]},
    }
    data.update(kw)
    return JobConfig(**data)


def test_declarative_job_runs_through_the_runner(site, tmp_path):
    site.routes["/p"] = (200, {}, PRICES_HTML.replace("n/a", "1,400"))
    out = tmp_path / "out.json"
    assert execute_job(declarative_job(site, out, isolate=False)) is True
    assert len(json.loads(out.read_text(encoding="utf-8"))) == 3
    assert db.recent_runs("decl_job")[0]["status"] == "success"


def test_junk_values_fail_validation_before_anything_is_exported(site, tmp_path):
    site.routes["/p"] = (200, {}, PRICES_HTML)           # contains "n/a" -> price None
    out = tmp_path / "out.json"
    assert execute_job(declarative_job(site, out, isolate=False)) is False
    assert "'price'" in db.recent_runs("decl_job")[0]["error"]
    assert not out.exists()


def test_in_a_real_subprocess_the_unpatched_checks_refuse_loopback(site, tmp_path):
    # the child process doesn't inherit this test's patches, so it applies the real rules
    site.routes["/p"] = (200, {}, PRICES_HTML)
    assert execute_job(declarative_job(site, tmp_path / "out.json")) is False
    run = db.recent_runs("decl_job")[0]
    assert run["status"] == "failed" and "blocked" in run["error"]
    assert site.hits == []
