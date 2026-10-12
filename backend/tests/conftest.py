"""Test fixtures: a real Postgres database migrated with Alembic, truncated between tests."""

import os
from collections.abc import Iterator

import psycopg
import pytest

TEST_URL = os.environ.get("PM_TEST_DATABASE_URL", "postgresql+psycopg://pm:pm@db:5432/pm_test")
os.environ["PM_DATABASE_URL"] = TEST_URL
os.environ.setdefault("PM_MASTER_KEY", "dGVzdC1rZXktdGVzdC1rZXktdGVzdC1rZXktdGVzdDE=")
os.environ.setdefault("PM_RULES_DIR", "/srv/rules")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.platform.config import get_settings  # noqa: E402
from app.platform.db import engine, reset_engine, session_factory  # noqa: E402


def _ensure_database() -> None:
    plain = TEST_URL.replace("postgresql+psycopg://", "postgresql://")
    base, _, name = plain.rpartition("/")
    with psycopg.connect(base + "/postgres", autocommit=True) as conn:
        exists = conn.execute("select 1 from pg_database where datname = %s", (name,)).fetchone()
        if exists:
            conn.execute(f'drop database "{name}" with (force)')
        conn.execute(f'create database "{name}"')


@pytest.fixture(scope="session", autouse=True)
def _database() -> Iterator[None]:
    get_settings.cache_clear()
    _ensure_database()
    reset_engine()
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "migrations"))
    command.upgrade(cfg, "head")
    from app.main import load_modules

    load_modules()  # register every module's jobs and providers, so a test file also passes on its own
    yield
    reset_engine()


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    yield
    with engine().begin() as conn:
        tables = (
            conn.execute(
                text("select tablename from pg_tables where schemaname='public' and tablename <> 'alembic_version'")
            )
            .scalars()
            .all()
        )
        if tables:
            conn.execute(text("truncate " + ", ".join(f'"{t}"' for t in tables) + " restart identity cascade"))


@pytest.fixture
def db() -> Iterator[Session]:
    s = session_factory()()
    try:
        yield s
        s.commit()
    finally:
        s.close()


@pytest.fixture
def client() -> TestClient:
    from app.main import app

    return TestClient(app, headers={"X-PM-Request": "1"})


@pytest.fixture
def auth_client(client: TestClient) -> TestClient:
    r = client.post("/api/auth/setup", json={"username": "owner", "password": "correct horse battery"})
    assert r.status_code == 201, r.text
    return client
