import pytest
import tempfile
from pathlib import Path
import json
import csv
from utils.storage import save_data
from schemas.job_schema import OutputConfig

@pytest.fixture
def sample_data():
    return [
        {"id": 1, "name": "Alice", "score": 95},
        {"id": 2, "name": "Bob", "score": 82},
        {"id": 3, "name": "Charlie", "score": 88}
    ]

def test_save_data_json(sample_data):
    """Test saving list of dicts to a JSON file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / "out.json"
        config = OutputConfig(type="json", path=str(out_path))
        
        success = save_data(sample_data, config)
        
        assert success is True
        assert out_path.exists()
        
        # Verify written data
        written_data = json.loads(out_path.read_text(encoding="utf-8"))
        assert written_data == sample_data

def test_save_data_csv(sample_data):
    """Test saving list of dicts to a CSV file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / "out.csv"
        config = OutputConfig(type="csv", path=str(out_path))
        
        success = save_data(sample_data, config)
        
        assert success is True
        assert out_path.exists()
        
        # Verify written data
        with open(out_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            written_data = list(reader)
            
            # Note: CSV stringifies everything
            assert len(written_data) == 3
            assert written_data[0]["id"] == "1"
            assert written_data[0]["name"] == "Alice"
            assert written_data[0]["score"] == "95"

def test_save_data_empty_list():
    """Test behavior when given an empty list."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / "out.json"
        config = OutputConfig(type="json", path=str(out_path))
        
        success = save_data([], config)
        assert success is True
        
        written_data = json.loads(out_path.read_text(encoding="utf-8"))
        assert written_data == []
