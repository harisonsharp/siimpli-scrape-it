"""
Loads YAML into Pydantic models.
"""
import logging
import yaml
from pathlib import Path
from pydantic import ValidationError
from typing import Optional, List
from schemas.job_schema import JobConfig

logger = logging.getLogger(__name__)

def parse_yaml_string(yaml_content: str) -> Optional[JobConfig]:
    """Parse a YAML string into a JobConfig instance. Return None on invalid yaml without killing process."""
    try:
        data = yaml.safe_load(yaml_content)
        if not isinstance(data, dict):
            logger.warning("YAML parsed incorrectly: expected a dictionary")
            return None
        return JobConfig(**data)
    except yaml.YAMLError as e:
        logger.warning(f"YAML parsed incorrectly: {e}")
        return None
    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        return None

def load_config(file_path: str | Path) -> Optional[JobConfig]:
    """
    Parse a YAML configuration file into a JobConfig instance.
    """
    path = Path(file_path)
    if not path.exists():
        logger.error(f"Configuration file not found: {path}")
        return None
    try:
        content = path.read_text(encoding="utf-8")
        return parse_yaml_string(content)
    except Exception as e:
        logger.error(f"Error reading {path}: {e}")
        return None
