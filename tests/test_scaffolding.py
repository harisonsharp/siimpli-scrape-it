"""
Test structural integrity of the project scaffolding.
"""
import os
from pathlib import Path

def test_project_structure_exists():
    """
    Verify that the required directories and files exist as defined in Phase 0.
    """
    base_dir = Path(__file__).parent.parent
    
    # Directories
    assert (base_dir / "core").is_dir()
    assert (base_dir / "schemas").is_dir()
    assert (base_dir / "utils").is_dir()
    assert (base_dir / "scrapers").is_dir()
    assert (base_dir / "jobs").is_dir()
    
    # Files
    assert (base_dir / "pyproject.toml").is_file()
    assert (base_dir / "main.py").is_file()
    
    assert (base_dir / "core" / "config.py").is_file()
    assert (base_dir / "core" / "runner.py").is_file()
    assert (base_dir / "core" / "scheduler.py").is_file()
    assert (base_dir / "core" / "logger.py").is_file()
    assert (base_dir / "core" / "exceptions.py").is_file()
    
    assert (base_dir / "schemas" / "job_schema.py").is_file()
    assert (base_dir / "schemas" / "output_schema.py").is_file()
    
    assert (base_dir / "utils" / "http.py").is_file()
    assert (base_dir / "utils" / "browser.py").is_file()
    assert (base_dir / "utils" / "storage.py").is_file()

def test_pyproject_toml_has_dependencies():
    """
    Ensure pyproject.toml is not empty and has some basic config.
    """
    pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
    with open(pyproject_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    assert "siimpli_scrape_it" in content
    assert "pytest" in content
    assert "pydantic" in content
