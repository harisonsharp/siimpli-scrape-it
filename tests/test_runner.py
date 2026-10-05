import pytest
import tempfile
import json
from pathlib import Path
from core.runner import execute_job
from schemas.job_schema import JobConfig

@pytest.fixture
def dummy_job_config(tmp_path):
    output_path = tmp_path / "out.json"
    data = {
        "job_name": "test_job",
        "script": "scrapers.dummy_test_scraper",
        "schedule": "hourly",
        "output": {"type": "json", "path": str(output_path)},
        "retry": {"retries": 1, "retry_delay": 0}
    }
    return JobConfig(**data)

def test_execute_valid_job(dummy_job_config, tmp_path):
    """Test executing a valid dynamically imported script."""
    # Write a dummy module
    scrapers_dir = Path("scrapers")
    scrapers_dir.mkdir(exist_ok=True)
    dummy_file = scrapers_dir / "dummy_test_scraper.py"
    
    dummy_code = """
def run(params=None):
    return [{"id": 1, "status": "ok"}]
"""
    dummy_file.write_text(dummy_code, encoding="utf-8")
    
    try:
        success = execute_job(dummy_job_config)
        assert success is True
        
        # Verify output was created
        out_path = Path(dummy_job_config.output.path)
        assert out_path.exists()
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["status"] == "ok"
    finally:
        if dummy_file.exists():
            dummy_file.unlink()

def test_execute_failing_job_catches_exception(dummy_job_config):
    """Test that if a script raises an exception, it is caught and false is returned."""
    scrapers_dir = Path("scrapers")
    scrapers_dir.mkdir(exist_ok=True)
    dummy_file = scrapers_dir / "dummy_test_error.py"
    
    dummy_code = """
def run(params=None):
    raise ValueError("Induced error")
"""
    dummy_file.write_text(dummy_code, encoding="utf-8")
    
    dummy_job_config.script = "scrapers.dummy_test_error"
    
    try:
        success = execute_job(dummy_job_config)
        assert success is False
    finally:
        if dummy_file.exists():
            dummy_file.unlink()
