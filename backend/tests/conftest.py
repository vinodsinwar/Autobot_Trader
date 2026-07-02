import os
import uuid

import pytest

# test bootstrap env — must be set before app.core.config is imported anywhere
os.environ.setdefault(
    "AUTOBOT_MASTER_KEY", "zH9x3T5X0mUj8bJb0Yl0lqrmXAeuIxOMx4B3P8T4Qhc="
)


@pytest.fixture
async def db(tmp_path, monkeypatch):
    """Fresh SQLite database per test, patched into the app session factory."""
    from app.core.config import get_settings
    from app.db import session as db_session

    db_file = tmp_path / f"test-{uuid.uuid4().hex}.db"
    monkeypatch.setenv("AUTOBOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    await db_session.dispose_db()
    await db_session.init_db()
    yield db_session
    await db_session.dispose_db()
    get_settings.cache_clear()
