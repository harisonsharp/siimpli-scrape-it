"""
Isolated script runner.
"""
from typing import Dict, Any, List
import importlib
import time
import logging
from schemas.job_schema import JobConfig
from utils.storage import save_data

logger = logging.getLogger(__name__)

def execute_job(config: JobConfig) -> bool:
    """Execute a scraper job specified by JobConfig.
    Returns True on success, False on failure.
    """
    max_attempts = max(1, config.retry.retries)
    attempt = 0
    
    while attempt < max_attempts:
        attempt += 1
        start_time = time.perf_counter()
        
        try:
            logger.info(f"Executing job: {config.job_name} (Attempt {attempt}/{max_attempts})")
            
            # 3. Use importlib to load script
            module = importlib.import_module(config.script)
            
            # 5. Execute module.run
            if hasattr(module, "run"):
                data = module.run(params=config.parameters)
            else:
                raise AttributeError(f"Module {config.script} missing 'run' function")
                
            # 6. Validate output
            if not isinstance(data, list):
                raise ValueError(f"Expected List[Dict] output, got {type(data)}")
                
            runtime = time.perf_counter() - start_time
            item_count = len(data)
            
            # 7. Pass data to storage
            if save_data(data, config.output):
                # 8. Log success
                logger.info(f"Job {config.job_name} SUCCESS in {runtime:.2f}s, scraped {item_count} items")
                return True
            else:
                raise RuntimeError("Failed to save data")
                
        except Exception as e:
            runtime = time.perf_counter() - start_time
            logger.error(f"Job {config.job_name} FAILED in {runtime:.2f}s: {str(e)}")
            
            if attempt < max_attempts:
                logger.info(f"Retrying in {config.retry.retry_delay}s...")
                time.sleep(config.retry.retry_delay)
                
    return False
