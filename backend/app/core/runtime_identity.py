"""Safe, cached runtime identity for deployment-parity diagnostics."""

from functools import lru_cache
from datetime import UTC, datetime
from pathlib import Path
import os
import subprocess

from app.core.config import ROOT


APP_VERSION = "0.1.0"
APP_STARTED_AT_UTC = datetime.now(UTC).isoformat()


@lru_cache(maxsize=1)
def runtime_identity() -> dict[str, str]:
    runtime_path = str(Path(ROOT).resolve())
    commit = os.getenv("APP_GIT_COMMIT", "").strip()
    if not commit:
        try:
            result = subprocess.run(
                ["git", "-C", str(Path(ROOT).resolve().parent), "rev-parse", "--short", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
            commit = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            commit = "unknown"
    return {
        "git_commit": commit or "unknown",
        "runtime_path": runtime_path,
        "app_version": APP_VERSION,
        "process_id": os.getpid(),
        "started_at_utc": APP_STARTED_AT_UTC,
    }
