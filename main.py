"""
Typer CLI entrypoint for siimpli_scrape_it.
"""
import json
import typer
import time
from pathlib import Path
from rich.console import Console
from rich.table import Table

from core.config import load_config
from core.drafts import DraftError, DraftStore
from core.logger import configure_logging
from core.runner import execute_job
from core.scheduler import ScraperScheduler
from utils.db import delete_results, load_results, query_results, recent_runs

app = typer.Typer(help="siimpli_scrape_it Daemon & CLI Tool")
console = Console()


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug-level logging")):
    """siimpli_scrape_it Daemon & CLI Tool"""
    configure_logging("DEBUG" if verbose else None)


def get_all_jobs():
    """Helper to load all valid jobs from jobs directory."""
    jobs_dir = Path("jobs")
    if not jobs_dir.exists():
        return []
    
    jobs = []
    for yaml_file in jobs_dir.glob("*.yaml"):
        config = load_config(yaml_file)
        if config:
            jobs.append(config)
    return jobs

@app.command("list")
def list_jobs():
    """List all configured scraper jobs."""
    jobs = get_all_jobs()
    
    if not jobs:
        console.print("[yellow]No jobs found in jobs directory.[/yellow]")
        return
        
    table = Table(title="Configured Jobs")
    table.add_column("Job Name", style="cyan", no_wrap=True)
    table.add_column("Schedule", style="magenta")
    table.add_column("Script", style="green")
    
    for job in jobs:
        table.add_row(job.job_name, job.schedule, job.script)
        
    console.print(table)

@app.command("run")
def run_job(job_name: str = typer.Argument(None, help="Name of the job to run"), all: bool = typer.Option(False, "--all", help="Run all available jobs")):
    """Run a specific scraper job or all jobs immediately.

    Exits 1 if a job fails, so a caller such as the siimpli monthly jobs can tell.
    """
    if not job_name and not all:
        console.print("[red]Error: Must specify a job_name or use --all flag[/red]")
        raise typer.Exit(code=1)

    jobs = get_all_jobs()

    if all:
        console.print(f"[bold green]Running all jobs ({len(jobs)} found)...[/bold green]")
        failed = [job.job_name for job in jobs if not execute_job(job)]
        if failed:
            console.print(f"[bold red]Failed: {', '.join(failed)}[/bold red]")
            raise typer.Exit(code=1)
        return

    target_job = next((j for j in jobs if j.job_name == job_name), None)
    if not target_job:
        console.print(f"[bold red]Job {job_name} not found[/bold red]")
        raise typer.Exit(code=1)

    console.print(f"[bold green]Executing {job_name} manually...[/bold green]")
    if not execute_job(target_job):
        console.print(f"[bold red]{job_name} failed[/bold red] (see: siimpli_scrape_it status {job_name})")
        raise typer.Exit(code=1)

@app.command("export")
def export_results(
    job_name: str = typer.Argument(..., help="Job whose stored results to export"),
    out: Path = typer.Option(..., "--out", "-o", help="JSON file to write"),
):
    """Write every stored row of a job to a JSON file (a list of objects).

    This reads the results database, not the job's output file, so it works the
    same whatever the job's output type and path are. The siimpli monthly jobs
    load scraped metal prices this way.
    """
    rows = load_results(job_name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, default=str), encoding="utf-8")
    console.print(f"Wrote {len(rows)} row(s) for {job_name} to {out}")

@app.command("status")
def show_status(
    job_name: str = typer.Argument(None, help="Only show runs for this job"),
    limit: int = typer.Option(15, "--limit", "-n", help="Number of runs to show"),
):
    """Show recent job runs (status, duration, items, errors)."""
    runs = recent_runs(job_name, limit)
    if not runs:
        console.print("[yellow]No runs recorded yet.[/yellow]")
        return

    colors = {"success": "green", "empty": "yellow", "failed": "red", "running": "blue"}
    table = Table(title="Recent Runs")
    for col in ("ID", "Job", "Started (UTC)", "Status", "Secs", "Tries", "Items", "Error"):
        table.add_column(col)
    for r in runs:
        color = colors.get(r["status"], "white")
        table.add_row(
            str(r["id"]),
            r["job_name"],
            r["started_at"].replace("T", " ").replace("+00:00", ""),
            f"[{color}]{r['status']}[/{color}]",
            f"{r['duration_s']:.1f}" if r["duration_s"] is not None else "-",
            str(r["attempts"]),
            str(r["item_count"]) if r["item_count"] is not None else "-",
            (r["error"] or "")[:60],
        )
    console.print(table)

@app.command("purge")
def purge_results(
    job_name: str = typer.Argument(..., help="Job whose stored results should be deleted"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
):
    """Delete a job's stored results (run history is kept).

    Use after changing a job's output.key or the shape of its rows, so old rows
    don't get exported alongside the new ones.
    """
    total, _ = query_results(job_name, limit=0)
    if total == 0:
        console.print(f"[yellow]No stored results for {job_name}.[/yellow]")
        return
    if not yes:
        typer.confirm(f"Delete {total} stored row(s) for {job_name}?", abort=True)
    removed = delete_results(job_name)
    console.print(f"[green]Deleted {removed} stored row(s) for {job_name}.[/green]")
    console.print("Its output file is rebuilt from the database on the next run.")

@app.command("scheduler")
def start_scheduler():
    """Start the APScheduler background daemon."""
    jobs = get_all_jobs()
    scheduler = ScraperScheduler()
    
    for job in jobs:
        scheduler.add_job_from_config(job)
        
    console.print(f"[bold blue]Starting scheduler daemon with {len(jobs)} jobs...[/bold blue]")
    console.print("[yellow]Press Ctrl+C to exit.[/yellow]")
    
    scheduler.start()
    # Drop persisted jobs whose YAML was deleted (needs the store loaded, so after start)
    scheduler.remove_stale_jobs(j.job_name for j in jobs)
    
    try:
        # Keep the main thread alive while background scheduler runs
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        console.print("\n[bold red]Shutting down scheduler...[/bold red]")
        scheduler.shutdown()

drafts_app = typer.Typer(help="Review agent-written job drafts (jobs/drafts/). Approval is only possible here, never over MCP.")
app.add_typer(drafts_app, name="draft")


def _drafts() -> DraftStore:
    return DraftStore("jobs")


def _guard(fn, *args):
    try:
        return fn(*args)
    except DraftError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)


@drafts_app.command("list")
def draft_list():
    """List drafts and the result of their last dry run."""
    rows = _drafts().list()
    if not rows:
        console.print("[yellow]No drafts.[/yellow]")
        return
    for d in rows:
        t = d["last_test"] or {}
        console.print(f"{d['name']}  author={d['author']}  last test: {t.get('status', 'never run')}"
                      + (f" ({t['item_count']} rows)" if t.get("item_count") is not None else ""))


@drafts_app.command("show")
def draft_show(name: str = typer.Argument(...)):
    """Print a draft's YAML, test status and sample rows."""
    d = _guard(_drafts().get, name)
    console.print(d["yaml"], markup=False)
    t = d["last_test"] or {}
    console.print(f"Last test: {t.get('status', 'never run')}; this exact version passed: {d['tested_current_version']}")
    if t.get("error"):
        console.print(f"Error: {t['error']}", markup=False)
    for row in d["sample"][:5]:
        console.print(str(row), markup=False)


@drafts_app.command("test")
def draft_test(name: str = typer.Argument(...)):
    """Dry-run a draft through the production runner (scratch output, no webhook)."""
    result = _guard(_drafts().run_test, name)
    color = "green" if result["status"] == "success" else "red"
    console.print(f"[{color}]{result['status']}[/{color}] run {result['run_id']}, {result['item_count']} rows")
    if result["error"]:
        console.print(result["error"], markup=False)
    raise typer.Exit(code=0 if result["status"] == "success" else 1)


@drafts_app.command("approve")
def draft_approve(
    name: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
):
    """Promote a draft that passed a dry run into jobs/ (the scheduler picks it up on restart)."""
    store = _drafts()
    d = _guard(store.get, name)
    console.print(d["yaml"], markup=False)
    if not d["tested_current_version"]:
        console.print("[red]This exact version has no successful dry run. Run: draft test " + name + "[/red]")
        raise typer.Exit(code=1)
    for row in d["sample"][:5]:
        console.print(str(row), markup=False)
    if not yes:
        typer.confirm(f"Approve {name} as a live job?", abort=True)
    path = _guard(store.approve, name)
    console.print(f"[green]Approved: {path}[/green] (restart the scheduler to schedule it)")


@drafts_app.command("reject")
def draft_reject(name: str = typer.Argument(...)):
    """Delete a draft and its dry-run rows."""
    _guard(_drafts().delete, name)
    console.print(f"Deleted draft {name}.")


@app.command("mcp")
def start_mcp(
    project_dir: str = typer.Option(
        None, "--project-dir", "-C",
        help="Directory containing jobs/ and the data paths (default: current directory)",
    ),
):
    """Start the MCP server (stdio) so an AI agent can list/run jobs and read results."""
    try:
        from core.mcp_server import serve
    except ImportError:
        console.print("[red]The MCP SDK is not installed. Run: pip install -e \".[mcp]\"[/red]")
        raise typer.Exit(code=1)
    serve(project_dir)

if __name__ == "__main__":
    app()
