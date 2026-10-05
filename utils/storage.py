"""
Storage utilities for saving scraped data.
"""
from typing import List, Dict, Any
import csv
import json
from pathlib import Path
from schemas.job_schema import OutputConfig
import logging

logger = logging.getLogger(__name__)

def save_data(data: List[Dict[str, Any]], config: OutputConfig) -> bool:
    """Save a list of dictionaries to the destination specified in OutputConfig.
    Returns True on success, False on failure.
    """
    try:
        path = Path(config.path)
        # Ensure parent directories exist
        path.parent.mkdir(parents=True, exist_ok=True)
        
        if config.type == "json":
            _save_json(data, path)
        elif config.type == "csv":
            _save_csv(data, path)
        else:
            logger.error(f"Unsupported output type: {config.type}")
            return False
            
        return True
    except Exception as e:
        logger.error(f"Failed to save data to {config.path}: {e}")
        return False

def _save_json(data: List[Dict[str, Any]], path: Path) -> None:
    with open(path, mode="w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def _save_csv(data: List[Dict[str, Any]], path: Path) -> None:
    if not data:
        path.write_text("", encoding="utf-8")
        return
        
    # Assume all dicts have the same keys as the first one
    keys = data[0].keys()
    with open(path, mode="w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(data)
