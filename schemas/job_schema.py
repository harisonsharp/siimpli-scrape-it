"""
Pydantic models for Jobs.
"""
from pydantic import BaseModel, Field
from typing import Optional, Literal, Dict, Any

class OutputConfig(BaseModel):
    """Configuration for data output."""
    type: Literal["csv", "json"]
    path: str

class RetryConfig(BaseModel):
    """Configuration for script retries."""
    retries: int = Field(default=3, ge=0)
    retry_delay: int = Field(default=60, ge=0) # in seconds

class RateLimitConfig(BaseModel):
    """Configuration for rate limiting."""
    rate_limit: int = Field(default=10) # requests per minute
    delay_min: int = Field(default=2)
    delay_max: int = Field(default=5)

class JobConfig(BaseModel):
    """Main job configuration defining script, schedule, and rules."""
    job_name: str
    script: str
    schedule: str
    output: OutputConfig
    retry: RetryConfig = Field(default_factory=RetryConfig)
    rate_limit: Optional[RateLimitConfig] = None
    parameters: Optional[Dict[str, Any]] = None
