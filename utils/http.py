"""
Requests wrapper with retries.
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def get_with_retries(url: str, retries: int = 3, backoff_factor: float = 0.3, **kwargs):
    """
    Execute an HTTP GET request with automatic backoff and retries.
    
    Args:
        url (str): The URL to fetch.
        retries (int): Number of total retries around 502/503 errors.
        backoff_factor (float): The backoff multiplier.
        **kwargs: Additional arguments passed to requests.get().
        
    Returns:
        requests.Response: Successful response object.
    """
    session = requests.Session()
    
    retry_strategy = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    return session.get(url, **kwargs)
