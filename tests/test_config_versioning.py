import os
import shutil
import pytest
from pathlib import Path
from utils.config_version import ConfigVersioner

@pytest.fixture
def temp_versions_dir(tmp_path):
    """Fixture to provide a temporary directory for config versions."""
    d = tmp_path / "versions"
    d.mkdir()
    return d

def test_snapshot_creation(temp_versions_dir):
    versioner = ConfigVersioner(versions_dir=str(temp_versions_dir))
    
    # Set a test env var
    os.environ["TEST_CONFIG_VAR"] = "value1"
    
    version_id = versioner.save_snapshot()
    
    assert version_id in versioner.list_versions()
    
    file_path = temp_versions_dir / f"{version_id}.json"
    assert file_path.exists()
    
    import json
    with open(file_path, "r") as f:
        data = json.load(f)
        assert data["TEST_CONFIG_VAR"] == "value1"

def test_hashing_consistency(temp_versions_dir):
    versioner = ConfigVersioner(versions_dir=str(temp_versions_dir))
    
    os.environ["TEST_HASH_VAR"] = "constant"
    
    # Snapshots with same env should have same hash part in version_id
    # (Timestamp will differ, but we can check the hash part)
    v1 = versioner.save_snapshot()
    v2 = versioner.save_snapshot()
    
    hash1 = v1.split("_")[-1]
    hash2 = v2.split("_")[-1]
    
    assert hash1 == hash2

def test_rollback(temp_versions_dir):
    versioner = ConfigVersioner(versions_dir=str(temp_versions_dir))
    
    # Save state 1
    os.environ["ROLLBACK_VAR"] = "state1"
    v1 = versioner.save_snapshot()
    
    # Change state
    os.environ["ROLLBACK_VAR"] = "state2"
    v2 = versioner.save_snapshot()
    
    assert os.environ["ROLLBACK_VAR"] == "state2"
    
    # Rollback to state 1
    success = versioner.rollback(v1)
    assert success is True
    assert os.environ["ROLLBACK_VAR"] == "state1"

def test_list_versions_ordering(temp_versions_dir):
    versioner = ConfigVersioner(versions_dir=str(temp_versions_dir))
    
    v1 = versioner.save_snapshot()
    import time
    time.sleep(1.1) # Ensure different timestamp
    v2 = versioner.save_snapshot()
    
    versions = versioner.list_versions()
    assert versions[0] == v2
    assert versions[1] == v1
