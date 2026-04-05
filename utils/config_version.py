import os
import json
import hashlib
import logging
from datetime import datetime, UTC
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("config_versioner")

class ConfigVersioner:
    """
    Handles configuration versioning by snapshotting environment variables.
    Optimized for HFT to ensure traceability of runtime parameters.
    """
    
    def __init__(self, versions_dir: str = "config/versions"):
        self.versions_dir = Path(versions_dir)
        self.versions_dir.mkdir(parents=True, exist_ok=True)

    def _get_env_snapshot(self) -> Dict[str, str]:
        """Returns a sorted dictionary of current environment variables."""
        return {k: v for k, v in sorted(os.environ.items())}

    def _calculate_hash(self, env_dict: Dict[str, str]) -> str:
        """Calculates a stable SHA-256 hash of the environment dictionary."""
        env_str = json.dumps(env_dict, sort_keys=True)
        return hashlib.sha256(env_str.encode("utf-8")).hexdigest()

    def save_snapshot(self) -> str:
        """
        Saves current environment variables to a versioned JSON file.
        Returns the version ID (timestamp_hash).
        """
        env_snapshot = self._get_env_snapshot()
        config_hash = self._calculate_hash(env_snapshot)
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        version_id = f"{timestamp}_{config_hash[:12]}"
        
        file_path = self.versions_dir / f"{version_id}.json"
        
        # Avoid redundant writes if nothing changed (optional, but good for HFT)
        # For now, we follow the task to write it.
        
        with open(file_path, "w") as f:
            json.dump(env_snapshot, f, indent=2)
            
        logger.info(f"Config snapshot saved: {version_id}")
        return version_id

    def list_versions(self) -> List[str]:
        """Lists available configuration version IDs."""
        files = sorted(self.versions_dir.glob("*.json"), reverse=True)
        return [f.stem for f in files]

    def rollback(self, version_id: str) -> bool:
        """
        Restores environment variables from a specific snapshot.
        Note: This only affects the current process environment.
        """
        file_path = self.versions_dir / f"{version_id}.json"
        if not file_path.exists():
            logger.error(f"Version {version_id} not found at {file_path}")
            return False
            
        try:
            with open(file_path, "r") as f:
                env_snapshot = json.load(f)
                
            # Clear current env and restore from snapshot
            # WARNING: This might remove variables set by the shell but not in snapshot
            # For safety in HFT, we update rather than clear, or clear selectively.
            # The task says "restore environment variables from a snapshot".
            
            # os.environ.clear() # Dangerous if we need PATH, etc.
            for k, v in env_snapshot.items():
                os.environ[k] = v
                
            logger.info(f"Config rolled back to version: {version_id}")
            return True
        except Exception as e:
            logger.error(f"Rollback failed: {e}")
            return False

    def auto_snapshot(self) -> str:
        """Automated snapshot for startup sequence."""
        version_id = self.save_snapshot()
        print(f"--- CONFIG VERSION: {version_id} ---")
        return version_id
