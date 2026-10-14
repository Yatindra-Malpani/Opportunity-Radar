import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    for k in ("GOOGLE_CSE_KEY", "GOOGLE_CSE_CX", "TAVILY_API_KEY", "BRAVE_API_KEY", "SERPER_API_KEY",
              "PROVIDER_ORDER"):
        monkeypatch.delenv(k, raising=False)
    db.init_db()
    yield
