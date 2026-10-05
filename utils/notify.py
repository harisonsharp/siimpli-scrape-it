"""
Webhook notifications for job outcomes.
"""
import logging
from typing import Any, Dict

import requests

logger = logging.getLogger(__name__)


def send_webhook(url: str, payload: Dict[str, Any]) -> bool:
    """POST ``payload`` as JSON.  Never raises; returns True on a 2xx response."""
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Webhook notification to {url} failed: {e}")
        return False
