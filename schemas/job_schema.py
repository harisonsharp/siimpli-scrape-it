"""
Pydantic models for Jobs.
"""
from pydantic import BaseModel, Field
from typing import Optional, Literal, Dict, Any, List

class OutputConfig(BaseModel):
    """Configuration for data output."""
    type: Literal["csv", "json"]
    path: str
    # Fields that uniquely identify a row (e.g. ["date", "metal"]). When set,
    # the results database upserts on them so history accumulates without
    # duplicates; when unset every run's rows are stored as new.
    key: Optional[List[str]] = None
    # "latest": the file holds the last run's rows (original behaviour).
    # "all": the file is rebuilt from every row stored for the job.
    export: Literal["latest", "all"] = "latest"

class RetryConfig(BaseModel):
    """Configuration for script retries."""
    retries: int = Field(default=3, ge=0)
    retry_delay: int = Field(default=60, ge=0) # in seconds

class RateLimitConfig(BaseModel):
    """Configuration for rate limiting."""
    rate_limit: int = Field(default=10) # requests per minute
    delay_min: int = Field(default=2)
    delay_max: int = Field(default=5)
    # Skip pages disallowed by the site's robots.txt (checked per navigation / request)
    respect_robots: bool = False

class ValidationConfig(BaseModel):
    """Expectations a scraper's rows must meet; a violation fails the run
    before anything is stored or exported."""
    min_rows: Optional[int] = Field(default=None, ge=0)
    # Fields every row must have, non-null and non-blank
    required_fields: List[str] = Field(default_factory=list)
    # Expected JSON type per field, checked where the value is present and non-null
    types: Dict[str, Literal["string", "number", "integer", "boolean"]] = Field(default_factory=dict)

class NotifyConfig(BaseModel):
    """Webhook notification settings (JSON POST)."""
    webhook_url: str
    on_failure: bool = True
    on_empty: bool = True

class JobConfig(BaseModel):
    """Main job configuration defining script, schedule, and rules."""
    job_name: str
    script: str
    schedule: str
    output: OutputConfig
    retry: RetryConfig = Field(default_factory=RetryConfig)
    rate_limit: Optional[RateLimitConfig] = None
    notify: Optional[NotifyConfig] = None
    validation: Optional[ValidationConfig] = None
    # Run the script in a child process that is killed after this many seconds
    # (None = no limit). Applies per attempt; only enforced when isolate is true.
    timeout_seconds: Optional[int] = Field(default=3600, gt=0)
    # false runs the script inside the runner's own process (no timeout) —
    # useful for debugging with a debugger or breakpoints.
    isolate: bool = True
    parameters: Optional[Dict[str, Any]] = None
