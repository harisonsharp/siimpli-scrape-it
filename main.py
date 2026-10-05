"""
Typer CLI entrypoint for siimpli_scrape_it.
"""
import typer
import time
from pathlib import Path
from rich.console import Console
from rich.table import Table

from core.config import load_config
from core.logger import configure_logging
from core.runner import execute_job
from core.scheduler import ScraperScheduler
from utils.db import delete_results, query_results, recent_runs

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
    """Run a specific scraper job or all jobs immediately."""
    if not job_name and not all:
        console.print("[red]Error: Must specify a job_name or use --all flag[/red]")
        raise typer.Exit(code=1)
        
    jobs = get_all_jobs()
    
    if all:
        console.print(f"[bold green]Running all jobs ({len(jobs)} found)...[/bold green]")
        for job in jobs:
            execute_job(job)
        return
        
    target_job = next((j for j in jobs if j.job_name == job_name), None)
    if not target_job:
        console.print(f"[bold red]Job {job_name} not found[/bold red]")
        raise typer.Exit(code=1)
        
    console.print(f"[bold green]Executing {job_name} manually...[/bold green]")
    execute_job(target_job)

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
