"""Per-test database isolation.

Tests historically set ``os.environ["DATABASE_URL"]`` at module import then
did ``from app.db import engine``. Because ``app.db`` is imported once per
pytest process, every test ended up sharing whichever DB was bound first,
which let leftover rows from one test bleed into another.

This fixture builds a fresh SQLite DB per test, recreates the schema, and
rebinds the ``engine`` attribute on ``app.db`` plus every already-imported
test module that had captured the original engine via
``from app.db import engine``. Tests do not need to opt in -- the fixture
is autouse.

Direct ``python tests/test_*.py`` invocations still work because each test
file keeps its own module-level DATABASE_URL setup; pytest just overrides
that per test.
"""
from __future__ import annotations

import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    new_engine = create_engine(db_url, connect_args={"check_same_thread": False})

    # Make sure all model classes are registered with SQLModel.metadata.
    from app import models  # noqa: F401

    SQLModel.metadata.create_all(new_engine)

    from app import db as app_db
    original_engine = app_db.engine

    monkeypatch.setattr(app_db, "engine", new_engine, raising=False)
    for module in list(sys.modules.values()):
        if module is None or module is app_db:
            continue
        captured = getattr(module, "engine", None)
        if isinstance(captured, Engine) and captured is original_engine:
            monkeypatch.setattr(module, "engine", new_engine, raising=False)

    try:
        from app.config import get_settings

        get_settings.cache_clear()
    except Exception:
        pass

    try:
        yield new_engine
    finally:
        new_engine.dispose()
