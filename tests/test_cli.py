import pytest
from typer.testing import CliRunner
from main import app
from unittest.mock import patch, MagicMock
from schemas.job_schema import JobConfig

runner = CliRunner()

@pytest.fixture
def mock_job_config():
    return JobConfig(
        job_name="mock_job",
        schedule="hourly",
        script="scrapers.mock_scraper",
        output={"type": "json", "path": "mock_data"},
        retry={"retries": 3, "retry_delay": 5},
        parameters={"currency": "USD"}
    )

@patch("main.Path.glob")
@patch("main.load_config")
def test_list_command_empty(mock_load_config, mock_glob):
    mock_glob.return_value = []
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "No jobs found" in result.stdout

@patch("main.Path.glob")
@patch("main.load_config")
def test_list_command_with_jobs(mock_load_config, mock_glob, mock_job_config):
    mock_glob.return_value = [MagicMock()] # 1 file
    mock_load_config.return_value = mock_job_config
    
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "mock_job" in result.stdout
    assert "hourly" in result.stdout
    assert "scrapers.mock_scraper" in result.stdout

@patch("main.Path.glob")
@patch("main.load_config")
@patch("main.execute_job")
def test_run_command_all(mock_execute, mock_load_config, mock_glob, mock_job_config):
    mock_glob.return_value = [MagicMock()]
    mock_load_config.return_value = mock_job_config
    mock_execute.return_value = True
    
    result = runner.invoke(app, ["run", "--all"])
    assert result.exit_code == 0
    assert "Running all jobs" in result.stdout
    mock_execute.assert_called_once_with(mock_job_config)

@patch("main.Path.glob")
@patch("main.load_config")
@patch("main.execute_job")
def test_run_command_specific_job_found(mock_execute, mock_load_config, mock_glob, mock_job_config):
    mock_glob.return_value = [MagicMock()]
    mock_load_config.return_value = mock_job_config
    mock_execute.return_value = True
    
    result = runner.invoke(app, ["run", "mock_job"])
    assert result.exit_code == 0
    assert "Executing mock_job manually" in result.stdout
    mock_execute.assert_called_once_with(mock_job_config)

@patch("main.Path.glob")
@patch("main.load_config")
def test_run_command_specific_job_not_found(mock_load_config, mock_glob):
    mock_glob.return_value = []
    
    result = runner.invoke(app, ["run", "non_existent_job"])
    assert result.exit_code == 1
    assert "Job non_existent_job not found" in result.stdout

@patch("main.Path.glob")
@patch("main.load_config")
@patch("main.ScraperScheduler")
@patch("time.sleep")
def test_scheduler_command(mock_sleep, mock_scheduler_class, mock_load_config, mock_glob, mock_job_config):
    mock_glob.return_value = [MagicMock(), MagicMock()]
    mock_load_config.side_effect = [mock_job_config, None] # One valid, one invalid
    
    mock_scheduler_instance = MagicMock()
    mock_scheduler_class.return_value = mock_scheduler_instance
    
    # Make sleep raise KeyboardInterrupt to break out of infinite loop
    mock_sleep.side_effect = KeyboardInterrupt
    
    result = runner.invoke(app, ["scheduler"])
    
    # Process exits normally from KeyboardInterrupt handling
    assert result.exit_code == 0
    assert "Starting scheduler daemon" in result.stdout
    assert "Shutting down" in result.stdout
    
    # Verify job added and scheduler started
    mock_scheduler_instance.add_job_from_config.assert_called_once_with(mock_job_config)
    mock_scheduler_instance.start.assert_called_once()
    mock_scheduler_instance.shutdown.assert_called_once()

