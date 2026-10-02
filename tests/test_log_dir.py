"""LOG_DIR: where the rotating log files go, and console only when they cannot."""

import logging

import pytest

from interview_agent.config import Settings
from interview_agent.logging_config import setup_file_logging

FILE_HANDLER_TAG = "interview_agent_file_log"


def file_handlers():
    root = logging.getLogger()
    return [h for h in root.handlers if getattr(h, "_tag", None) == FILE_HANDLER_TAG]


@pytest.fixture
def restored_root_logging():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    formatters = {handler: handler.formatter for handler in original_handlers}
    level = root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in original_handlers:
                root.removeHandler(handler)
                handler.close()
        for handler, formatter in formatters.items():
            handler.setFormatter(formatter)
        root.setLevel(level)


def test_default_log_dir_writes_the_file_under_logs(tmp_path, monkeypatch, restored_root_logging):
    monkeypatch.chdir(tmp_path)
    # The default is what the test is about: a LOG_DIR exported in the shell
    # would still reach Settings with the env file off.
    monkeypatch.delenv("LOG_DIR", raising=False)
    settings = Settings(_env_file=None)
    assert settings.log_dir == "logs"
    assert str(settings.log_file("server.log")) == "logs/server.log"

    log_path = setup_file_logging(settings.log_file("server.log"))

    assert log_path is not None and log_path.resolve() == tmp_path / "logs" / "server.log"
    logging.getLogger("interview_agent.server").info("server ready")
    for handler in file_handlers():
        handler.flush()
    assert "server ready" in (tmp_path / "logs" / "server.log").read_text()


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_log_dir_disables_file_logging(value, restored_root_logging):
    settings = Settings(_env_file=None, LOG_DIR=value)
    assert settings.log_file("agent.log") is None
    before = len(file_handlers())

    assert setup_file_logging(settings.log_file("agent.log")) is None
    assert len(file_handlers()) == before


def directory_cannot_be_created(tmp_path):
    # A regular file where the directory should be: mkdir fails on every host,
    # root included, the way a read-only filesystem fails for everyone else.
    occupied = tmp_path / "logs"
    occupied.write_text("not a directory")
    return occupied


def file_cannot_be_opened(tmp_path):
    # The directory exists but a directory sits where the file goes: mkdir
    # succeeds and opening the file fails, for root too.
    log_dir = tmp_path / "logs"
    (log_dir / "server.log").mkdir(parents=True)
    return log_dir


@pytest.mark.parametrize("occupy", [directory_cannot_be_created, file_cannot_be_opened])
def test_unwritable_log_dir_falls_back_to_console_with_a_warning(
    occupy, tmp_path, caplog, restored_root_logging
):
    occupied = occupy(tmp_path)
    settings = Settings(_env_file=None, LOG_DIR=str(occupied))
    before = len(file_handlers())

    with caplog.at_level(logging.WARNING, logger="interview_agent.logging_config"):
        assert setup_file_logging(settings.log_file("server.log")) is None

    assert len(file_handlers()) == before
    assert not (occupied / "server.log").is_file()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].msg == "file logging unavailable: log directory not writable; console only"
    # Configuration never reaches the line: the template is the whole message.
    assert str(tmp_path) not in warnings[0].getMessage()


def test_worker_entrypoint_consults_log_dir(tmp_path, monkeypatch, capsys, restored_root_logging):
    import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "run", lambda: None)
    before = len(file_handlers())

    monkeypatch.setattr(main.settings, "log_dir", "")
    main.main()
    assert "[interview-agent] Logs: console only" in capsys.readouterr().out
    assert len(file_handlers()) == before
    assert not (tmp_path / "logs").exists()

    monkeypatch.setattr(main.settings, "log_dir", "worker-logs")
    main.main()
    assert f"Writing logs to {tmp_path / 'worker-logs' / 'agent.log'}" in capsys.readouterr().out
    assert len(file_handlers()) == before + 1
    assert (tmp_path / "worker-logs" / "agent.log").is_file()
