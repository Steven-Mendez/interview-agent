"""Exercise actual Alembic commands against an isolated migration database."""

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from interview_agent.interview import db
from interview_agent.runtime import EXPECTED_REVISION

ROOT = Path(__file__).resolve().parents[1]


async def test_v2_upgrade_preserves_legacy_data_and_blocks_destructive_downgrade(
    integration_database_url,
):
    source = integration_database_url
    assert source.database.endswith("_test"), "Migration tests require an explicit test database"
    target = source.set(database="interview_migration_test")
    admin = create_async_engine(source.set(database="postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        if not await connection.scalar(
            text("SELECT 1 FROM pg_database WHERE datname='interview_migration_test'")
        ):
            await connection.execute(text('CREATE DATABASE "interview_migration_test"'))
    await admin.dispose()
    engine = create_async_engine(target)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))

    async def alembic(*args):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "alembic",
            *args,
            cwd=ROOT,
            env={**os.environ, "DATABASE_URL": target.render_as_string(hide_password=False)},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, error = await process.communicate()
        return process.returncode, (output + error).decode()

    try:
        code, output = await alembic("upgrade", "c5e28d41f7a3")
        assert code == 0, output
        conversation_id, milestone_id = uuid.uuid4(), uuid.uuid4()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO conversations (id,status,job_offer,resume_markdown) "
                    "VALUES (:id,'evaluated','Synthetic role','Synthetic CV')"
                ),
                {"id": conversation_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO milestones "
                    "(id,conversation_id,position,title,description,completed) "
                    "VALUES (:id,:conversation_id,0,'SQL','Describe an index',true)"
                ),
                {"id": milestone_id, "conversation_id": conversation_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO messages (conversation_id,role,content,seq) "
                    "VALUES (:id,'user','An index makes lookups faster.',0)"
                ),
                {"id": conversation_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO evaluations "
                    "(conversation_id,hired,score,strengths,weaknesses,rationale,ended_by) "
                    "VALUES (:id,true,82,'[]','[]','Synthetic legacy evaluation','plan_complete')"
                ),
                {"id": conversation_id},
            )
        code, output = await alembic("upgrade", "head")
        assert code == 0, output
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == EXPECTED_REVISION
            )
            # Metrics are exported over OTLP; no table keeps them any more.
            for table in ("metric_events", "metric_aggregates"):
                assert (
                    await connection.scalar(text("SELECT to_regclass(:name)"), {"name": table})
                    is None
                )
        migrated_engine, sessionmaker = db.create_engine_and_sessionmaker(
            target.render_as_string(hide_password=False)
        )
        try:
            async with sessionmaker() as session:
                row = await db.get_conversation(session, conversation_id)
                assert row.evaluation.score == 82 and row.evaluation.hired is True
                assert row.run_config is None and row.evaluation.result is None
                assert row.stt_drain is None
                assert row.state_revision == 0
                assert row.worker_epoch == 0 and row.worker_owner_id is None
                assert row.worker_acquired_at is None and row.worker_lease_until is None
                assert row.worker_activity_at is None and row.worker_disconnected_at is None
                assert row.response_onset_samples == 0
                assert list(await session.scalars(select(db.TurnExecution))) == []
                assert row.milestones[0].lifecycle == "closed"
                assert row.milestones[0].close_reason == "legacy_unspecified"
                messages = await db.get_messages(session, conversation_id)
                assert (
                    messages[0].source_id is None
                    and messages[0].content == "An index makes lookups faster."
                )
                assert row.started_at == messages[0].created_at
                row.worker_epoch = 1
                row.worker_owner_id = uuid.uuid4()
                await session.commit()
            code, output = await alembic("downgrade", "c632ad97e120")
            assert code != 0 and "Downgrade would destroy worker ownership" in output
            async with sessionmaker() as session:
                row = await db.get_conversation(session, conversation_id)
                assert row.worker_epoch == 1 and row.worker_owner_id is not None
                row.worker_epoch, row.worker_owner_id = 0, None
                await session.commit()
            # The legacy metric tables come back below head: a server sample
            # without an interview still blocks the worker ownership downgrade.
            code, output = await alembic("downgrade", "34ae6815db20")
            assert code == 0, output
            metric_id = uuid.uuid4()
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO metric_events (id,component,name,value,dimensions) "
                        "VALUES (:id,'server','sweep_lag_seconds',0.1,CAST(:dimensions AS jsonb))"
                    ),
                    {"id": metric_id, "dimensions": '{"source": "lifecycle_sweeper"}'},
                )
            code, output = await alembic("downgrade", "c632ad97e120")
            assert code != 0 and "Downgrade would destroy worker ownership" in output
            async with engine.begin() as connection:
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM metric_events WHERE conversation_id IS NULL")
                    )
                    == 1
                )
                await connection.execute(
                    text("DELETE FROM metric_events WHERE id=:id"), {"id": metric_id}
                )
            code, output = await alembic("upgrade", "head")
            assert code == 0, output
            async with sessionmaker() as session:
                row = await db.get_conversation(session, conversation_id)
                row.run_config = {"schema_version": 2}
                row.evaluation.score = None
                row.evaluation.hired = None
                await session.commit()
            code, output = await alembic("downgrade", "c5e28d41f7a3")
            assert code != 0 and "Downgrade would destroy v2" in output
            async with sessionmaker() as session:
                row = await db.get_conversation(session, conversation_id)
                assert row.run_config == {"schema_version": 2}
                row.run_config = None
                await session.commit()
        finally:
            await migrated_engine.dispose()
        code, output = await alembic("downgrade", "c5e28d41f7a3")
        assert code == 0, output
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT score FROM evaluations WHERE conversation_id=:id"),
                    {"id": conversation_id},
                )
                is None
            )
        code, output = await alembic("upgrade", "head")
        assert code == 0, output
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE conversations SET stt_drain=CAST(:drain AS jsonb) WHERE id=:id"),
                {"id": conversation_id, "drain": '{"complete": false}'},
            )
        code, output = await alembic("downgrade", "0ad71283bce4")
        assert code != 0 and "Downgrade would destroy STT drain evidence" in output
        async with engine.begin() as connection:
            assert await connection.scalar(
                text("SELECT stt_drain FROM conversations WHERE id=:id"),
                {"id": conversation_id},
            ) == {"complete": False}
            await connection.execute(
                text("UPDATE conversations SET stt_drain=NULL WHERE id=:id"),
                {"id": conversation_id},
            )
        execution_id, owner_id = uuid.uuid4(), uuid.uuid4()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO turn_executions (id,conversation_id,turn_id,invocations_reserved) "
                    "VALUES (:id,:conversation,'durable-turn',1)"
                ),
                {"id": execution_id, "conversation": conversation_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO turn_invocations "
                    "(id,execution_id,ordinal,owner_id,state_revision) "
                    "VALUES (:id,:execution,1,:owner,0)"
                ),
                {"id": uuid.uuid4(), "execution": execution_id, "owner": owner_id},
            )
        code, output = await alembic("downgrade", "e79a42c608bd")
        assert code != 0 and "Downgrade would destroy durable turn reservations" in output
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT invocations_reserved FROM turn_executions WHERE id=:id"),
                    {"id": execution_id},
                )
                == 1
            )
            assert await connection.scalar(text("SELECT count(*) FROM turn_invocations")) == 1
        await assert_capture_schema(engine, alembic, conversation_id, milestone_id, execution_id)
        await assert_recovery_schema(engine, alembic)
        await assert_seal_schema(engine, alembic)
        await assert_external_deletion_schema(engine, alembic)
        await assert_manifest_schema(engine, alembic)
        await assert_profiles_schema(engine, alembic)
        await assert_accounts_schema(engine, alembic)
    finally:
        await engine.dispose()


async def assert_capture_schema(engine, alembic, conversation_id, milestone_id, execution_id):
    # A producer fence alone is valuable durable data, even without captures.
    for column, value in (("producer_owner_id", uuid.uuid4()), ("producer_epoch", 1)):
        async with engine.begin() as connection:
            await connection.execute(
                text(f"UPDATE turn_executions SET {column}=:value WHERE id=:id"),
                {"value": value, "id": execution_id},
            )
        code, output = await alembic("downgrade", "a3e72bc19054")
        assert code != 0 and "Downgrade would destroy logical capture versions" in output
        async with engine.begin() as connection:
            assert (
                await connection.scalar(
                    text(f"SELECT {column} FROM turn_executions WHERE id=:id"), {"id": execution_id}
                )
                == value
            )
            await connection.execute(
                text(f"UPDATE turn_executions SET {column}=NULL WHERE id=:id"), {"id": execution_id}
            )
    async with engine.begin() as connection:
        message_id = await connection.scalar(
            text("SELECT id FROM messages WHERE conversation_id=:id"), {"id": conversation_id}
        )
        assert (
            await connection.scalar(
                text("SELECT version FROM messages WHERE id=:id"), {"id": message_id}
            )
            is None
        )
        await connection.execute(
            text(
                "INSERT INTO evidence (id, conversation_id, milestone_id, message_id, quote) "
                "VALUES (:id,:conversation,:milestone,:message,'legacy quote')"
            ),
            {
                "id": uuid.uuid4(),
                "conversation": conversation_id,
                "milestone": milestone_id,
                "message": message_id,
            },
        )
    with pytest.raises(DBAPIError):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO evidence (id, conversation_id, milestone_id, message_id, "
                    "quote) VALUES (:id,:conversation,:milestone,:message,'legacy quote')"
                ),
                {
                    "id": uuid.uuid4(),
                    "conversation": conversation_id,
                    "milestone": milestone_id,
                    "message": message_id,
                },
            )
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO message_versions "
                "(message_id,version,content,source_id,interrupted,metrics) VALUES "
                "(:id,1,'First','sdk',false,'{}'), (:id,2,'Corrected','sdk2',false,'{}')"
            ),
            {"id": message_id},
        )
        await connection.execute(
            text(
                "INSERT INTO evidence (id, conversation_id, milestone_id, message_id, "
                "message_version, quote) VALUES (:id,:conversation,:milestone,:message,1,'First')"
            ),
            {
                "id": uuid.uuid4(),
                "conversation": conversation_id,
                "milestone": milestone_id,
                "message": message_id,
            },
        )
    with pytest.raises(DBAPIError, match="immutable"):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE message_versions SET content='Changed' WHERE message_id=:id AND "
                    "version=1"
                ),
                {"id": message_id},
            )
    with pytest.raises(DBAPIError, match="evidence_message_version_fk"):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO evidence (id, conversation_id, milestone_id, message_id, "
                    "message_version, quote) VALUES "
                    "(:id,:conversation,:milestone,:message,3,'Missing')"
                ),
                {
                    "id": uuid.uuid4(),
                    "conversation": conversation_id,
                    "milestone": milestone_id,
                    "message": message_id,
                },
            )
    code, output = await alembic("downgrade", "a3e72bc19054")
    assert code != 0 and "Downgrade would destroy logical capture versions" in output
    async with engine.begin() as connection:
        assert (
            await connection.scalar(
                text("SELECT content FROM message_versions WHERE message_id=:id AND version=1"),
                {"id": message_id},
            )
            == "First"
        )
        await connection.execute(
            text("DELETE FROM conversations WHERE id=:id"), {"id": conversation_id}
        )
        for table in (
            "messages",
            "evidence",
            "message_versions",
            "turn_executions",
            "turn_invocations",
        ):
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def assert_recovery_schema(engine, alembic):
    conversation, question, attempt, request, run = [uuid.uuid4() for _ in range(5)]
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO conversations (id,status,job_offer,resume_markdown) "
                "VALUES (:id,'completed','Role','CV')"
            ),
            {"id": conversation},
        )
        await connection.execute(
            text(
                "INSERT INTO turn_runs (id,conversation_id,turn_id,decision) "
                "VALUES (:id,:conversation,'opening','{}')"
            ),
            {"id": question, "conversation": conversation},
        )
        await connection.execute(
            text(
                "INSERT INTO question_deliveries (id,conversation_id,text,capture_order) "
                "VALUES (:id,:conversation,'Question?',0)"
            ),
            {"id": question, "conversation": conversation},
        )
        await connection.execute(
            text(
                "INSERT INTO question_attempts (id,question_id,explicit,status) "
                "VALUES (:id,:question,false,'started')"
            ),
            {"id": attempt, "question": question},
        )
    code, output = await alembic("downgrade", "b84dc72e0519")
    assert code != 0 and "Downgrade would destroy question delivery history" in output
    request_sql = text(
        "INSERT INTO evaluation_requests "
        "(id,conversation_id,automatic,transcript_hash,status,attempts,deadline_at) "
        "VALUES (:id,:conversation,true,'hash','running',1,now()+interval '5 minutes')"
    )
    async with engine.begin() as connection:
        await connection.execute(request_sql, {"id": request, "conversation": conversation})
        await connection.execute(
            text(
                "INSERT INTO evaluation_runs "
                "(id,conversation_id,request_id,ordinal,status,transcript_hash,config,lease_until) "
                "VALUES (:id,:conversation,:request,1,'running','hash','{}',"
                "now()+interval '2 minutes')"
            ),
            {"id": run, "conversation": conversation, "request": request},
        )
    code, output = await alembic("downgrade", "ef293b0d18a7")
    assert code != 0 and "Downgrade would destroy evaluation request history" in output
    with pytest.raises(DBAPIError, match="evaluation_automatic_once"):
        async with engine.begin() as connection:
            await connection.execute(
                request_sql, {"id": uuid.uuid4(), "conversation": conversation}
            )
    async with engine.begin() as connection:
        assert (
            await connection.scalar(
                text("SELECT request_id FROM evaluation_runs WHERE id=:id"), {"id": run}
            )
            == request
        )
        await connection.execute(
            text("DELETE FROM conversations WHERE id=:id"), {"id": conversation}
        )
        for table in (
            "evaluation_requests",
            "evaluation_runs",
            "question_deliveries",
            "question_attempts",
        ):
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def assert_seal_schema(engine, alembic):
    conversation, seal, incident, resolution = [uuid.uuid4() for _ in range(4)]
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO conversations (id,status,job_offer,resume_markdown) "
                "VALUES (:id,'completed','Synthetic','Synthetic')"
            ),
            {"id": conversation},
        )
        await connection.execute(
            text(
                "INSERT INTO transcript_seals "
                "(id,conversation_id,version,records,provenance,transcript_hash,integrity) "
                "VALUES (:id,:conversation,1,'[]','{}','synthetic-hash','partial')"
            ),
            {"id": seal, "conversation": conversation},
        )
        await connection.execute(
            text("UPDATE conversations SET transcript_seal_id=:seal WHERE id=:id"),
            {"id": conversation, "seal": seal},
        )
        await connection.execute(
            text(
                "INSERT INTO capture_incidents "
                "(id,conversation_id,seal_id,turn_id,kind,payload,fingerprint) "
                "VALUES (:id,:conversation,:seal,'tail','late_after_seal','{}',"
                "'synthetic-incident')"
            ),
            {"id": incident, "conversation": conversation, "seal": seal},
        )
        await connection.execute(
            text(
                "INSERT INTO incident_resolutions "
                "(id,conversation_id,incident_id,decision,rationale,reviewer) "
                "VALUES (:id,:conversation,:incident,'omission','Checked audio','Reviewer')"
            ),
            {"id": resolution, "conversation": conversation, "incident": incident},
        )
    for statement in (
        "UPDATE transcript_seals SET integrity='complete'",
        "DELETE FROM transcript_seals",
        "UPDATE incident_resolutions SET decision='duplicate'",
        "DELETE FROM incident_resolutions",
    ):
        with pytest.raises(DBAPIError, match="Sealed history is immutable"):
            async with engine.begin() as connection:
                await connection.execute(text(statement))
    code, output = await alembic("downgrade", "f084ac76d119")
    assert code != 0 and "Downgrade would destroy sealed transcript history" in output
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM conversations WHERE id=:id"), {"id": conversation}
        )
        for table in ("transcript_seals", "incident_resolutions", "capture_incidents"):
            assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def external_deletion_schema(connection):
    """Which of the LangSmith deletion jobs' table, trigger function and trigger exist."""
    return {
        "table": await connection.scalar(text("SELECT to_regclass('external_traces')")),
        "function": await connection.scalar(
            text("SELECT to_regprocedure('tombstone_conversation_traces()')")
        ),
        "trigger": await connection.scalar(
            text("SELECT 1 FROM pg_trigger WHERE tgname='conversations_trace_tombstones'")
        ),
    }


async def assert_external_deletion_schema(engine, alembic):
    # The app no longer deletes LangSmith traces: head has no deletion jobs.
    async with engine.connect() as connection:
        assert set((await external_deletion_schema(connection)).values()) == {None}
    # Below head they come back, empty, and still behave as they did there.
    code, output = await alembic("downgrade", "dad9ce0068bd")
    assert code == 0, output
    async with engine.connect() as connection:
        assert None not in (await external_deletion_schema(connection)).values()
        assert await connection.scalar(text("SELECT count(*) FROM external_traces")) == 0
    conversation, trace = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO conversations (id,status,job_offer,resume_markdown) "
                "VALUES (:id,'completed','Synthetic','Synthetic')"
            ),
            {"id": conversation},
        )
        await connection.execute(
            text(
                "INSERT INTO external_traces (id,conversation_id,endpoint,project_name,expires_at) "
                "VALUES (:id,:conversation,'https://api.smith.langchain.com','Synthetic', "
                "clock_timestamp()+interval '30 days')"
            ),
            {"id": trace, "conversation": conversation},
        )
        # SQL callers cannot bypass tombstoning. The trace remains unlinked
        # and durable after CASCADE removes all local content.
        await connection.execute(
            text("DELETE FROM conversations WHERE id=:id"), {"id": conversation}
        )
        row = (
            await connection.execute(
                text(
                    "SELECT conversation_id,state,deletion_requested_at,next_attempt_at "
                    "FROM external_traces WHERE id=:id"
                ),
                {"id": trace},
            )
        ).one()
        assert row.conversation_id is None and row.state == "pending"
        assert row.deletion_requested_at is not None and row.next_attempt_at is not None
    code, output = await alembic("downgrade", "1b4f70c9d821")
    assert code != 0 and "Downgrade would destroy external deletion jobs" in output
    # Upgrading drops them again, pending jobs included: nothing deletes them now.
    code, output = await alembic("upgrade", "head")
    assert code == 0, output
    async with engine.connect() as connection:
        assert set((await external_deletion_schema(connection)).values()) == {None}


async def assert_manifest_schema(engine, alembic):
    manifest = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO process_manifests (id,role,snapshot) "
                "VALUES (:id,'api','{\"verification\":\"local_factory_only\"}')"
            ),
            {"id": manifest},
        )
    code, output = await alembic("downgrade", "23c4d8e190af")
    assert code != 0 and "Downgrade would destroy execution manifests" in output


async def profiles_schema(connection):
    """Which of the profiles' table and index exist."""
    return {
        name: await connection.scalar(text("SELECT to_regclass(:name)"), {"name": name})
        for name in ("user_profiles", "user_profiles_last_seen_idx")
    }


async def assert_profiles_schema(engine, alembic):
    async with engine.connect() as connection:
        assert None not in (await profiles_schema(connection)).values()
        assert await connection.scalar(text("SELECT count(*) FROM user_profiles")) == 0
    # Empty, the profiles go and come back with the accounts below them.
    code, output = await alembic("downgrade", "6a1f0c3e9b27")
    assert code == 0, output
    async with engine.connect() as connection:
        assert set((await profiles_schema(connection)).values()) == {None}
    # Users the accounts tables know: through an interview (the oldest one
    # dates them), a quota or settings. An interview from before accounts
    # belongs to nobody and makes no profile.
    first, later, nobodys = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as connection:
        for conversation, owner, days_ago in (
            (first, "user-synthetic", 9),
            (later, "user-synthetic", 2),
            (nobodys, None, 30),
        ):
            await connection.execute(
                text(
                    "INSERT INTO conversations (id,status,job_offer,resume_markdown,owner_id,"
                    "created_at) VALUES (:id,'planned','Synthetic','Synthetic',:owner,"
                    "now() - make_interval(days => :days))"
                ),
                {"id": conversation, "owner": owner, "days": days_ago},
            )
        await connection.execute(
            text(
                "INSERT INTO user_interview_quotas (owner_id,interviews_used) "
                "VALUES ('user-synthetic',2),('local:guest',1)"
            )
        )
        await connection.execute(text("INSERT INTO user_settings (owner_id) VALUES ('local-dev')"))
    code, output = await alembic("upgrade", "head")
    assert code == 0, output
    async with engine.begin() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT owner_id,auth_provider,email,name,last_seen_at,"
                    "created_at = (SELECT created_at FROM conversations WHERE id=:first) AS dated,"
                    "created_at > now() - interval '1 minute' AS fresh "
                    "FROM user_profiles ORDER BY owner_id"
                ),
                {"first": first},
            )
        ).all()
        assert [tuple(row) for row in rows] == [
            ("local-dev", "local", None, None, None, False, True),
            ("local:guest", "local", None, None, None, False, True),
            ("user-synthetic", "neon", None, None, None, True, False),
        ]
        with pytest.raises(DBAPIError, match="user_profile_provider"):
            async with connection.begin_nested():
                await connection.execute(
                    text("INSERT INTO user_profiles (owner_id,auth_provider) VALUES ('x','google')")
                )
    code, output = await alembic("downgrade", "6a1f0c3e9b27")
    assert code != 0 and "Downgrade would destroy user profiles" in output
    async with engine.begin() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM user_profiles")) == 3
        await connection.execute(text("DELETE FROM user_profiles"))
        await connection.execute(text("DELETE FROM user_settings"))
        await connection.execute(text("DELETE FROM user_interview_quotas"))
        await connection.execute(
            text("DELETE FROM conversations WHERE id IN (:first,:later,:nobodys)"),
            {"first": first, "later": later, "nobodys": nobodys},
        )
    code, output = await alembic("downgrade", "6a1f0c3e9b27")
    assert code == 0, output
    code, output = await alembic("upgrade", "head")
    assert code == 0, output


async def accounts_schema(connection):
    """Which of the accounts' column, index and tables exist."""
    return {
        "owner_id": await connection.scalar(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='conversations' AND column_name='owner_id'"
            )
        ),
        "index": await connection.scalar(
            text("SELECT to_regclass('conversations_owner_created_idx')")
        ),
        **{
            table: await connection.scalar(text("SELECT to_regclass(:name)"), {"name": table})
            for table in ("user_settings", "user_interview_quotas", "guest_interview_months")
        },
    }


async def assert_accounts_schema(engine, alembic):
    async with engine.connect() as connection:
        assert None not in (await accounts_schema(connection)).values()
    conversation = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO conversations (id,status,job_offer,resume_markdown) "
                "VALUES (:id,'planned','Synthetic','Synthetic')"
            ),
            {"id": conversation},
        )
    # An owner, a user's settings, a quota or a month's count: each is data
    # the downgrade would destroy.
    for insert, undo in (
        (
            f"UPDATE conversations SET owner_id='user-synthetic' WHERE id='{conversation}'",
            "UPDATE conversations SET owner_id=NULL",
        ),
        (
            "INSERT INTO user_settings (owner_id) VALUES ('user-synthetic')",
            "DELETE FROM user_settings",
        ),
        (
            "INSERT INTO user_interview_quotas (owner_id,interviews_used) "
            "VALUES ('user-synthetic',3)",
            "DELETE FROM user_interview_quotas",
        ),
        (
            "INSERT INTO guest_interview_months (month,interviews_started) "
            "VALUES (date_trunc('month', now())::date,2)",
            "DELETE FROM guest_interview_months",
        ),
    ):
        async with engine.begin() as connection:
            await connection.execute(text(insert))
        code, output = await alembic("downgrade", "2d4e8adaf02e")
        assert code != 0 and "Downgrade would destroy interview owners" in output
        async with engine.begin() as connection:
            assert None not in (await accounts_schema(connection)).values()
            await connection.execute(text(undo))
    async with engine.connect() as connection:
        # Defaults match app_settings, and a new quota starts at zero.
        assert (
            await connection.scalar(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name='user_interview_quotas' AND column_name='interviews_used'"
                )
            )
            == "0"
        )
    code, output = await alembic("downgrade", "2d4e8adaf02e")
    assert code == 0, output
    async with engine.connect() as connection:
        assert set((await accounts_schema(connection)).values()) == {None}
        # The interview itself survives, nobody's again.
        assert await connection.scalar(
            text("SELECT count(*) FROM conversations WHERE id=:id"), {"id": conversation}
        )
    code, output = await alembic("upgrade", "head")
    assert code == 0, output
