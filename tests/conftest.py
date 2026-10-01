"""Integration database isolated from the application's database."""

import os

# Offline tests must not depend on a developer's .env: the process manifest
# builds real (never invoked) OpenAI clients, which require some key. The live
# calibration suite keeps the real key from .env.
if os.environ.get("RUN_LIVE_LLM_TESTS") != "1":
    os.environ["OPENAI_API_KEY"] = "synthetic-offline-test-key"

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from interview_agent.interview import db


@pytest.fixture
async def postgres_sessionmaker():
    url = make_url(
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql+asyncpg://interview:interview@localhost:5432/interview_test",
        )
    )
    if not url.database or not url.database.endswith("_test"):
        raise ValueError("Integration tests require a dedicated *_test database")
    admin = create_async_engine(
        url.set(database="postgres").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    async with admin.connect() as connection:
        exists = await connection.scalar(
            text("SELECT 1 FROM pg_database WHERE datname=:name"),
            {"name": url.database},
        )
        if not exists:
            await connection.execute(text(f'CREATE DATABASE "{url.database}"'))
    await admin.dispose()
    engine, sessionmaker = db.create_engine_and_sessionmaker(
        url.render_as_string(hide_password=False),
    )
    async with engine.begin() as connection:
        # A previous test run can have an older schema without newly named
        # cyclic constraints. Reset only the validated dedicated test schema.
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
        await connection.run_sync(db.Base.metadata.create_all)
        # ORM fixtures prove application behavior; real migrations are tested separately.
        await connection.execute(
            text("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
        )
        await connection.execute(text("INSERT INTO alembic_version VALUES ('34ae6815db20')"))
    try:
        yield sessionmaker
    finally:
        await engine.dispose()
