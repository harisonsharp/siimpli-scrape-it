"""
Scheduler configuration and lifecycle management using APScheduler.
"""
from typing import List, Optional
import os
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ThreadPoolExecutor

from schemas.job_schema import JobConfig
from core.runner import execute_job

logger = logging.getLogger(__name__)

class ScraperScheduler:
    def __init__(self, db_url: str = "sqlite:///jobs.sqlite", max_workers: int = 4):
        jobstores = {
            'default': SQLAlchemyJobStore(
                url=db_url,
                engine_options={"connect_args": {"check_same_thread": False}} if db_url.startswith("sqlite") else {}
            )
        }
        executors = {
            'default': ThreadPoolExecutor(max_workers)
        }
        job_defaults = {
            'coalesce': False,
            'max_instances': 1
        }
        self.scheduler = BackgroundScheduler(
            jobstores=jobstores,
            executors=executors,
            job_defaults=job_defaults
        )
        
    def start(self):
        """Start the scheduler daemon."""
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("Scheduler started.")
        
    def shutdown(self):
        """Shutdown the scheduler daemon."""
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Scheduler shutdown.")
            
    def _parse_schedule(self, schedule_str: str) -> dict:
        """Parse string schedules into kwargs for APScheduler CronTrigger."""
        if schedule_str == "hourly":
            return {"minute": "0"}
        elif schedule_str == "daily":
            return {"hour": "0", "minute": "0"}
        else:
            # Assume cron format: minute hour day month day_of_week
            parts = schedule_str.split(" ")
            if len(parts) == 5:
                return {
                    "minute": parts[0],
                    "hour": parts[1],
                    "day": parts[2],
                    "month": parts[3],
                    "day_of_week": parts[4]
                }
            raise ValueError(f"Unsupported schedule format: {schedule_str}")
        
    def add_job_from_config(self, config: JobConfig):
        """Bind a JobConfig to the APScheduler."""
        trigger_kwargs = self._parse_schedule(config.schedule)
        
        # Check if job already exists
        if self.scheduler.get_job(config.job_name):
            logger.info(f"Updating existing job: {config.job_name}")
            self.scheduler.reschedule_job(
                config.job_name,
                trigger='cron',
                **trigger_kwargs
            )
        else:
            logger.info(f"Adding new job: {config.job_name}")
            self.scheduler.add_job(
                execute_job,
                trigger='cron',
                args=[config],
                id=config.job_name,
                replace_existing=True,
                **trigger_kwargs
            )
