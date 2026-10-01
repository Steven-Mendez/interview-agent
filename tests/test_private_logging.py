"""Content canaries must not escape through file, console, SDK or exceptions."""

import io
import logging
import sys
from pathlib import Path

import pytest
from livekit.agents.cli import cli as sdk_cli

from interview_agent import agent
from interview_agent.logging_config import MetadataFormatter, setup_file_logging

CANARY = "CANARY_CV_TRANSCRIPT_SECRET_95a34"


class Sensitive:
    def __str__(self):
        raise AssertionError("Sensitive object must never be stringified")

    def __repr__(self):
        raise AssertionError("Sensitive object must never be represented")


@pytest.fixture
def restored_logging():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    level = root.level
    existing = {
        name: (logger.level, list(logger.handlers))
        for name, logger in logging.Logger.manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
    }
    formatters = {handler: handler.formatter for handler in original_handlers}
    for _, handlers in existing.values():
        formatters.update({h: h.formatter for h in handlers})
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in original_handlers:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(level)
        for name, (prior_level, handlers) in existing.items():
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                if handler not in handlers:
                    logger.removeHandler(handler)
                    handler.close()
            logger.setLevel(prior_level)
        for handler, formatter in formatters.items():
            handler.setFormatter(formatter)


def record(name, msg, args=(), **extras):
    item = logging.LogRecord(name, logging.ERROR, "/private/" + CANARY, 31, msg, args, None)
    item.__dict__.update(extras)
    return item


@pytest.mark.parametrize(
    "name",
    ["pdfminer.pdfinterp", "livekit.agents", "openai._base_client", "httpx", "arbitrary." + CANARY],
)
def test_unknown_messages_extras_arguments_paths_and_cached_exception_text_are_omitted(name):
    item = record(
        name,
        CANARY,
        (Sensitive(),),
        transcript=CANARY,
        resume=Sensitive(),
        headers={"Authorization": CANARY},
        exc_text=CANARY,
        stack_info=CANARY,
        filename=CANARY,
    )
    output = MetadataFormatter().format(item)
    assert CANARY not in output
    assert "content omitted" in output
    assert item.msg == CANARY and item.args  # Do not mutate records for another consumer.


def test_owned_event_preserves_template_and_safe_numbers_without_linked_ids_or_body():
    item = record(
        "interview_agent.server",
        "planning failed for %s",
        (Sensitive(),),
        conversation=CANARY,
        bytes=245,
        attempt=2,
        duration=float("nan"),
        status_code=CANARY,
    )
    output = MetadataFormatter().format(item)
    assert "planning failed for %s" in output
    assert "bytes=245" in output and "attempt=2" in output
    assert "duration=" not in output and "status_code=" not in output and CANARY not in output


def test_exception_body_traceback_and_stack_info_are_not_formatted():
    try:
        raise ValueError(CANARY)
    except ValueError:
        item = record(
            "interview_agent.server",
            "planning failed for %s",
            (CANARY,),
            exc_info=sys.exc_info(),
            stack_info=CANARY,
        )
        output = MetadataFormatter().format(item)
    assert "exception_type=ValueError" in output
    assert CANARY not in output and "Traceback" not in output


def test_file_and_nonpropagating_console_are_private_and_rotation_remains_private(
    tmp_path, restored_logging
):
    stream = io.StringIO()
    console = logging.StreamHandler(stream)
    foreign = logging.getLogger("pdfminer.canary")
    foreign.addHandler(console)
    foreign.propagate = False
    foreign.setLevel(logging.DEBUG)
    try:
        path = setup_file_logging(str(tmp_path / "private.log"), level=logging.DEBUG)
        root = logging.getLogger()
        file_handler = next(
            h for h in root.handlers if getattr(h, "_tag", None) == "interview_agent_file_log"
        )
        foreign.debug("Parsed PDF %s", CANARY, extra={"resume": CANARY})
        logging.getLogger("interview_agent.server").error(
            "planning failed for %s", CANARY, extra={"transcript": CANARY}
        )
        file_handler.flush()
        assert CANARY not in stream.getvalue() and CANARY not in path.read_text()
        assert Path(file_handler.baseFilename).stat().st_mode & 0o777 == 0o600
        file_handler.doRollover()
        file_handler.flush()
        assert path.stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "private-1.log").stat().st_mode & 0o777 == 0o600
        assert setup_file_logging(str(tmp_path / "other.log")) == path.resolve()
    finally:
        foreign.removeHandler(console)
        foreign.propagate = True


def test_real_sdk_console_setup_cannot_bypass_privacy_and_adapter_restores(
    monkeypatch, tmp_path, restored_logging
):
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    original = sdk_cli.setup_logging
    setup_file_logging(str(tmp_path / "worker.log"))

    def invoke_cli(options):
        sdk_cli.setup_logging("DEBUG", devmode=False, console=False)
        logging.getLogger("livekit.agents").error(
            "Received transcript %s", CANARY, extra={"text": CANARY, "transcript": CANARY}
        )
        raise RuntimeError("controlled CLI exit")

    monkeypatch.setattr(agent.cli, "run_app", invoke_cli)
    with pytest.raises(RuntimeError, match="controlled CLI exit"):
        agent.run()
    assert sdk_cli.setup_logging is original
    assert CANARY not in output.getvalue()
    assert "technical event" in output.getvalue()
