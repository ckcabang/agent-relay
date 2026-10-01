"""Shared test setup: point every test at a dedicated PostgreSQL database.

Fixtures drop and recreate all tables, so the tests never use
RELAY_DATABASE_URL from your shell. They use RELAY_TEST_DATABASE_URL
(default: ``agent_relay_test`` on the compose ``postgres`` service published
at 127.0.0.1:5432), refuse any database whose name does not end in ``_test``,
and create it if it does not exist yet.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

TEST_DATABASE_URL = os.getenv(
    "RELAY_TEST_DATABASE_URL",
    "postgresql+psycopg://agent_relay:agent_relay@127.0.0.1:5432/agent_relay_test",
)


def _ensure_test_database(url: str) -> None:
    target = make_url(url)
    if not (target.database or "").endswith("_test"):
        pytest.exit(f"refusing to run tests against {target.database!r}: the database name must end in '_test'", 2)
    admin = create_engine(
        target.set(database="postgres"), isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 10}
    )
    try:
        with admin.connect() as connection:
            exists = connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target.database}
            )
            if not exists:
                quoted = connection.dialect.identifier_preparer.quote(target.database)
                connection.execute(text(f"CREATE DATABASE {quoted}"))
    except Exception as exc:  # noqa: BLE001 - turn any connection failure into one clear message
        pytest.exit(
            f"cannot reach PostgreSQL for tests at {target.render_as_string(hide_password=True)}: {exc}\n"
            "Start it with: docker compose up -d postgres",
            2,
        )
    finally:
        admin.dispose()


def pytest_configure(config: pytest.Config) -> None:
    # Runs before test modules import ``database``, which reads this variable.
    os.environ["RELAY_DATABASE_URL"] = TEST_DATABASE_URL
    os.environ.pop("DATABASE_URL", None)
    _ensure_test_database(TEST_DATABASE_URL)
