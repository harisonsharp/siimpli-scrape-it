import pytest
from pydantic import ValidationError
from schemas.job_schema import JobConfig, OutputConfig, RetryConfig, RateLimitConfig

def test_valid_job_config():
    """Test that a fully valid JobConfig structure passes validation."""
    data = {
        "job_name": "test_job",
        "script": "scrapers.test_script",
        "schedule": "0 3 * * *",
        "output": {
            "type": "csv",
            "path": "/tmp/output.csv"
        },
        "retry": {"retries": 5, "retry_delay": 120},
        "rate_limit": {"rate_limit": 20, "delay_min": 1, "delay_max": 2},
        "parameters": {"url": "http://example.com"}
    }
    config = JobConfig(**data)
    assert config.job_name == "test_job"
    assert config.output.type == "csv"
    assert config.retry.retries == 5
    assert config.rate_limit.rate_limit == 20
    assert config.parameters["url"] == "http://example.com"

def test_default_constraints():
    """Test that JobConfig sets correct default values when optional fields are omitted."""
    data = {
        "job_name": "minimal_job",
        "script": "scrapers.minimal",
        "schedule": "hourly",
        "output": {
            "type": "json",
            "path": "/data/out.json"
        }
    }
    config = JobConfig(**data)
    
    # Check default retry config
    assert config.retry.retries == 3
    assert config.retry.retry_delay == 60
    
    # Check optional fields default to None
    assert config.rate_limit is None
    assert config.parameters is None

def test_invalid_structure_raises_validation_error():
    """Test that invalid structures correctly raise ValidationError."""
    # Missing required field 'script'
    data_missing_script = {
        "job_name": "bad_job",
        "schedule": "hourly",
        "output": {"type": "json", "path": "/tmp/1"}
    }
    with pytest.raises(ValidationError):
        JobConfig(**data_missing_script)

    # Invalid literal for output type
    data_bad_output_type = {
        "job_name": "job2",
        "script": "s",
        "schedule": "daily",
        "output": {"type": "xml", "path": "/tmp/2"} # xml is invalid
    }
    with pytest.raises(ValidationError):
        JobConfig(**data_bad_output_type)
        
    # Invalid constraint (negative retries)
    data_negative_retry = {
        "job_name": "job3",
        "script": "s",
        "schedule": "daily",
        "output": {"type": "csv", "path": "/tmp/3"},
        "retry": {"retries": -1, "retry_delay": 10} # < 0 is invalid
    }
    with pytest.raises(ValidationError):
        JobConfig(**data_negative_retry)
