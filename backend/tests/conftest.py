"""Test setup: isolated SQLite DB and no real API keys. Must run before `app` is imported."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="researchflow-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.replace(os.sep, '/')}/test.db"
for _key in ("ANTHROPIC_API_KEY", "TAVILY_API_KEY", "BRAVE_API_KEY"):
    os.environ[_key] = ""  # env vars win over .env, so a developer's real keys are never used by tests
os.environ["RATE_LIMIT_PER_MINUTE"] = "1000"

from app.database import init_db  # noqa: E402

init_db()
