"""
Custom error classes (JobFailedError, ConfigError).
"""

class JobFailedError(Exception):
    """Raised when a scraping script fails during execution."""
    pass

class ConfigError(Exception):
    """Raised when parsing or validating configurations fails."""
    pass
