"""Test-mode startup must never fall through to the existing demo database."""

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
DEMO_DATABASE = BACKEND / "data" / "zmt_enterprise.db"


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _fresh_import(database_url: str | None, *, start_lifespan: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["APP_ENV"] = "test"
    env["DEMO_IDENTITY_ENABLED"] = "true"
    if database_url is None:
        env.pop("DATABASE_URL", None)
    else:
        env["DATABASE_URL"] = database_url
    script = (
        "from fastapi.testclient import TestClient\nfrom app.main import app\n"
        "with TestClient(app) as client:\n"
        "    assert client.get('/api/v1/health').status_code == 200\n"
        if start_lifespan else "import app.db.session; import app.main"
    )
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script],
        cwd=BACKEND, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


def test_test_startup_requires_explicit_database_before_engine_import():
    before = _digest(DEMO_DATABASE) if DEMO_DATABASE.exists() else None
    result = _fresh_import(None)
    assert result.returncode != 0
    assert "TEST_DATABASE_URL_REQUIRED" in result.stderr
    assert (_digest(DEMO_DATABASE) if DEMO_DATABASE.exists() else None) == before


def test_test_startup_rejects_demo_database_even_when_explicit():
    before = _digest(DEMO_DATABASE) if DEMO_DATABASE.exists() else None
    result = _fresh_import(f"sqlite+aiosqlite:///{DEMO_DATABASE.as_posix()}")
    assert result.returncode != 0
    assert "TEST_DATABASE_PATH_FORBIDDEN" in result.stderr
    assert (_digest(DEMO_DATABASE) if DEMO_DATABASE.exists() else None) == before


def test_test_startup_accepts_only_memory_or_isolated_temp_path(tmp_path):
    memory = _fresh_import("sqlite+aiosqlite:///:memory:")
    assert memory.returncode == 0, memory.stderr
    outside = _fresh_import(f"sqlite+aiosqlite:///{(tmp_path / 'outside.db').as_posix()}")
    assert outside.returncode != 0
    assert "TEST_DATABASE_PATH_FORBIDDEN" in outside.stderr
    root = Path(tempfile.gettempdir()) / "ozon-agent-tests"
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        database = Path(directory) / "synthetic.db"
        isolated = _fresh_import(f"sqlite+aiosqlite:///{database.as_posix()}")
        assert isolated.returncode == 0, isolated.stderr
        assert not database.exists()  # Module import does not open or initialize it.


def test_isolated_lifespan_initializes_only_explicit_temp_database():
    before = _digest(DEMO_DATABASE) if DEMO_DATABASE.exists() else None
    root = Path(tempfile.gettempdir()) / "ozon-agent-tests"
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        database = Path(directory) / "synthetic.db"
        result = _fresh_import(f"sqlite+aiosqlite:///{database.as_posix()}", start_lifespan=True)
        assert result.returncode == 0, result.stderr
        assert database.exists()
        assert (_digest(DEMO_DATABASE) if DEMO_DATABASE.exists() else None) == before
