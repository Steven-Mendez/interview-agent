"""Integration database isolated from the application's database."""

import os

# Offline tests must not depend on a developer's .env: the process manifest
# builds real (never invoked) OpenAI clients, which require some key. The live
# calibration suite keeps the real key from .env.
if os.environ.get("RUN_LIVE_LLM_TESTS") != "1":
    os.environ["OPENAI_API_KEY"] = "synthetic-offline-test-key"
# No suite traces to the developer's LangSmith (a real key would send synthetic
# interviews there): without a key config turns tracing off. Tracing tests turn
# it on with a mock client (the langsmith_runs fixture).
os.environ["LANGSMITH_API_KEY"] = ""
os.environ["LANGSMITH_PROJECT"] = "interview-agent"
os.environ["LANGSMITH_ENDPOINT"] = "https://api.smith.langchain.com"
# Nor to the developer's metrics collector; metric tests read their own
# in-memory reader (the recorded_metrics fixture).
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""
os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = ""

from unittest.mock import Mock

import langsmith
import pytest
from langchain_core.tracers.langchain import wait_for_all_tracers
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

from interview_agent import otel_metrics
from interview_agent.config import Settings
from interview_agent.interview import db
from interview_agent.runtime import EXPECTED_REVISION


@pytest.fixture(scope="session")
def integration_database_url():
    """TEST_DATABASE_URL when set (e.g. CI's Postgres service), else a throwaway
    Postgres container for this session, so Docker is the only local setup. It
    starts only when a test needs the database and is removed at the end."""
    if explicit := os.environ.get("TEST_DATABASE_URL"):
        yield make_url(explicit)
        return
    with PostgresContainer(
        "postgres:16-alpine",
        username="interview",
        password="interview",
        dbname="interview_test",
        driver="asyncpg",
    ) as postgres:
        yield make_url(postgres.get_connection_url())


@pytest.fixture
async def postgres_sessionmaker(integration_database_url):
    url = integration_database_url
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
        await connection.execute(
            text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": EXPECTED_REVISION}
        )
    try:
        yield sessionmaker
    finally:
        await engine.dispose()


@pytest.fixture
def recorded_metrics():
    """This process's metrics in memory instead of OTLP. Calling the helper with
    a full metric name (interview_agent.<component>.<name>) returns its data
    points so far; recording is unconfigured again afterwards."""
    reader = InMemoryMetricReader()
    otel_metrics.configure(Settings(_env_file=None), "interview-agent-test", reader=reader)

    def points(name: str) -> list:
        data = reader.get_metrics_data()
        return [
            point
            for resource in (data.resource_metrics if data else [])
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == name
            for point in metric.data.data_points
        ]

    try:
        yield points
    finally:
        otel_metrics.shutdown()


@pytest.fixture(autouse=True)
def tracing_stays_off():
    """No test leaves LangSmith tracing (or a client) behind for the next one."""
    yield
    langsmith.configure(enabled=False, client=None)


@pytest.fixture
def langsmith_runs():
    """Tracing on, as a LangSmith key turns it on, with a mock client: nothing
    leaves the process. Calling the helper returns the runs created so far."""
    client = Mock()
    langsmith.configure(enabled=True, client=client)

    def runs() -> list[dict]:
        wait_for_all_tracers()
        return [call.kwargs for call in client.create_run.call_args_list]

    runs.client = client
    return runs
