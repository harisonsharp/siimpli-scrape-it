import pytest
import yaml
from core.config import parse_yaml_string, load_config
from schemas.job_schema import JobConfig

def test_parse_valid_yaml_string():
    """Test parsing a valid YAML string into a JobConfig."""
    yaml_content = """
job_name: valid_job
script: scrapers.dummy
schedule: hourly
output:
  type: json
  path: /tmp/out.json
"""
    config = parse_yaml_string(yaml_content)
    assert isinstance(config, JobConfig)
    assert config.job_name == "valid_job"

def test_parse_invalid_yaml_string_syntax_error(caplog):
    """Test parsing malformed YAML syntax logs a warning and returns None."""
    yaml_content = """
job_name: [unclosed list
script: missing_quote"
"""
    config = parse_yaml_string(yaml_content)
    assert config is None
    assert "YAML parsed incorrectly" in caplog.text

def test_parse_valid_yaml_invalid_schema(caplog):
    """Test parsing valid YAML but missing required fields logs an error and returns None."""
    yaml_content = """
job_name: validation_fail
# missing script
schedule: hourly
output:
  type: invalid_type
  path: /tmp/out.json
"""
    config = parse_yaml_string(yaml_content)
    assert config is None
    assert "Validation error" in caplog.text
