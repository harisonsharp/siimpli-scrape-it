import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
from core.scheduler import ScraperScheduler
from schemas.job_schema import JobConfig

@pytest.fixture
def mock_job_config():
    data = {
        "job_name": "scheduler_test_job",
        "script": "scrapers.dummy",
        "schedule": "0 12 * * *", # noon every day
        "output": {"type": "json", "path": "/tmp/out.json"}
    }
    return JobConfig(**data)

def test_scheduler_initialization_with_in_memory_db():
    """Test that the scheduler initializes correctly with an in-memory SQLite store."""
    scheduler = ScraperScheduler(db_url="sqlite:///:memory:")
    
    assert isinstance(scheduler.scheduler, BackgroundScheduler)
    
    # Check max_workers configuration
    executors = scheduler.scheduler._executors
    assert "default" in executors
    assert isinstance(executors["default"], ThreadPoolExecutor)
    # The pool max workers should be 4 per requirements
    assert executors["default"]._pool._max_workers == 4
    
    # Check job store
    jobstores = scheduler.scheduler._jobstores
    assert "default" in jobstores
    assert isinstance(jobstores["default"], SQLAlchemyJobStore)

def test_add_job_from_config(mock_job_config):
    """Test adding a job from a JobConfig object."""
    app_scheduler = ScraperScheduler(db_url="sqlite:///:memory:")
    app_scheduler.start()
    
    try:
        app_scheduler.add_job_from_config(mock_job_config)
        
        jobs = app_scheduler.scheduler.get_jobs()
        assert len(jobs) == 1
        
        job = jobs[0]
        assert job.id == mock_job_config.job_name
        # The trigger should be a CronTrigger derived from "0 12 * * *"
        assert str(job.trigger) == "cron[month='*', day='*', day_of_week='*', hour='12', minute='0']"
    finally:
        app_scheduler.shutdown()
