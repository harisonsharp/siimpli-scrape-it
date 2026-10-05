"""
Typer CLI entrypoint for siimpli_scrape_it.
"""
import typer
import time
from pathlib import Path
from rich.console import Console
from rich.table import Table

from core.config import load_config
from core.runner import execute_job
from core.scheduler import ScraperScheduler

app = typer.Typer(help="siimpli_scrape_it Daemon & CLI Tool")
console = Console()

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
    
    try:
        # Keep the main thread alive while background scheduler runs
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        console.print("\n[bold red]Shutting down scheduler...[/bold red]")
        scheduler.shutdown()

if __name__ == "__main__":
    app()
